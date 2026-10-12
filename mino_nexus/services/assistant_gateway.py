"""IM 入站：去重、绑定、群聊 @、限流，然后交给助手。"""
from __future__ import annotations

import time
import hashlib
import json
import threading
from mino_nexus.core.execution_user import execution_user
from typing import Any

from mino_nexus.core.log import SLog
from mino_nexus.services import assistant_store as store
from mino_nexus.services.assistant_agent import handle_utterance

TAG = "AssistantGateway"
_WINDOW = 60
_LIMIT = 20
_hits: dict[str, list[float]] = {}
_hit_lock = threading.Lock()


def _limited_unlocked(user_id: str) -> bool:
    now = time.time()
    rows = [t for t in _hits.get(user_id, []) if now - t < _WINDOW]
    if len(rows) >= _LIMIT:
        _hits[user_id] = rows
        return True
    rows.append(now)
    _hits[user_id] = rows
    return False


def _limited(user_id: str) -> bool:
    with _hit_lock:
        return _limited_unlocked(user_id)


def validated_source(inbound: dict[str, Any]) -> str:
    """来源已由 Scout WS 认证；再校验节点归属与它实际持有的机器人实例。"""
    from mino_nexus.services.node_registry import get_registry
    from mino_nexus.services.access_scope import node_owner_id
    node_id = str(inbound.get("node_id") or "")
    node = get_registry().get_node(node_id)
    channel = str(inbound.get("channel") or "")
    instance = str(inbound.get("instance_id") or "")
    if node is None or not node.alive or not instance or not node.owner_user_id:
        return ""
    if node_owner_id(node_id) != node.owner_user_id:
        return ""
    platform = {"feishu": "lark", "wecom": "wecombot", "dingtalk": "dingtalk"}.get(channel)
    for row in node.plugins or []:
        if channel == "wechat" and row.get("id") == "wechat":
            status = row.get("status") or {}
            if status.get("connected") and status.get("holding") is not False and status.get("instance_id") == instance:
                return str(node.owner_user_id)
        if platform and row.get("id") == "langbot":
            status = (row.get("platforms") or {}).get(platform) or {}
            ctx = inbound.get("reply_ctx") or {}
            if status.get("enabled") and status.get("holding") is not False and status.get("instance_id") == instance and ctx.get("bot_uuid") == instance:
                return str(node.owner_user_id)
    return ""


def send_to_chat(*, channel: str, node_id: str, chat_id: str, sender_id: str, reply_ctx: dict[str, Any], text: str) -> None:
    """按渠道把一段文字发回会话。微信走 iLink 插件，飞书 / 企微 / 钉钉经 LangBot。
    reply_ctx 原样带回：Scout 靠它知道往哪发（微信的 context_token、LangBot 的 bot_uuid / target）。"""
    if not text:
        return
    from mino_nexus.services.node_plugin_call import call_plugin

    kind, plugin_id = ("bot", "wechat") if channel == "wechat" else ("im", "langbot")
    call_plugin(
        kind=kind,
        plugin_id=plugin_id,
        node_id=node_id,
        capability_id="plugin.im.send",
        params={
            "channel": channel,
            "chat_id": chat_id,
            "sender_id": sender_id,
            "reply_ctx": reply_ctx if isinstance(reply_ctx, dict) else {},
            "text": text[:1800],
        },
        timeout=30,
    )


def _send(inbound: dict[str, Any], text: str) -> None:
    try:
        send_to_chat(
            channel=str(inbound.get("channel") or ""),
            node_id=str(inbound.get("node_id") or ""),
            chat_id=str(inbound.get("chat_id") or ""),
            sender_id=str(inbound.get("sender_id") or ""),
            reply_ctx=inbound.get("reply_ctx") if isinstance(inbound.get("reply_ctx"), dict) else {},
            text=text,
        )
    except Exception as exc:
        SLog.w(TAG, f"send failed: {type(exc).__name__}")


def _shortcut(inbound: dict[str, Any], user_id: str, text: str) -> str:
    raw = text.strip()
    if raw in ("/帮助", "/help"):
        return "可以说：有哪些项目、设备在不在线、最近任务、在某台设备上跑用例。绑定：先在 Workbench 生成绑定码，再发 /绑定 和那 6 位数字。"
    if raw.startswith("/绑定"):
        code = raw.replace("/绑定", "", 1).strip()
        uid = store.consume_bind_code(
            code,
            channel=str(inbound.get("channel") or ""),
            tenant=str(inbound.get("tenant") or ""),
            sender_id=str(inbound.get("sender_id") or ""),
            instance_id=str(inbound.get("instance_id") or ""),
            node_id=str(inbound.get("node_id") or ""),
        )
        if not uid:
            return "绑定码无效或已过期。请在 Workbench 的助手页重新生成。"
        if user_id and user_id == uid:
            return "你已经绑定过了，不用再绑定，直接说事就行。"
        return "已绑定。之后我会按你的账号查询和执行。"
    if raw in ("/状态", "/status") and user_id:
        profile = store.get_profile(user_id)
        return "助手已绑定，当前已启用。" if profile["enabled"] else "助手已绑定，当前已停用。"
    return ""


def handle_inbound(inbound: dict[str, Any]) -> None:
    owner = validated_source(inbound)
    if not owner:
        SLog.w(TAG, "拒绝来源或归属未经确认的 IM 回调")
        return
    inbound = dict(inbound)
    inbound["reply_ctx"] = {**(inbound.get("reply_ctx") or {}), "instance_id": inbound["instance_id"], "tenant": str(inbound.get("tenant") or ""), "sender_id": str(inbound.get("sender_id") or "")}
    with execution_user(owner):
        _handle_owned_inbound(inbound, owner)


def confirmed_history(inbound: dict[str, Any], binding: dict[str, Any], *,
                      limit: int = 8, text_limit: int = 500) -> list[dict[str, Any]]:
    """仅接受当前绑定之后、同实例/租户/实际发送者的本地上下文。"""
    out = []
    rows = inbound.get("recent_turns")
    for row in rows[-max(1, min(limit, 100)):] if isinstance(rows, list) else []:
        if not isinstance(row, dict) or row.get("role") not in ("user", "assistant"):
            continue
        if any(str(row.get(k) or "") != str(inbound.get(k) or "")
               for k in ("instance_id", "tenant", "sender_id")):
            continue
        try:
            ts = int(row.get("ts") or 0)
        except (TypeError, ValueError):
            continue
        if not int(binding.get("bound_at") or 0) < ts <= int(time.time()):
            continue
        out.append({"role": row["role"], "text": str(row.get("text") or "")[:text_limit], "ts": ts})
    return out


def _handle_owned_inbound(inbound: dict[str, Any], owner: str) -> None:
    channel = str(inbound.get("channel") or "")
    chat_id = str(inbound.get("chat_id") or "")
    sender = str(inbound.get("sender_id") or "")
    text = str(inbound.get("text") or "").strip()
    msg_id = str(inbound.get("msg_id") or "")
    if not text or not sender:
        return
    if str(inbound.get("chat_type") or "") == "group" and not inbound.get("mentioned"):
        return
    if not msg_id:
        return  # 无可靠消息标识不能执行。
    identity_parts = [owner, str(inbound.get("node_id") or ""), channel,
                      str(inbound.get("instance_id") or ""), str(inbound.get("tenant") or ""), sender, msg_id]
    key = hashlib.sha256(json.dumps(identity_parts, ensure_ascii=False).encode()).hexdigest()
    if not store.claim_inbound(key, owner):
        return
    user_id = store.find_identity(channel, str(inbound.get("tenant") or ""), sender,
        instance_id=str(inbound.get("instance_id") or ""), node_id=str(inbound.get("node_id") or ""))
    if user_id and user_id != owner:
        return
    if raw := _shortcut(inbound, user_id or "", text):
        if user_id or raw.startswith(("已绑定", "你已经绑定")) or text.startswith("/帮助") or text.startswith("/help"):
            _send(inbound, raw)
        else:
            _send(inbound, "还没有绑定。请打开 Workbench 的助手页生成绑定码，然后发 /绑定 和那 6 位数字。")
        return
    if not user_id:
        _send(inbound, "还没有绑定。请打开 Workbench 的助手页生成绑定码，然后发 /绑定 和那 6 位数字。")
        return
    if _limited(user_id):
        _send(inbound, "这一分钟的消息太多了，请稍后再发。")
        return
    SLog.i(TAG, f"inbound channel={channel} chat={chat_id} len={len(text)}")
    binding = store.find_route_identity(channel, str(inbound.get("tenant") or ""), sender,
        instance_id=str(inbound.get("instance_id") or ""), node_id=str(inbound.get("node_id") or ""))
    if not binding or binding["user_id"] != user_id:
        return
    inbound["reply_ctx"]["binding_key"] = binding["binding_key"]
    started = time.time()
    try:
        store.require_im_binding(user_id, channel, str(inbound.get("node_id") or ""), inbound["reply_ctx"])
        result = handle_utterance(
            user_id=user_id,
            text=text,
            recent_turns=confirmed_history(inbound, binding),
            channel=channel,
            chat_id=chat_id,
            node_id=str(inbound.get("node_id") or ""),
            reply_ctx=inbound.get("reply_ctx") if isinstance(inbound.get("reply_ctx"), dict) else {},
        )
    except Exception as exc:
        SLog.w(TAG, f"agent failed: {type(exc).__name__}")
        try:
            store.require_im_binding(user_id, channel, str(inbound.get("node_id") or ""), inbound["reply_ctx"])
            _send(inbound, "处理失败，请稍后再试。")
        except PermissionError:
            pass
        return
    usage = result.get("usage") or {}
    store.save_turn({
        "turn_id": store.new_turn_id(),
        "user_id": user_id,
        "surface": "im",
        "channel": channel,
        "chat_id": chat_id,
        "node_id": str(inbound.get("node_id") or ""),
        "msg_key": key,
        "detail": {"_im_route": {k: inbound["reply_ctx"].get(k, "") for k in
            ("instance_id", "tenant", "sender_id", "binding_key", "bot_uuid")}},
        "status": (result.get("card") or {}).get("status") or "ok",
        "intent": result.get("intent") or "",
        "elapsed_ms": int((time.time() - started) * 1000),
        "steps": result.get("steps") or [],
        "card": result.get("card") or {},
        "prompt_tokens": usage.get("prompt_tokens") or 0,
        "completion_tokens": usage.get("completion_tokens") or 0,
    })
    try:
        store.require_im_binding(user_id, channel, str(inbound.get("node_id") or ""), inbound["reply_ctx"])
    except PermissionError:
        return
    _send(inbound, str(result.get("text") or ""))
