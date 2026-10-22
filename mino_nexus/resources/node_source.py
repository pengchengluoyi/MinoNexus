"""入站消息的来源校验：节点归属、渠道与插件快照。"""
from __future__ import annotations

from typing import Any


def source_validation(inbound: dict[str, Any]) -> tuple[str, str]:
    from mino_nexus.resources.node_registry import get_registry
    from mino_nexus.resources.access_scope import node_owner_id
    node_id = str(inbound.get("node_id") or "")
    node = get_registry().get_node(node_id)
    channel = str(inbound.get("channel") or "")
    instance = str(inbound.get("instance_id") or "")
    if node is None or not node.alive:
        return "", "node_unavailable"
    if not instance:
        return "", "instance_missing"
    if not node.owner_user_id:
        return "", "owner_missing"
    if node_owner_id(node_id) != node.owner_user_id:
        return "", "owner_mismatch"
    platform = {"feishu": "lark", "wecom": "wecombot", "dingtalk": "dingtalk"}.get(channel)
    if channel not in ("wechat", "feishu_cli") and not platform:
        return "", "channel_unsupported"
    if not node.plugins and not node.plugins_reported.is_set():
        return "", "plugin_snapshot_pending"
    for row in node.plugins or []:
        if channel == "feishu_cli" and row.get("class") == "cli" and row.get("id") == "feishu":
            status = row.get("status") or {}
            if not status.get("enabled") or (inbound.get("reply_ctx") or {}).get("transport") != "feishu_cli":
                return "", "channel_disabled"
        elif channel == "wechat" and row.get("id") == "wechat":
            status = row.get("status") or {}
        elif platform and row.get("id") == "langbot":
            status = (row.get("platforms") or {}).get(platform) or {}
            ctx = inbound.get("reply_ctx") or {}
            if not isinstance(ctx, dict) or ctx.get("bot_uuid") != instance:
                return "", "reply_instance_mismatch"
        else:
            continue
        if status.get("instance_id") != instance:
            return "", "instance_mismatch"
        if status.get("holding") is False:
            return "", "channel_yielded"
        if channel == "wechat" and not status.get("connected"):
            return "", "channel_disconnected"
        if platform and not status.get("enabled"):
            return "", "channel_disabled"
        return str(node.owner_user_id), ""
    return "", "channel_unreported"
