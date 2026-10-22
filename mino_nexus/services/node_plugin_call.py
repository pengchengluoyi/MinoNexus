"""把具名插件调用发到持有密钥的那台 Scout。这里不保存密钥。"""
from __future__ import annotations

import inspect
from typing import Any

from mino_nexus.core import protocol as P
from mino_nexus.core.wire_identity import assign_execute_identity
from mino_nexus.loop.router_proxy import _run_sync
from mino_nexus.services.node_registry import get_registry

_ALLOWED = frozenset({
    "plugin.cli.feishu",
    "plugin.cli.meego",
    "plugin.bot.send",
    "plugin.bot.wechat",
    "plugin.gmail.fetch_otp",
    "plugin.im.send",
    "plugin.im.stream",
    "plugin.im.history",
    "plugin.im.ack",
    "plugin.im.attachment",
    "plugin.im.upload_image",
})

_HIDDEN = ("secret", "password", "webhook", "bot_token", "app_secret", "plugin_secret")


class PluginCallError(Exception):
    pass


class PluginCapabilityError(PluginCallError):
    pass


def node_for_plugin(kind: str, plugin_id: str, node_id: str = ""):
    from mino_nexus.core.execution_user import require_user_id
    uid = require_user_id()
    reg = get_registry()
    from mino_nexus.services.execution_dependencies import plugin_fact
    spec = {"kind": "plugin", "class": kind, "plugin_id": plugin_id}
    if node_id:
        spec["node_id"] = str(node_id).strip()
    result = plugin_fact(reg.nodes(), spec, uid)
    if result["status"] != "ready":
        messages = {
            "plugin_node_unavailable": "没有这个节点",
            "plugin_node_offline": "节点离线，无法调用插件",
            "plugin_node_ambiguous": "有多台节点配了这个插件，请指定 node_id",
            "plugin_manifest_unreported": "节点未上报插件配置，请等待上报或升级 Scout",
            "plugin_not_configured": "指定插件尚未配置完成",
        }
        raise PluginCallError(messages.get(result["reason_code"], "插件依赖不可用"))
    node = reg.get_node(result["node_id"])
    if (node is None or node.owner_user_id != uid or not node.alive or node.send is None
            or node.node_connection_generation != result["node_connection_generation"]
            or node.scout_process_generation != result["scout_process_generation"]):
        raise PluginCallError("节点连接已变化，请重试")
    return node


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
    if capability_id == 'plugin.cli.meego' and (params.get('attachments') or params.get('attachment_ref')):
        if not any(row.get('class') == 'cli' and row.get('id') == 'meego' and row.get('attachment_upload_version') == 1
                   for row in node.plugins or []):
            raise PluginCapabilityError('当前 Scout 尚未声明受控附件上传能力，请更新 Scout 并等待重新上报；本次没有上传或创建工作项。')
    payload = P.Execute(
        run_id="",
        step_idx=-1,
        sn="",
        capability_id=capability_id,
        params=dict(params or {}),
        timeout_sec=float(timeout),
    )

    assign_execute_identity(payload)

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
