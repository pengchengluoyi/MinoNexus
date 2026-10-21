"""IM 入站：去重、绑定、群聊 @、限流，然后交给助手。"""
from __future__ import annotations

import time
import re
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
_SOURCE_SNAPSHOT_WAIT_SEC = 20.0
_hits: dict[str, list[float]] = {}
_hit_lock = threading.Lock()


def _failure_diagnostic(exc: Exception, stage: str) -> dict[str, Any]:
    """只记录固定原因码和代码位置，不记录异常正文、聊天或凭据。"""
    reasons = {
        "缺少执行账号，已拒绝执行": "execution_user_missing",
        "执行账号不匹配": "execution_user_mismatch",
        "会话已被新消息打断，旧解析结果不再执行": "generation_superseded",
        "入站处理租约已失效，禁止继续写操作": "inbound_lease_lost",
        "群策略已撤销": "group_policy_revoked",
    }
    reason = "internal_error"
    if isinstance(exc, store.ImBindingError):
        reason = exc.reason
    elif isinstance(exc, PermissionError):
        reason = reasons.get(str(exc), "permission_denied")
    sites = []
    tb = exc.__traceback__
    while tb is not None:
        frame = tb.tb_frame
        sites.append(f"{frame.f_globals.get('__name__', '').rsplit('.', 1)[-1]}.{frame.f_code.co_name}:{tb.tb_lineno}")
        tb = tb.tb_next
    return {"error_type": type(exc).__name__, "error_reason": reason,
            "error_stage": stage, "error_site": " > ".join(sites[-3:])}


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
    return _source_validation(inbound)[0]


def _source_validation(inbound: dict[str, Any]) -> tuple[str, str]:
    from mino_nexus.services.node_registry import get_registry
    from mino_nexus.services.access_scope import node_owner_id
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


def _bind_authorized_owner(inbound: dict[str, Any], owner: str) -> str:
    """Only the verified channel login user inherits the authenticated node owner."""
    channel = inbound.get("channel")
    identity_fields = {
        "feishu_cli": ("cli", "feishu", "authorized_sender_id"),
        "wechat": ("bot", "wechat", "account_id"),
    }
    if channel not in identity_fields or inbound.get("chat_type") != "private":
        return ""
    if validated_source(inbound) != owner:
        return ""
    from mino_nexus.services.node_registry import get_registry
    node = get_registry().get_node(str(inbound.get("node_id") or ""))
    if node is None:
        return ""
    kind, plugin_id, identity_field = identity_fields[channel]
    for plugin in node.plugins or []:
        if plugin.get("class") != kind or plugin.get("id") != plugin_id:
            continue
        status = plugin.get("status") or {}
        sender = status.get(identity_field)
        if (not sender or sender != inbound.get("sender_id")
                or status.get("instance_id") != inbound.get("instance_id")):
            return ""
        return store.bind_identity(owner, channel=channel, tenant=str(inbound.get("tenant") or ""),
            sender_id=sender, instance_id=inbound["instance_id"], node_id=inbound["node_id"])
    return ""


def send_to_chat(*, channel: str, node_id: str, chat_id: str, sender_id: str, reply_ctx: dict[str, Any], text: str, delivery_id: str = "", card: dict | None = None) -> dict:
    """按渠道把一段文字发回会话。微信走 iLink 插件，飞书 / 企微 / 钉钉经 LangBot。
    reply_ctx 原样带回：Scout 靠它知道往哪发（微信的 context_token、LangBot 的 bot_uuid / target）。"""
    if not text:
        return
    from mino_nexus.services.node_plugin_call import call_plugin

    kind, plugin_id = ("cli", "feishu") if channel == "feishu_cli" else (("bot", "wechat") if channel == "wechat" else ("im", "langbot"))
    return call_plugin(
        kind=kind,
        plugin_id=plugin_id,
        node_id=node_id,
        capability_id="plugin.im.send",
        params={
            "channel": channel,
            "chat_id": chat_id,
            "sender_id": sender_id,
            "reply_ctx": reply_ctx if isinstance(reply_ctx, dict) else {},
            "text": text,
            "delivery_id": delivery_id,
            "card": card or {},
        },
        timeout=30,
    )


def _send(inbound: dict[str, Any], text: str, card: dict | None = None, stream=None, *, turn_id: str = "") -> None:
    from mino_nexus.services.assistant_cards import split_text
    from mino_nexus.services.assistant_envelope import capabilities
    from mino_nexus.services import assistant_conversation as conversation
    from mino_nexus.ai.dispatch_log import ctx
    capability = capabilities(inbound.get("capabilities"))
    if card and card.get("card_type") == "clarification" and (card.get("request") or {}).get("id"):
        text += "\n\n请求编号：#" + card["request"]["id"][-8:]
    origin = conversation.origin_for(str(inbound.get("channel") or ""), str(inbound.get("chat_id") or ""),
                                     str(inbound.get("node_id") or ""), inbound.get("reply_ctx") or {})
    request = (card or {}).get("request") or {}
    if card and inbound.get("chat_type") == "group":
        policy = conversation.group_policy(origin)
        if not policy.get("enabled"):
            raise PermissionError("群策略已撤销")
        if card.get("card_type") == "notification" and not policy.get("disclose_results"):
            text = "本次请求状态：" + str((card.get("header") or {}).get("title") or "已处理") + "。详细结果请在私聊或 Workbench 查看。"
            card = None
    segments = split_text(text, capability["max_characters"], capability["length_unit"])
    if turn_id:
        store.record_turn_delivery(turn_id, text, "prepared")
    for index, segment in enumerate(segments):
        scope = str(ctx().get("assistant_inbound_key") or inbound.get("msg_id") or "")
        delivery_id = hashlib.sha256(json.dumps([origin, scope, index, segment], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        try:
            result = stream.finish(segment, card) if stream is not None and index == 0 else None
            if result is None:
                result = send_to_chat(channel=origin["channel"], node_id=origin["node_id"], chat_id=origin["chat_id"],
                sender_id=origin["sender_id"], reply_ctx=inbound.get("reply_ctx") or {}, text=segment,
                delivery_id=delivery_id, card=card if index == 0 else None)
        except Exception:
            if turn_id:
                store.record_turn_delivery(turn_id, text, "partially_sent" if index else "failed", sent_segments=index,
                                           sent_text="\n\n".join(segments[:index]))
            raise
        if turn_id:
            store.record_turn_delivery(turn_id, text, "sent" if index + 1 == len(segments) else "partially_sent", sent_segments=index + 1,
                                       sent_text="\n\n".join(segments[:index + 1]))
        if request:
            conversation.reference(origin, request.get("id", ""), int(request.get("revision") or 0),
                                   message_id=delivery_id, provider_id=str(result.get("provider_message_id") or delivery_id))


def _ack(inbound: dict[str, Any]) -> bool:
    if not inbound.get("delivery_key"):
        return True
    from mino_nexus.services.node_plugin_call import call_plugin
    channel = str(inbound.get("channel") or "")
    try:
        call_plugin(kind="cli" if channel == "feishu_cli" else ("bot" if channel == "wechat" else "im"), plugin_id="feishu" if channel == "feishu_cli" else ("wechat" if channel == "wechat" else "langbot"),
                    node_id=str(inbound.get("node_id") or ""), capability_id="plugin.im.ack", params={
                        "channel": channel, "chat_id": inbound.get("chat_id"), "sender_id": inbound.get("sender_id"),
                        "reply_ctx": inbound.get("reply_ctx") or {}, "delivery_key": inbound["delivery_key"]}, timeout=10)
    except Exception as exc:
        # ACK 失败不代表消息业务处理失败。保留 done/uncertain，重投只重试 ACK。
        reason = str(exc) if str(exc) in {"request_key_conflict", "request_inflight"} else type(exc).__name__
        SLog.w(TAG, f"入站确认失败，等待渠道重投后重试 ACK：{reason}")
        return False
    return True


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
            return "绑定码无效或已过期。请在 Workbench 的 Scout 节点对应渠道配置中重新生成。"
        if user_id and user_id == uid:
            return "你已经绑定过了，不用再绑定，直接说事就行。"
        return "已绑定。之后我会按你的账号查询和执行。"
    if raw in ("/状态", "/status") and user_id:
        profile = store.get_profile(user_id)
        return "助手已绑定，当前已启用。" if profile["enabled"] else "助手已绑定，当前已停用。"
    return ""


def handle_inbound(inbound: dict[str, Any]) -> None:
    owner, reason = _source_validation(inbound)
    if reason == "plugin_snapshot_pending":
        from mino_nexus.services.node_registry import get_registry

        registry = get_registry()
        node_id = str(inbound.get("node_id") or "")
        node = registry.get_node(node_id)
        if node is not None:
            SLog.i(TAG, f"等待已认证节点的首次插件快照 node={node_id}")
            node.plugins_reported.wait(_SOURCE_SNAPSHOT_WAIT_SEC)
            if registry.get_node(node_id) is node:
                owner, reason = _source_validation(inbound)
                if reason == "plugin_snapshot_pending":
                    reason = "plugin_snapshot_timeout"
            else:
                owner, reason = "", "node_session_changed"
    if not owner:
        SLog.w(TAG, f"拒绝来源或归属未经确认的 IM 回调 reason={reason} node={str(inbound.get('node_id') or '')}")
        return
    from mino_nexus.services.assistant_envelope import normalize
    try:
        inbound = normalize(inbound)
    except (ValueError, TypeError):
        SLog.w(TAG, "拒绝格式不完整的 IM 事件")
        return
    inbound["reply_ctx"] = {**(inbound.get("reply_ctx") or {}), "instance_id": inbound["instance_id"], "tenant": str(inbound.get("tenant") or ""), "sender_id": str(inbound.get("sender_id") or ""), "source_message_id": inbound.get("msg_id", ""), "thread_id": inbound.get("thread_id", ""), "chat_type": inbound.get("chat_type", "private")}
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
               for k in ("instance_id", "tenant", "sender_id", "thread_id")):
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
    if not sender:
        return
    if str(inbound.get("chat_type") or "") == "group" and not inbound.get("mentioned"):
        return
    if not msg_id:
        return  # 无可靠消息标识不能执行。
    identity_parts = [owner, channel, str(inbound.get("instance_id") or ""), str(inbound.get("tenant") or ""), chat_id, sender, msg_id, str(inbound.get("event_type") or "message"), str(inbound.get("revision") or "")]
    key = hashlib.sha256(json.dumps(identity_parts, ensure_ascii=False).encode()).hexdigest()
    # 兼容旧永久 claim，不能在升级后重放已处理消息。
    legacy = [owner, str(inbound.get("node_id") or ""), channel, str(inbound.get("instance_id") or ""), str(inbound.get("tenant") or ""), sender, msg_id]
    if store.legacy_inbound_claimed(hashlib.sha256(json.dumps(legacy, ensure_ascii=False).encode()).hexdigest()):
        return
    from mino_nexus.services import assistant_receipts as receipts
    from mino_nexus.ai import dispatch_log
    token = receipts.acquire(key, kind="inbound", lease_seconds=3600)
    if not token:
        previous = receipts.get(key) or {}
        if previous.get("state") in ("done", "uncertain", "rejected"):
            _ack(inbound)
        return
    bound = dispatch_log.bind(assistant_inbound_key=key, assistant_inbound_token=token, assistant_mutation_scope=key)
    try:
        _process_claimed_inbound(inbound, owner, key)
    except Exception as exc:
        receipts.finish(key, token, "retryable")
        diagnostic = _failure_diagnostic(exc, "inbound")
        SLog.w(TAG, f"入站处理失败，可恢复状态已记录：{diagnostic['error_type']} reason={diagnostic['error_reason']} site={diagnostic['error_site']}")
    else:
        receipts.finish(key, token, "done")
        _ack(inbound)
    finally:
        dispatch_log.reset(bound)


def _process_claimed_inbound(inbound: dict[str, Any], owner: str, key: str) -> None:
    channel, chat_id, sender = (str(inbound.get(k) or "") for k in ("channel", "chat_id", "sender_id"))
    text = str(inbound.get("text") or "").strip()
    user_id = store.find_identity(channel, str(inbound.get("tenant") or ""), sender,
        instance_id=str(inbound.get("instance_id") or ""), node_id=str(inbound.get("node_id") or ""))
    if not user_id:
        user_id = _bind_authorized_owner(inbound, owner)
    if user_id and user_id != owner:
        return
    if inbound.get("chat_type") == "group" and text.startswith("/绑定"):
        _send(inbound, "请在私聊中绑定账号。")
        return
    if raw := _shortcut(inbound, user_id or "", text):
        # Shortcut replies are safe before binding. Preserve an invalid/expired
        # code diagnostic instead of hiding it behind the generic bind prompt.
        _send(inbound, raw)
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
    from mino_nexus.services import assistant_conversation as conversation
    origin = conversation.origin_for(channel, chat_id, str(inbound.get("node_id") or ""), inbound["reply_ctx"])
    group = conversation.group_policy(origin) if inbound.get("chat_type") == "group" else None
    if group is not None and not group.get("enabled"):
        if re.search(r"(平台|Mino|Workbench).*(怎么用|如何使用|有什么功能)", text, re.I):
            from mino_nexus.services.assistant_runtime import route_control
            help_result = route_control(text, origin, inbound)
            if help_result:
                _send(inbound, help_result['text'], help_result['card'])
                return
        _send(inbound, "当前群提供平台使用帮助；查询或执行账号任务请转到私聊，或由账号主人在 Workbench 设置群策略。")
        return
    generation = conversation.begin(origin)
    from mino_nexus.ai import dispatch_log
    from mino_nexus.services.assistant_stream import DraftStream
    stream = DraftStream(inbound, origin, generation, group)
    context_token = dispatch_log.bind(assistant_origin=origin, assistant_generation=generation, assistant_group_policy=group,
                                      assistant_stream_callback=stream.accept if stream.enabled else None)
    started = time.time()
    turn_id = store.new_turn_id()
    turn_base = {"turn_id": turn_id, "user_id": user_id, "surface": "im", "channel": channel,
                 "chat_id": chat_id, "node_id": str(inbound.get("node_id") or ""), "msg_key": key,
                 "user_text": str(inbound.get("text") or ""),
                 "created_at": int(started)}
    route_detail = {"_im_route": {name: inbound["reply_ctx"].get(name, "") for name in
                    ("instance_id", "tenant", "sender_id", "binding_key", "bot_uuid", "thread_id", "source_message_id", "transport")}}
    route_detail["_inbound"] = {"event_type": str(inbound.get("event_type") or "message"),
                                "revision": str(inbound.get("revision") or ""),
                                "reply_to": str(inbound.get("reply_to") or ""),
                                "delivery_key": str(inbound.get("delivery_key") or "")}
    stage = "binding"
    try:
        store.require_im_binding(user_id, channel, str(inbound.get("node_id") or ""), inbound["reply_ctx"])
        stage = "history"
        history = confirmed_history(inbound, binding) or store.recent_im_history(user_id, inbound, binding)
        stage = "record"
        store.save_turn({**turn_base, "status": "processing", "detail": route_detail})
        evidence = []
        if inbound.get("attachments"):
            stage = "media"
            from mino_nexus.services.assistant_media import ingest
            try:
                text, evidence = ingest(inbound, origin)
            except ValueError as exc:
                store.save_turn({**turn_base, "status": "blocked", "assistant_text": str(exc), "detail": route_detail})
                _send(inbound, str(exc), turn_id=turn_id)
                return
            conversation.require_generation(origin, generation)
            if not text:
                from mino_nexus.services.assistant_runtime import result_for
                result = result_for(answer="\n\n".join(row["text"] for row in evidence) + "\n\n请说明要咨询的问题，或要测试的应用和模块。", origin=origin)
                store.save_turn({**turn_base, "status": result["card"]["status"], "assistant_text": result["text"],
                                 "card": result["card"], "detail": route_detail})
                _send(inbound, result["text"], result["card"], turn_id=turn_id)
                return
        stage = "agent"
        result = handle_utterance(
            user_id=user_id,
            text=text,
            recent_turns=history + evidence,
            channel=channel,
            chat_id=chat_id,
            node_id=str(inbound.get("node_id") or ""),
            reply_ctx=inbound.get("reply_ctx") if isinstance(inbound.get("reply_ctx"), dict) else {},
            inbound=inbound,
        )
    except Exception as exc:
        diagnostic = _failure_diagnostic(exc, stage)
        SLog.w(TAG, f"agent failed: {diagnostic['error_type']} reason={diagnostic['error_reason']} stage={stage} site={diagnostic['error_site']}")
        failure_text = "当前没有可确认的执行结果，请先在任务页查询状态，避免重复提交。"
        store.save_turn({**turn_base, "status": "interrupted", "assistant_text": failure_text,
                         "elapsed_ms": int((time.time() - started) * 1000),
                         "detail": {**route_detail, "request_id": conversation.get_focus(origin).get("request_id") or "",
                                    **diagnostic, "delivery_status": "prepared"}})
        try:
            store.require_im_binding(user_id, channel, str(inbound.get("node_id") or ""), inbound["reply_ctx"])
            conversation.require_generation(origin, generation)
            stream.finish("回复已中断。当前没有可确认的执行结果，请在任务页查看状态。", {"card_type": "notification", "header": {"title": "处理结果待确认"}})
            _send(inbound, failure_text, turn_id=turn_id)
        except PermissionError:
            pass
        raise
    finally:
        dispatch_log.reset(context_token)
    usage = result.get("usage") or {}
    store.save_turn({
        **turn_base,
        "user_id": user_id,
        "surface": "im",
        "channel": channel,
        "chat_id": chat_id,
        "node_id": str(inbound.get("node_id") or ""),
        "msg_key": key,
        "detail": {**route_detail, "request_id": result.get("request_id") or "", "task_id": result.get("task_id") or "", "app_id": result.get("app_id") or "", "card_type": (result.get("card") or {}).get("card_type"), "usage_status": "reported" if result.get("usage", {}).get("available", True) else "missing", "routing_path": result.get("routing_path") or "", "delivery_status": "prepared"},
        "assistant_text": str(result.get("text") or ""),
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
    conversation.require_generation(origin, generation)
    _send(inbound, str(result.get("text") or ""), result.get("card"), stream=stream, turn_id=turn_id)
    if result.get("model_error"):
        raise RuntimeError("模型不可用，入站消息可在安全范围恢复")
