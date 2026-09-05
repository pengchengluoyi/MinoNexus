"""节点登记表：node_id ↔ 连接 ↔ 设备。

这是拆分新引入的一层（docs/NODE_REGISTRY.md）。上游 MiniOrangeServer 只有 `sn`，
它既是设备标识又是连接端点；多节点下必须显式建 `node_id → devices[]` 的归属，
否则 Nexus 答不出"这台设备该派给哪个节点"。

**连通性是缓存，不是权威** —— 权威在 Scout 的 REGISTER / HEARTBEAT
（CLAUDE.md §6：不要把设备连通性当成本地可查的状态）。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from mino_nexus.core import protocol as P
from mino_nexus.services.device_store import remember as remember_device
from mino_nexus.core.log import SLog
from mino_nexus.services.node_store import get_node as stored_node, sanitize_id, upsert as persist_node
from mino_nexus.runtime.run_context import (
    LEGACY_WEB_SLOT_SNS,
    is_legacy_web_sn,
    is_per_node_web_sn,
)

TAG = "NodeRegistry"

# 协议 §1：Scout 每 15s 心跳，Nexus 45s 未收到视为掉线
HEARTBEAT_TIMEOUT_SEC = 45.0
_ONLINE_CHANNEL = frozenset({"connected", "online", "available"})


def _channel_value(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("state") or value.get("status") or "").strip().lower()
    return str(value or "").strip().lower()


def _channel_connected(channels: Any) -> bool:
    if not isinstance(channels, dict):
        return False
    return any(_channel_value(v) in _ONLINE_CHANNEL for v in channels.values())


@dataclass
class NodeSession:
    node_id: str
    platform: str = ""
    arch: str = ""
    scout_version: str = ""
    executors: dict[str, P.ExecutorManifest] = field(default_factory=dict)
    devices: dict[str, P.DeviceManifest] = field(default_factory=dict)
    last_seen: float = field(default_factory=time.time)
    busy: bool = False
    active_runs: list[str] = field(default_factory=list)
    draining: bool = False
    hostname: str = ""
    studio_id: str = ""
    owner_user_id: str = ""
    # 发一条消息并等应答；由 websocket/node.py 注入
    send: Optional[Callable[..., Any]] = None

    @property
    def alive(self) -> bool:
        return (time.time() - self.last_seen) < HEARTBEAT_TIMEOUT_SEC

    def available_executors(self) -> list[str]:
        return sorted(k for k, v in self.executors.items() if v.available)

    def provides(self) -> set[str]:
        """该节点所有可用 executor 提供的 abstract cap 全集。"""
        out: set[str] = set()
        for ex in self.executors.values():
            if ex.available:
                out.update(ex.provides or [])
        return out

    def connectivity_flags(self) -> dict[str, bool]:
        """喂给 catalog.registry.filter_capabilities。"""
        return {ex_id: True for ex_id in self.available_executors()}

    def brief(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "scout_id": self.node_id,
            "platform": self.platform,
            "arch": self.arch,
            "hostname": self.hostname,
            "studio_id": self.studio_id,
            "owner_user_id": self.owner_user_id,
            "scout_version": self.scout_version,
            "alive": self.alive,
            "busy": self.busy,
            "draining": self.draining,
            "active_runs": list(self.active_runs),
            "executors": {
                k: {"available": v.available, "provides": len(v.provides or []), "reason": v.reason}
                for k, v in sorted(self.executors.items())
            },
            "devices": sorted(self.devices),
            "device_count": len(self.devices),
            "last_seen": self.last_seen,
            "last_seen_ago_sec": round(time.time() - self.last_seen, 1),
        }

    def persist_snapshot(self) -> None:
        persist_node(
            self.node_id,
            hostname=self.hostname,
            platform=self.platform,
            arch=self.arch,
            scout_version=self.scout_version,
            studio_id=self.studio_id,
            owner_user_id=self.owner_user_id,
            device_count=len(self.devices),
            last_seen=self.last_seen,
        )


class NodeRegistry:
    """进程内单例。Nexus 重启后等 Scout 重新 REGISTER 恢复（ARCHITECTURE.md §7）。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._nodes: dict[str, NodeSession] = {}
        # sn -> node_id 归属
        self._owner: dict[str, str] = {}

    # ---------------- 注册 ----------------

    def register(
        self,
        reg: P.Register,
        send: Callable[..., Any],
        *,
        owner_user_id: str = "",
    ) -> list[str]:
        """处理 REGISTER，返回要放进 REGISTERED.warnings 的提示。"""
        warnings: list[str] = []
        with self._lock:
            prev = self._nodes.get(reg.node_id)
            if prev is not None:
                SLog.i(TAG, f"节点 {reg.node_id} 重新注册（覆盖上一次会话）")

            incoming = {d.sn: d for d in reg.devices}
            has_new_web = any(is_per_node_web_sn(sn) for sn in incoming)
            self._retire_legacy_web_slot(reg.node_id, has_new_web)

            devices = dict(incoming)
            if prev is not None:
                for sn, old in prev.devices.items():
                    if sn in devices:
                        continue
                    if is_legacy_web_sn(sn):
                        continue
                    stale = P.DeviceManifest(
                        sn=old.sn, platform=old.platform, model=old.model,
                        channels={ch: "disconnected" for ch in (old.channels or {})},
                    )
                    devices[sn] = stale

            studio_id = sanitize_id(getattr(reg, "studio_id", "") or "")
            hostname = str(getattr(reg, "hostname", "") or "").strip()
            owner = sanitize_id(owner_user_id)
            stored = stored_node(reg.node_id) or {}
            if prev is not None:
                studio_id = studio_id or prev.studio_id
                hostname = hostname or prev.hostname
                owner = owner or prev.owner_user_id
            studio_id = studio_id or sanitize_id(str(stored.get("studio_id") or ""))
            hostname = hostname or str(stored.get("hostname") or "").strip()
            owner = owner or sanitize_id(str(stored.get("owner_user_id") or ""))
            session = NodeSession(
                node_id=reg.node_id,
                platform=reg.platform,
                arch=reg.arch,
                scout_version=reg.scout_version,
                executors={e.id: e for e in reg.executors},
                devices=devices,
                send=send,
                hostname=hostname,
                studio_id=studio_id,
                owner_user_id=owner,
            )
            self._nodes[reg.node_id] = session
            session.persist_snapshot()

            for d in reg.devices:
                self._place_device(d.sn, reg.node_id, device=d, session=session)
            self._purge_orphaned_legacy_web_slots()

        avail = session.available_executors()
        SLog.i(
            TAG,
            f"REGISTER node={reg.node_id} scout=v{reg.scout_version} "
            f"executors={avail} devices={sorted(session.devices)}",
        )
        if not avail:
            warnings.append("该节点没有任何可用 executor，无法承接任务")
        return warnings

    def _retire_legacy_web_slot(self, node_id: str, has_new_web: bool) -> None:
        """Drop leftover `web-local` when this node now reports `web{scout_id}`.

        Only this node's leftover (or a dead previous owner after node_id
        format change). Never steal a live node's devices.
        """
        if has_new_web:
            for sn in LEGACY_WEB_SLOT_SNS:
                owner = self._owner.get(sn)
                if owner is None:
                    continue
                if owner == node_id:
                    self._owner.pop(sn, None)
                    continue
                other = self._nodes.get(owner)
                if other is None or not other.alive:
                    self._owner.pop(sn, None)
        self._purge_orphaned_legacy_web_slots()

    def _record_device(self, session: NodeSession, device: P.DeviceManifest, *, status: str = "") -> None:
        owner_name = ""
        uid = session.owner_user_id
        if uid:
            try:
                from mino_nexus.services import auth_store
                pub = auth_store.public_user_by_id(uid)
                if pub:
                    owner_name = str(pub.get("name") or pub.get("username") or "")
            except Exception:
                owner_name = ""
        remember_device(
            device.sn,
            platform=device.platform,
            model=device.model,
            scout_id=session.node_id,
            studio_id=session.studio_id,
            owner_user_id=uid,
            owner_name=owner_name,
            channels=dict(device.channels or {}),
            status=status,
        )

    def _place_device(
        self,
        sn: str,
        node_id: str,
        *,
        device: P.DeviceManifest | None = None,
        session: NodeSession | None = None,
    ) -> None:
        """当前位置跟当前 Scout；账号归属由 device_store 按首次出现锁定。"""
        prev = self._owner.get(sn)
        if prev and prev != node_id:
            other = self._nodes.get(prev)
            if other is not None and sn in other.devices:
                other.devices.pop(sn, None)
            SLog.i(TAG, f"设备 {sn} 当前位置 {prev} → {node_id}（账号归属不改）")
        self._owner[sn] = node_id
        node = session or self._nodes.get(node_id)
        if node is not None and device is not None:
            self._record_device(node, device)

    def _purge_orphaned_legacy_web_slots(self) -> None:
        """Drop leftover web-local / web_local unless a live node still reports it."""
        for sn in LEGACY_WEB_SLOT_SNS:
            owner = self._owner.get(sn)
            node = self._nodes.get(owner) if owner else None
            if node is not None and node.alive and sn in node.devices:
                continue
            if owner is not None:
                self._owner.pop(sn, None)
            if node is not None:
                node.devices.pop(sn, None)
            for other in self._nodes.values():
                other.devices.pop(sn, None)

    def heartbeat(self, hb: P.Heartbeat) -> None:
        with self._lock:
            node = self._nodes.get(hb.node_id)
            if node is None:
                SLog.w(TAG, f"收到未注册节点的心跳: {hb.node_id}")
                return
            node.last_seen = time.time()
            node.busy = hb.busy
            node.active_runs = list(hb.active_runs or [])
            for d in hb.device_delta or []:
                if is_legacy_web_sn(d.sn) and not _channel_connected(d.channels):
                    node.devices.pop(d.sn, None)
                    if self._owner.get(d.sn) == hb.node_id:
                        self._owner.pop(d.sn, None)
                    continue
                if d.sn in node.devices:
                    node.devices[d.sn].channels.update(d.channels or {})
                    self._place_device(d.sn, hb.node_id, device=node.devices[d.sn], session=node)
                else:
                    node.devices[d.sn] = d
                    self._place_device(d.sn, hb.node_id, device=d, session=node)
                    SLog.i(TAG, f"心跳里出现设备 {d.sn}，当前节点 {hb.node_id}")
            self._purge_orphaned_legacy_web_slots()
            if node is not None:
                node.persist_snapshot()

    def node_event(self, req: P.Execute, *, node_id: str = "") -> list[str]:
        """处理 S→N 框架 EXECUTE（node.device_lost 等）。`shutting_down` 返回在途 run_id。"""
        params = dict(req.params or {})
        nid = str(node_id or params.get("node_id") or "").strip()
        cap = str(req.capability_id or "")
        event = cap.split(".", 1)[-1] if cap.startswith("node.") else cap
        sn = str(req.sn or req.device_id or params.get("sn") or "").strip()
        detail = str(params.get("detail") or "")
        interrupted: list[str] = []
        with self._lock:
            node = self._nodes.get(nid)
            if node is None:
                return []
            if event == "device_lost" and sn in node.devices:
                if is_legacy_web_sn(sn):
                    node.devices.pop(sn, None)
                    if self._owner.get(sn) == nid:
                        self._owner.pop(sn, None)
                else:
                    for ch in node.devices[sn].channels:
                        node.devices[sn].channels[ch] = "disconnected"
            elif event == "device_found" and sn:
                if sn not in node.devices:
                    node.devices[sn] = P.DeviceManifest(
                        sn=sn, platform=str(req.platform or ""), channels={},
                    )
                self._place_device(sn, nid, device=node.devices[sn], session=node)
            elif event == "shutting_down":
                node.draining = True
                node.busy = False
                interrupted = list(node.active_runs or [])
                node.active_runs = []
            self._purge_orphaned_legacy_web_slots()
            if node is not None:
                node.persist_snapshot()
        SLog.i(TAG, f"EXECUTE {cap} node={nid} sn={sn} {detail}")
        return interrupted

    def disconnect(self, node_id: str) -> list[str]:
        """连接断了。**不清除设备归属** —— 否则 UI 上的设备会闪现闪灭
        （NODE_REGISTRY.md §2）。只把连通性标为不可用。

        返回断开时仍挂在该节点上的 run_id，调用方应立刻判失败，不要干等超时。
        """
        with self._lock:
            node = self._nodes.pop(node_id, None)
            runs = list(node.active_runs) if node is not None else []
            if node is not None:
                node.persist_snapshot()
            self._purge_orphaned_legacy_web_slots()
        if node is not None:
            SLog.i(
                TAG,
                f"节点 {node_id} 断开，名下 {len(node.devices)} 台设备置为离线"
                + (f"，中断 {len(runs)} 条在途 run" if runs else ""),
            )
        return runs

    # ---------------- 查询 / 派单 ----------------

    def nodes(self) -> list[NodeSession]:
        with self._lock:
            return [n for n in self._nodes.values()]

    def get_node(self, node_id: str) -> Optional[NodeSession]:
        with self._lock:
            return self._nodes.get(node_id)

    def resolve(self, sn: str) -> tuple[Optional[NodeSession], str]:
        """sn → 该设备所在的活节点。

        返回 (session, 拒绝原因)。session 为 None 时原因非空且**必须说清是哪一种**
        —— 派单失败时立刻明说，不要静默排队（NODE_REGISTRY.md §4）。
        """
        with self._lock:
            owner = self._owner.get(sn)
            if owner is None:
                known = sorted(self._owner)
                return None, (
                    f"设备 {sn} 未登记到任何节点"
                    + (f"（已知设备：{known}）" if known else "（当前没有任何节点在线）")
                )
            node = self._nodes.get(owner)
            if node is None:
                return None, f"设备 {sn} 所属节点 {owner} 离线"
            if not node.alive:
                ago = round(time.time() - node.last_seen, 1)
                return None, f"设备 {sn} 所属节点 {owner} 心跳超时（{ago}s 未上报）"
            if node.draining:
                return None, f"设备 {sn} 所属节点 {owner} 正在关闭"
            return node, ""

    def devices(self) -> list[dict[str, Any]]:
        """给 UI / 调试接口用的设备清单。连通性来自 Scout 上报。"""
        out: list[dict[str, Any]] = []
        with self._lock:
            self._purge_orphaned_legacy_web_slots()
            for sn, node_id in sorted(self._owner.items()):
                node = self._nodes.get(node_id)
                d = node.devices.get(sn) if node else None
                out.append(
                    {
                        "sn": sn,
                        "node_id": node_id,
                        "node_alive": bool(node and node.alive),
                        "platform": d.platform if d else "",
                        "model": d.model if d else "",
                        "channels": dict(d.channels) if d else {},
                        "last_seen": node.last_seen if node else 0,
                    }
                )
        return out


_registry: Optional[NodeRegistry] = None
_registry_lock = threading.Lock()


def get_registry() -> NodeRegistry:
    global _registry
    with _registry_lock:
        if _registry is None:
            _registry = NodeRegistry()
    return _registry
