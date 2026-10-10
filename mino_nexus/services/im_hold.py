"""同一个机器人只允许一台节点持有连接，否则同一条消息会被两台节点各收一次。

规则（只看节点心跳里的快照，不探测）：
  同一渠道（微信 / LangBot 的某个平台）在多台在线节点上都「已启用且持有」时，
  保留 node_id 排序最小的那台，给其余节点下发 im_hold=false。
  被让出的节点不会登出、不会丢配置，只是停止接收；Workbench 可以手动把持有权改回来。
"""
from __future__ import annotations

import threading
from typing import Any

from mino_nexus.core import protocol as P
from mino_nexus.core.log import SLog
from mino_nexus.services.node_registry import get_registry

TAG = "ImHold"
_lock = threading.Lock()
_pinned: dict[str, str] = {}  # 渠道 → 手动指定的持有节点


def _holders() -> dict[str, list[str]]:
    held: dict[str, list[str]] = {}
    for node in get_registry().nodes():
        if not node.alive or node.send is None:
            continue
        for row in node.plugins or []:
            pid = str(row.get("id") or "")
            if pid == "wechat":
                st = row.get("status") or {}
                if st.get("connected") and st.get("holding") is not False:
                    held.setdefault("wechat", []).append(node.node_id)
            elif pid == "langbot":
                for plat, st in (row.get("platforms") or {}).items():
                    if st.get("enabled") and st.get("holding") is not False:
                        held.setdefault(f"langbot:{plat}", []).append(node.node_id)
    return held


def pin(channel: str, node_id: str) -> None:
    with _lock:
        _pinned[channel] = node_id


def _winner(channel: str, nodes: list[str]) -> str:
    want = _pinned.get(channel)
    return want if want in nodes else sorted(nodes)[0]


def push_hold(node_id: str, channel: str, hold: bool) -> None:
    _push(node_id, channel, hold)


def _push(node_id: str, channel: str, hold: bool) -> None:
    node = get_registry().get_node(node_id)
    if node is None or node.send is None:
        return
    if channel == "wechat":
        params = {"class": "bot", "id": "wechat", "im_hold": hold}
    else:
        _, _, plat = channel.partition(":")
        params = {"class": "im", "id": "langbot", "platform": plat, "im_hold": hold}
    from mino_nexus.loop.router_proxy import _run_sync

    async def _once():
        import inspect

        res = node.send(P.MsgType.EXECUTE, P.Execute(
            run_id="", step_idx=-1, sn="", capability_id="node.plugin_config",
            params=params, timeout_sec=30.0), timeout=35.0)
        return await res if inspect.isawaitable(res) else res

    try:
        _run_sync(_once())
        SLog.i(TAG, f"{channel} 持有权：node={node_id} hold={hold}")
    except Exception as exc:
        SLog.w(TAG, f"下发 im_hold 失败 node={node_id} {type(exc).__name__}")


def arbitrate() -> list[tuple[str, str]]:
    """返回本次让出的 (渠道, 节点)。"""
    yielded: list[tuple[str, str]] = []
    with _lock:
        for channel, nodes in _holders().items():
            if len(nodes) < 2:
                continue
            keep = _winner(channel, nodes)
            for nid in nodes:
                if nid != keep:
                    yielded.append((channel, nid))
    for channel, nid in yielded:
        _push(nid, channel, False)
    return yielded
