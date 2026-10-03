"""把具名插件调用发到持有密钥的那台 Scout。这里不保存密钥。"""
from __future__ import annotations

import inspect
from typing import Any

from mino_nexus.core import protocol as P
from mino_nexus.loop.router_proxy import _run_sync
from mino_nexus.services.node_registry import get_registry

_ALLOWED = frozenset({
    "plugin.cli.feishu",
    "plugin.cli.meego",
    "plugin.bot.send",
    "plugin.bot.wechat",
    "plugin.gmail.fetch_otp",
})

_HIDDEN = ("secret", "password", "webhook", "bot_token", "app_secret", "plugin_secret")


class PluginCallError(Exception):
    pass


def node_for_plugin(kind: str, plugin_id: str, node_id: str = ""):
    reg = get_registry()
    want = str(node_id or "").strip()
    if want:
        node = reg.get_node(want)
        if node is None or not node.alive or node.send is None:
            raise PluginCallError("节点离线，无法调用插件")
        return node
    hits = []
    for node in reg.nodes():
        if not node.alive or node.send is None or node.plugins is None:
            continue
        for row in node.plugins:
            if (
                row.get("class") == kind
                and row.get("id") == plugin_id
                and row.get("configured")
            ):
                hits.append(node)
                break
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise PluginCallError(f"没有 Scout 节点配好 {kind}/{plugin_id}")
    raise PluginCallError("有多台节点配了这个插件，请指定 node_id")


def call_plugin(
    *,
    kind: str,
    plugin_id: str,
    capability_id: str,
    params: dict[str, Any],
    node_id: str = "",
    timeout: float = 60,
) -> dict[str, Any]:
    if capability_id not in _ALLOWED:
        raise PluginCallError(f"不允许的插件能力 {capability_id}")
    node = node_for_plugin(kind, plugin_id, node_id)
    payload = P.Execute(
        run_id="",
        step_idx=-1,
        sn="",
        capability_id=capability_id,
        params=dict(params or {}),
        timeout_sec=float(timeout),
    )

    async def _once():
        result = node.send(P.MsgType.EXECUTE, payload, timeout=float(timeout) + 5)
        if inspect.isawaitable(result):
            result = await result
        return result

    result = _run_sync(_once())
    if result is None:
        raise PluginCallError("节点未应答")
    status = getattr(getattr(result, "status", None), "value", None) or getattr(result, "status", "")
    summary = str(getattr(result, "summary", "") or "")
    error = str(getattr(result, "error", "") or "")
    if str(status).lower() not in ("pass", "passed", "ok", "success"):
        raise PluginCallError(error or summary or "插件调用失败")
    data = dict(getattr(result, "data", None) or {})
    return _safe(data)


def _safe(data: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in data.items():
        name = str(key)
        if any(flag in name for flag in _HIDDEN):
            continue
        out[name] = value
    return out
