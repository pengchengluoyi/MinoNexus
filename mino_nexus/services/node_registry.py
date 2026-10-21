"""节点登记表：node_id ↔ 连接 ↔ 设备。

这是拆分新引入的一层（docs/NODE_REGISTRY.md）。上游 MiniOrangeServer 只有 `sn`，
它既是设备标识又是连接端点；多节点下必须显式建 `node_id → devices[]` 的归属，
否则 Nexus 答不出"这台设备该派给哪个节点"。

**连通性是缓存，不是权威** —— 权威在 Scout 的 REGISTER / HEARTBEAT
（CLAUDE.md §6：不要把设备连通性当成本地可查的状态）。
"""
from __future__ import annotations

import threading
import uuid
from copy import deepcopy
from dataclasses import asdict
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Optional

from mino_nexus.core import protocol as P
from mino_nexus.services.device_store import remember as remember_device, get_device
from mino_nexus.core.log import SLog
from mino_nexus.services.node_store import get_node as stored_node, sanitize_id, upsert as persist_node
from mino_nexus.runtime.run_context import (
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


def _device_identity_valid(device: P.DeviceManifest, node_id: str, host_platform: str) -> bool:
    from mino_nexus.core.platform_contract import device_identity
    try:
        device_identity({k:v for k,v in asdict(device).items() if k!="os" or v},node_id=node_id,host={"os":host_platform})
        return True
    except ValueError: return False


_PLUGIN_CLASSES = frozenset({"cli", "mcp", "bot", "mail", "im"})
_SECRET_MARKERS = ("secret", "password", "token", "webhook")


def _plain_values(raw: Any) -> dict[str, str]:
    """非密钥字段可以进节点列表，页面要回显。密钥值一律丢掉。"""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in raw.items():
        name = str(key or "").strip()
        if not name or any(flag in name.lower() for flag in _SECRET_MARKERS):
            continue
        text = str(value or "").strip()
        if text:
            out[name] = text[:500]
    return out


def _secret_names(raw: Any) -> list[str]:
    """只留字段名，表示保险库里有这份密钥。"""
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        name = str(item or "").strip()
        # LangBot 平台字段形如 lark.app_secret，带「平台.」前缀，所以允许点号
        if name and len(name) <= 64 and name.replace("_", "").replace(".", "").isalnum():
            out.append(name)
    return out


def _plugin_status(raw: Any) -> dict[str, Any]:
    """渠道连接状态：只留连接与否、最近收消息的时间、一句错误。不含消息正文。"""
    if not isinstance(raw, dict):
        return {}
    try:
        last = int(raw.get("last_message_at") or 0)
    except (TypeError, ValueError):
        last = 0
    out = {
        "last_message_at": last,
        "error": str(raw.get("error") or "")[:200],
    }
    # CLI message channels carry enabled in status (LangBot uses platforms).
    # Preserve the typed fact used by assistant_gateway source validation.
    if isinstance(raw.get("enabled"), bool):
        out["enabled"] = raw["enabled"]
    if isinstance(raw.get("connected"), bool):
        out["connected"] = raw["connected"]
    if isinstance(raw.get("connection_verified"), bool):
        out["connection_verified"] = raw["connection_verified"]
    if raw.get("connection_source") in ("recent_message", "unconfirmed"):
        out["connection_source"] = raw["connection_source"]
    if raw.get("instance_id"):
        out["instance_id"] = str(raw["instance_id"])[:128]
    if isinstance(raw.get("authorized_sender_id"), str) and raw["authorized_sender_id"].startswith("ou_"):
        out["authorized_sender_id"] = raw["authorized_sender_id"][:128]
    if raw.get("account_id"):
        out["account_id"] = str(raw["account_id"])[:128]
    if "holding" in raw:
        out["holding"] = bool(raw.get("holding"))
    if raw.get("state"):
        out["state"] = str(raw["state"])[:16]
        out["healthy"] = bool(raw.get("healthy"))
        out["version"] = str(raw.get("version") or "")[:32]
        out["restarts"] = int(raw.get("restarts") or 0)
    return out


def _plugin_platforms(raw: Any) -> dict[str, Any]:
    """LangBot 各平台状态：只留开关、连接与否、最近收消息时间、一句错误。"""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {}
    for pid, st in raw.items():
        key = str(pid or "").strip()
        if not key or len(key) > 32 or not isinstance(st, dict):
            continue
        try:
            last = int(st.get("last_message_at") or 0)
        except (TypeError, ValueError):
            last = 0
        out[key] = {
            **_plugin_status(st),
            **({"enabled": st["enabled"]} if isinstance(st.get("enabled"), bool) else {}),
        }
    return out


def sanitize_plugins(raw: Any) -> list[dict[str, Any]] | None:
    """状态加非密钥明文。密钥值不进节点列表。"""
    if raw is None:
        return None
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        kind = str(row.get("class") or "").strip()
        pid = str(row.get("id") or "").strip()
        if kind not in _PLUGIN_CLASSES or not pid:
            continue
        item: dict[str, Any] = {
            "class": kind,
            "id": pid,
            "installed": bool(row.get("installed")),
            "configured": bool(row.get("configured")),
        }
        status = _plugin_status(row.get("status"))
        if status:
            item["status"] = status
        values = _plain_values(row.get("values"))
        secrets = _secret_names(row.get("saved_secrets"))
        if values:
            item["values"] = values
        if secrets:
            item["saved_secrets"] = secrets
        fields = []
        for field in row.get("fields") or []:
            if not isinstance(field, dict):
                continue
            key = str(field.get("key") or "").strip()
            if not key:
                continue
            fields.append({
                "key": key,
                "label": str(field.get("label") or key),
                "placeholder": str(field.get("placeholder") or ""),
                "secret": bool(field.get("secret")),
                "optional": bool(field.get("optional")),
                **({"type": str(field["type"])[:16]} if field.get("type") else {}),
                **({"group": str(field["group"])[:32]} if field.get("group") else {}),
            })
        if fields:
            item["fields"] = fields
        if row.get("label"):
            item["label"] = str(row.get("label"))
        groups = [
            {"id": str(g.get("id") or "")[:32], "label": str(g.get("label") or "")[:40]}
            for g in (row.get("groups") or []) if isinstance(g, dict) and g.get("id")
        ]
        if groups:
            item["groups"] = groups
        platforms = _plugin_platforms(row.get("platforms"))
        if platforms:
            item["platforms"] = platforms
        out.append(item)
    return out


def _public_plugin_job(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_id": str(raw.get("task_id") or ""),
        "active": bool(raw.get("active")),
        "stage": str(raw.get("stage") or ""),
        "label": str(raw.get("label") or ""),
        "percent": int(raw.get("percent") or 0),
        "class": str(raw.get("class") or ""),
        "id": str(raw.get("id") or ""),
        "bytes_received": int(raw.get("bytes_received") or 0),
        "bytes_total": int(raw.get("bytes_total") or 0),
        "error": str(raw.get("error") or ""),
    }


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
    device_workload: list[P.DeviceWorkload] = field(default_factory=list)
    draining: bool = False
    update_job: dict[str, Any] = field(default_factory=dict)
    hostname: str = ""
    studio_id: str = ""
    owner_user_id: str = ""
    host: dict[str, Any] = field(default_factory=dict)
    # None：这台 Scout 还没报插件状态。列表只含 class/id/installed/configured。
    plugins: Optional[list[dict[str, Any]]] = None
    plugins_ready: Optional[bool] = None
    tool_tasks_version: int = 0
    plugin_warning: str = ""
    runtime_environment: str = ""
    plugins_reported: threading.Event = field(default_factory=threading.Event, repr=False, compare=False)
    runtime_capabilities: Optional[list[dict[str, Any]]] = None
    node_connection_generation: str = ""
    scout_process_generation: str = ""
    capability_revision: str = ""
    execution_binding_version: int = 0
    binding_unavailable_reason: str = "execution_binding_unsupported"
    plugin_job: dict[str, Any] = field(default_factory=dict)
    # 发一条消息并等应答；由 websocket/node.py 注入
    send: Optional[Callable[..., Any]] = None

    @property
    def alive(self) -> bool:
        return (time.time() - self.last_seen) < min(HEARTBEAT_TIMEOUT_SEC, 2 * P.Registered.heartbeat_interval_sec)

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
        from mino_nexus.core.platform_contract import registration_shadow
        platform_audit = registration_shadow(
            self.host, [asdict(device) for device in self.devices.values()], self.runtime_capabilities)
        platform_audit.update(mode="enforced", activated=True)
        return {
            "platform_contract_version": 1,
            "platform_contract_audit": platform_audit,
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
            "device_workload": [
                {
                    "sn": w.sn,
                    "run_id": w.run_id,
                    "step_idx": w.step_idx,
                    "capability_id": w.capability_id,
                    "lanes_total": w.lanes_total,
                    "lanes_free": w.lanes_free,
                    "lanes_quarantined": w.lanes_quarantined,
                }
                for w in (self.device_workload or [])
            ],
            "executors": {
                k: {"available": v.available, "provides": len(v.provides or []), "reason": v.reason}
                for k, v in sorted(self.executors.items())
            },
            "devices": sorted(self.devices),
            "device_count": len(self.devices),
            "last_seen": self.last_seen,
            "last_seen_ago_sec": round(time.time() - self.last_seen, 1),
            "update_job": dict(self.update_job or {}),
            "host": dict(self.host or {}),
            "plugins": None if self.plugins is None else [dict(row) for row in self.plugins],
            "plugins_ready": self.plugins_ready,
            "tool_tasks_version": self.tool_tasks_version,
            "plugin_warning": self.plugin_warning,
            "runtime_environment": self.runtime_environment,
            "plugin_job": dict(self.plugin_job or {}),
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
            host=dict(self.host or {}),
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
        from mino_nexus.core.platform_contract import validate_platform_announcement
        validate_platform_announcement(reg)
        warnings: list[str] = []
        with self._lock:
            prev = self._nodes.get(reg.node_id)
            if prev is not None:
                SLog.i(TAG, f"节点 {reg.node_id} 重新注册（覆盖上一次会话）")

            accepted_devices = [d for d in reg.devices if _device_identity_valid(d, reg.node_id, reg.host["os"])]
            if len(accepted_devices) != len(reg.devices):
                warnings.append("已拒绝与节点身份或宿主系统不匹配的设备槽")
            incoming = {d.sn: d for d in accepted_devices}

            devices = dict(incoming)
            if prev is not None:
                for sn, old in prev.devices.items():
                    if sn in devices:
                        continue
                    if self._owner.get(sn, reg.node_id) != reg.node_id:
                        continue
                    stale = replace(old, channels={ch: "disconnected" for ch in (old.channels or {})})
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
                platform=reg.host["os"],
                arch=reg.arch,
                scout_version=reg.scout_version,
                executors={e.id: e for e in reg.executors},
                devices=devices,
                send=send,
                hostname=hostname,
                studio_id=studio_id,
                owner_user_id=owner,
                host=dict(getattr(reg, "host", {}) or {}),
                plugins=sanitize_plugins(getattr(reg, "plugins", None)),
                tool_tasks_version=getattr(reg, "tool_tasks_version", 0) if getattr(reg, "tool_tasks_version", 0) in (1, 2) else 0,
                plugins_ready=getattr(reg, "plugins_ready", None) if type(getattr(reg, "plugins_ready", None)) is bool else None,
                plugin_warning=str(getattr(reg, "plugin_warning", "") or "")[:200] if getattr(reg, "plugins_ready", None) is False else "",
                runtime_environment=getattr(reg, "runtime_environment", "") if getattr(reg, "runtime_environment", "") in {"development", "production"} else "",
                runtime_capabilities=getattr(reg, "runtime_capabilities", None),
                node_connection_generation=uuid.uuid4().hex,
                scout_process_generation=reg.scout_process_generation,
            )
            self._update_binding_epoch(session, reg)
            self._nodes[reg.node_id] = session
            if session.plugins:
                session.plugins_reported.set()
            session.persist_snapshot()

            for d in accepted_devices:
                self._place_device(d.sn, reg.node_id, device=d, session=session)
                if d.sn not in session.devices and "部分设备归属未确认或不属于当前账号，已忽略" not in warnings:
                    warnings.append("部分设备归属未确认或不属于当前账号，已忽略")

        avail = session.available_executors()
        SLog.i(
            TAG,
            f"REGISTER node={reg.node_id} scout=v{reg.scout_version} "
            f"executors={avail} devices={sorted(session.devices)}",
        )
        if not avail:
            warnings.append("该节点没有任何可用 executor，无法承接任务")
        return warnings



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
            metadata={k: getattr(device, k) for k in ("os", "session_key", "session_generation", "desktop_state", "blocked_reason", "capabilities", "readiness")},
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
        node = session or self._nodes.get(node_id)
        persisted = get_device(sn)
        if persisted is not None:
            uid = str(persisted.get("owner_user_id") or "")
            if not uid:
                previous_node = stored_node(str(persisted.get("current_scout_id") or "")) or {}
                uid = str(previous_node.get("owner_user_id") or "")
            if node is None or not uid or uid != node.owner_user_id:
                if node is not None:
                    node.devices.pop(sn, None)
                SLog.w(TAG, "忽略未确认或其他账号的设备上报")
                return
        prev = self._owner.get(sn)
        current = prev or str((persisted or {}).get("current_scout_id") or "")
        if current and current != node_id and device is not None and not _channel_connected(device.channels):
            # 设备移到 B 后，A 迟到的 device_lost/离线心跳不能抢回当前位置。
            if node is not None:
                node.devices.pop(sn, None)
            return
        if prev and prev != node_id:
            other = self._nodes.get(prev)
            if other is not None and sn in other.devices:
                other.devices.pop(sn, None)
            SLog.i(TAG, f"设备 {sn} 当前位置 {prev} → {node_id}（账号归属不改）")
        self._owner[sn] = node_id
        node = session or self._nodes.get(node_id)
        if node is not None and device is not None:
            self._record_device(node, device)



    def heartbeat(self, hb: P.Heartbeat, *, send=None) -> None:
        with self._lock:
            node = self._nodes.get(hb.node_id)
            if node is None:
                SLog.w(TAG, f"收到未注册节点的心跳: {hb.node_id}")
                return
            if send is not None and node.send != send:
                return  # A retired connection cannot mutate its replacement.
            if hb.node_connection_generation and hb.node_connection_generation != node.node_connection_generation:
                return
            from mino_nexus.core.platform_contract import validate_platform_announcement
            validate_platform_announcement(hb, fallback_host=node.host)
            if hb.host and hb.host.get("os") != node.host.get("os"):
                raise ValueError("host_os_changed_requires_registration")
            if type(hb.executor_snapshot_version) is not int or hb.executor_snapshot_version != 1 or hb.executors is None:
                raise ValueError("executor_snapshot_version_required")
            node.last_seen = time.time()
            node.busy = hb.busy
            node.active_runs = list(hb.active_runs or [])
            node.device_workload = list(hb.device_workload or [])
            node.executors = {executor.id: executor for executor in hb.executors}
            host = getattr(hb, "host", None)
            if isinstance(host, dict) and host:
                node.host = dict(host)
            node.tool_tasks_version = getattr(hb, "tool_tasks_version", 0) if getattr(hb, "tool_tasks_version", 0) in (1, 2) else 0
            ready = getattr(hb, "plugins_ready", None)
            node.plugins_ready = ready if type(ready) is bool else None
            warning = getattr(hb, "plugin_warning", "")
            node.plugin_warning = str(warning or "")[:200] if ready is False else ""
            environment = getattr(hb, "runtime_environment", "")
            node.runtime_environment = environment if environment in {"development", "production"} else ""
            reported = getattr(hb, "plugins", None)
            if reported is not None:
                node.plugins = sanitize_plugins(reported)
                node.plugins_reported.set()
            caps = getattr(hb, "runtime_capabilities", None)
            if caps is not None:
                node.runtime_capabilities = list(caps)
            self._update_binding_epoch(node, hb)
            ver = str(getattr(hb, "scout_version", "") or "").strip()
            if ver:
                node.scout_version = ver
            for d in hb.device_delta or []:
                if not _device_identity_valid(d, hb.node_id, node.host.get("os", "")):
                    SLog.w(TAG, "拒绝心跳中与节点身份不匹配的设备槽")
                    continue
                if d.sn in node.devices:
                    node.devices[d.sn] = d
                    self._place_device(d.sn, hb.node_id, device=node.devices[d.sn], session=node)
                else:
                    node.devices[d.sn] = d
                    self._place_device(d.sn, hb.node_id, device=d, session=node)
                    SLog.i(TAG, f"心跳里出现设备 {d.sn}，当前节点 {hb.node_id}")
            if node is not None:
                node.persist_snapshot()

    def _update_binding_epoch(self, node, announcement):
        from mino_nexus.core.capability_epoch import capability_revision
        node.execution_binding_version = 0
        node.capability_revision = ""
        reason = "execution_binding_unsupported"
        version = announcement.execution_binding_version
        if type(version) is int and version == 1:
            reason = "capability_snapshot_inconsistent"
            try:
                expected = capability_revision(list(node.executors.values()), node.runtime_capabilities)
                valid = (bool(node.scout_process_generation)
                    and announcement.scout_process_generation == node.scout_process_generation
                    and announcement.capability_revision == expected
                    and (not isinstance(announcement, P.Heartbeat)
                         or announcement.node_connection_generation == node.node_connection_generation
                         and (announcement.executors is None or announcement.executor_snapshot_version == 1)))
                if valid:
                    node.execution_binding_version = 1
                    node.capability_revision = expected
                    reason = ""
            except (ValueError, TypeError, KeyError):
                pass
        node.binding_unavailable_reason = reason

    def execution_snapshot(self, sn: str):
        """One connection/manifest view for the caller's authenticated binding input."""
        with self._lock:
            owner = self._owner.get(sn)
            node = self._nodes.get(owner) if owner else None
            device = node.devices.get(sn) if node else None
            if node is None or device is None:
                return None
            return deepcopy({"node_id": node.node_id, "owner_user_id": node.owner_user_id,
                "online": node.alive and node.send is not None, "device": asdict(device),
                "executors": [asdict(row) for row in node.executors.values()],
                "runtime_capabilities": node.runtime_capabilities,
                "node_connection_generation": node.node_connection_generation,
                "scout_process_generation": node.scout_process_generation,
                "capability_revision": node.capability_revision,
                "execution_binding_version": node.execution_binding_version,
                "binding_unavailable_reason": node.binding_unavailable_reason})

    def note_host_mode(self, node_id: str, mode: str) -> None:
        """休眠/启动的回执先于下一次心跳。立刻改 mode，避免列表还显示旧状态。"""
        nid = str(node_id or "").strip()
        want = "asleep" if str(mode or "") == "asleep" else "running"
        with self._lock:
            node = self._nodes.get(nid)
            if node is None:
                return
            host = dict(node.host or {})
            host["mode"] = want
            if want == "asleep":
                host["inhibit"] = False
            node.host = host
            node.last_seen = time.time()

    def note_plugins(self, node_id: str, plugins: Any) -> None:
        nid = str(node_id or "").strip()
        with self._lock:
            node = self._nodes.get(nid)
            if node is None:
                return
            node.plugins = sanitize_plugins(plugins)
            node.plugins_reported.set()
            node.last_seen = time.time()

    def node_event(self, req: P.Execute, *, node_id: str = "") -> list[str]:
        """处理 S→N 框架 EXECUTE（node.device_lost 等）。`shutting_down` 返回在途 run_id。"""
        params = dict(req.params or {})
        nid = str(node_id or params.get("node_id") or "").strip()
        cap = str(req.capability_id or "")
        event = cap.split(".", 1)[-1] if cap.startswith("node.") else cap
        sn = str(req.sn or req.device_id or params.get("sn") or "").strip()
        detail = str(params.get("detail") or "")
        interrupted: list[str] = []
        inbound: dict[str, Any] | None = None
        with self._lock:
            node = self._nodes.get(nid)
            if node is None:
                return []
            if event == "device_lost" and sn in node.devices:
                for ch in node.devices[sn].channels:
                    node.devices[sn].channels[ch] = "disconnected"
                from mino_nexus.services.run_store import running_run_ids_for_sn

                interrupted.extend(running_run_ids_for_sn(sn))
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
            elif event == "update_progress":
                raw = params.get("progress")
                if isinstance(raw, dict):
                    node.update_job = dict(raw)
                else:
                    node.update_job = {
                        "active": bool(params.get("active", True)),
                        "stage": str(params.get("stage") or ""),
                        "label": str(params.get("label") or detail),
                        "percent": int(params.get("percent") or 0),
                        "layer": str(params.get("layer") or ""),
                        "error": str(params.get("error") or ""),
                    }
                if not node.update_job.get("active"):
                    node.update_job = dict(node.update_job)
            elif event == "plugin_progress":
                raw = params.get("progress")
                if isinstance(raw, dict):
                    node.plugin_job = _public_plugin_job(raw)
                    from mino_nexus.services.node_tools import note_progress
                    note_progress(nid, node.owner_user_id, node.plugin_job)
            elif event in ("plugin_im_message", "plugin_wechat_message"):
                im = params.get("im") if isinstance(params.get("im"), dict) else {}
                if event == "plugin_wechat_message" and not im:
                    im = {
                        "channel": "wechat",
                        "chat_id": str(params.get("from_user_id") or ""),
                        "sender_id": str(params.get("from_user_id") or ""),
                        "text": str(params.get("text") or "")[:2000],
                        "mentioned": True,
                        "chat_type": "private",
                        "msg_id": "",
                        "reply_ctx": {"context_token": str(params.get("context_token") or "")},
                    }
                inbound = dict(im)
                inbound["node_id"] = nid
            if node is not None:
                node.persist_snapshot()
        SLog.i(TAG, f"EXECUTE {cap} node={nid} sn={sn} {detail}")
        if inbound and inbound.get("text"):
            import threading

            from mino_nexus.services.assistant_gateway import handle_inbound

            threading.Thread(
                target=handle_inbound,
                args=(inbound,),
                name="im-inbound",
                daemon=True,
            ).start()
        return interrupted

    def disconnect(self, node_id: str, *, send: Callable[..., Any] | None = None) -> list[str]:
        """连接断了。**不清除设备归属** —— 否则 UI 上的设备会闪现闪灭
        （NODE_REGISTRY.md §2）。只把连通性标为不可用。

        返回断开时仍挂在该节点上的 run_id，调用方应立刻判失败，不要干等超时。
        """
        with self._lock:
            current = self._nodes.get(node_id)
            if send is not None and current is not None and current.send != send:
                return []
            node = self._nodes.pop(node_id, None)
            runs = list(node.active_runs) if node is not None else []
            if node is not None:
                from mino_nexus.services.run_store import running_run_ids_for_sn

                for dev_sn in list(node.devices.keys()):
                    runs.extend(running_run_ids_for_sn(dev_sn))
                runs = list(dict.fromkeys(runs))
                node.persist_snapshot()
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
                        **({k: getattr(d, k) for k in ("os", "session_key", "session_generation", "desktop_state", "blocked_reason", "capabilities", "readiness")} if d else {}),
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
