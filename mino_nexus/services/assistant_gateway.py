"""IM 入站：去重、绑定、群聊 @、限流，然后交给助手。"""
from __future__ import annotations

import time
from typing import Any

from mino_nexus.core.log import SLog
from mino_nexus.services import assistant_store as store
from mino_nexus.services.assistant_agent import handle_utterance

TAG = "AssistantGateway"
_WINDOW = 60
_LIMIT = 20
_hits: dict[str, list[float]] = {}


def _limited(user_id: str) -> bool:
    now = time.time()
    rows = [t for t in _hits.get(user_id, []) if now - t < _WINDOW]
    if len(rows) >= _LIMIT:
        _hits[user_id] = rows
        return True
    rows.append(now)
    _hits[user_id] = rows
    return False


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
        )
        if not uid:
            return "绑定码无效或已过期。请在 Workbench 的助手页重新生成。"
        if user_id and user_id == uid:
            return "你已经绑定过了，不用再绑定，直接说事就行。"
        return "已绑定。之后我会按你的账号查询和执行。"
    if raw in ("/状态", "/status"):
        profile = store.get_profile(user_id)
        app_id = profile.get("default_app_id") or "未设置"
        sn = profile.get("default_sn") or "未设置"
        return f"助手已绑定。默认应用 {app_id}，默认设备 {sn}。"
    return ""


def _auto_bind_owner(inbound: dict[str, Any], sender: str) -> str | None:
    """微信：发消息的人就是这台节点扫码登录的账号时，自动绑到节点所有者，不用再输绑定码。
    只认节点上报的 account_id（来自扫码确认），别人给机器人发消息不会被当成所有者。"""
    from mino_nexus.services.node_registry import get_registry

    node = get_registry().get_node(str(inbound.get("node_id") or ""))
    if node is None or not str(node.owner_user_id or "").strip():
        return None
    for row in node.plugins or []:
        if row.get("id") == "wechat" and (row.get("status") or {}).get("account_id") == sender:
            return store.bind_identity(
                node.owner_user_id, channel="wechat", tenant="", sender_id=sender,
            )
    return None


def handle_inbound(inbound: dict[str, Any]) -> None:
    channel = str(inbound.get("channel") or "")
    chat_id = str(inbound.get("chat_id") or "")
    sender = str(inbound.get("sender_id") or "")
    text = str(inbound.get("text") or "").strip()
    msg_id = str(inbound.get("msg_id") or "")
    if not text or not sender:
        return
    if str(inbound.get("chat_type") or "") == "group" and not inbound.get("mentioned"):
        return
    key = f"{channel}:{msg_id}" if msg_id else ""
    if key and store.msg_seen(key):
        return
    user_id = store.find_identity(channel, str(inbound.get("tenant") or ""), sender)
    if not user_id and channel == "wechat":
        user_id = _auto_bind_owner(inbound, sender)
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
    started = time.time()
    try:
        result = handle_utterance(
            user_id=user_id,
            text=text,
            recent_turns=inbound.get("recent_turns") if isinstance(inbound.get("recent_turns"), list) else [],
            channel=channel,
            chat_id=chat_id,
            node_id=str(inbound.get("node_id") or ""),
            reply_ctx=inbound.get("reply_ctx") if isinstance(inbound.get("reply_ctx"), dict) else {},
        )
    except Exception as exc:
        SLog.w(TAG, f"agent failed: {type(exc).__name__}: {exc}")
        _send(inbound, "处理失败，请稍后再试。")
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
        "status": (result.get("card") or {}).get("status") or "ok",
        "intent": result.get("intent") or "",
        "elapsed_ms": int((time.time() - started) * 1000),
        "steps": result.get("steps") or [],
        "card": result.get("card") or {},
        "prompt_tokens": usage.get("prompt_tokens") or 0,
        "completion_tokens": usage.get("completion_tokens") or 0,
    })
    _send(inbound, str(result.get("text") or ""))
