"""跑批结束后，把结果推回登记过订阅的 IM 会话。"""
from __future__ import annotations


import threading
from typing import Any

from mino_nexus.core.log import SLog
from mino_nexus.services import assistant_store as store
from mino_nexus.services import run_store

TAG = "AssistantNotify"
_SEND_LOCK = threading.Lock()
_STATUS = {"pass": "通过", "fail": "失败", "blocked": "阻断", "declined": "拒绝", "cancelled": "取消", "skip": "跳过", "skipped": "跳过", "untestable": "不可测", "unverifiable": "不可验证", "unexecutable": "不可执行"}
_RUN_STATUS = {"done": "完成", "failed": "失败", "cancelled": "已取消"}


def _case_detail(case: dict[str, Any]) -> tuple[str, str]:
    """只根据实际错误给建议；没有错误证据时不猜测根因。"""
    import re
    from mino_nexus.services.assistant_errors import explain
    text = str(case.get("summary") or case.get("error") or "").strip()
    advice = ""
    if "flow_block:fuse_repeat_tap" in text:
        text = "重复点击保护已停止执行，尚不能确认原操作是否成功。"
        advice = "查看报告中的最后一次点击位置和前后截图，确认目标后重试。"
    elif "credential" in text or "缺少槽" in text:
        text, hints = explain(text)
        advice = "；".join(hints)
    elif any(key in text for key in ("model_no_tool_calls", "没有返回 tool_calls", "没有 function call")):
        text = "模型未提供可执行的操作指令，本轮没有下发设备操作。"
        advice = "在报告中查看模型调用结果，修复模型响应或工具解析后重试。"
    elif "capture_black=yes" in text or "截图不可用或全黑" in text:
        text = "设备截图为黑屏或不可用，屏幕恢复后仍未取得可用画面，测试尚未继续。"
        advice = "确认设备屏幕已亮起且可正常截图，并查看报告中的恢复动作是否成功。"
    if not text:
        text = "未记录具体原因，当前无法判断问题发生在哪一步。"
    text = re.sub(r'步骤\s*(\d+)\s*操作', r'操作步骤 \1', text)
    # 原始执行摘要可能包含账号字段，通知不展示凭据。
    text = re.sub(r'(?i)(password|passwd|token|otp|验证码|密码)\s*[:=：]\s*[^\s,，;；]+', r'\1：[已隐藏]', text)
    text = re.sub(r'(?<!\d)1[3-9]\d{9}(?!\d)', '[手机号已隐藏]', text)
    return text, advice


def _measure(text: str, unit: str) -> int:
    return len(text.encode("utf-16-le")) // 2 if unit == "utf16" else len(text)


def _clip(text: str, budget: int, unit: str) -> str:
    from mino_nexus.services.assistant_cards import _graphemes
    if _measure(text, unit) <= budget:
        return text
    out, size = [], 0
    for group in _graphemes(text):
        width = _measure(group, unit)
        if size + width > max(0, budget - 1):
            break
        out.append(group)
        size += width
    return "".join(out) + "…"


def _notification_payloads(doc: dict[str, Any], task_id: str, *, base_url: str = "",
                           max_characters: int = 1600, length_unit: str = "characters") -> list[dict]:
    from collections import Counter
    from mino_nexus.services.assistant_cards import render_text
    from mino_nexus.services.assistant_reports import report_url
    cases = [case for case in doc.get("cases") or [] if isinstance(case, dict)]
    counts = Counter(str(case.get("status") or "pending") for case in cases)
    normal = {"pass", "skip", "skipped"}
    abnormal = [(index, case) for index, case in enumerate(cases, 1) if case.get("status") not in normal]
    failed = counts["fail"]
    blocked = sum(counts[key] for key in ("blocked", "untestable", "unverifiable", "unexecutable", "declined", "pending", "queued", "running"))
    statuses = {**_STATUS, "blocked": "未能执行", "pending": "未执行", "queued": "未执行", "running": "结果未确认"}
    url = report_url(doc, task_id, base_url=base_url,
                     case_id=str(cases[0].get("case_id") or "") if len(cases) == 1 else "")
    limit = max(256, min(int(max_characters), 1600))
    status = "failed" if failed or doc.get("status") == "failed" else "blocked" if blocked else "done"
    context = " · ".join(str(doc.get(key) or "") for key in ("app_name", "platform") if doc.get(key))
    if len(cases) == 1:
        overview = context
    elif cases:
        overview = f"共 {len(cases)} 条：通过 {counts['pass']}，失败 {failed}，未能执行 {blocked}"
        rest = len(cases) - counts['pass'] - failed - blocked
        if rest:
            overview += f"，跳过/取消 {rest}"
        if context:
            overview += "。" + context
    else:
        overview = f"任务{_RUN_STATUS.get(str(doc.get('status') or ''), '已结束')}，没有可用的用例结果明细。"
    if doc.get("status") == "cancelled":
        overview = "任务已取消。" + overview
    if not url:
        overview += "\n报告链接未就绪，请在 Workbench 对话设置中配置可访问的报告地址。"
    show_limited = len(abnormal) > 5
    if show_limited:
        overview += f"\n下方展示前 5 条未通过用例，共 {len(abnormal)} 条；完整结果见测试报告。"
        selected = abnormal[:5]
    else:
        # 失败摘要前置，同时保留每条用例结果；通过项不附冗长摘要。
        selected = abnormal + [(i, case) for i, case in enumerate(cases, 1) if case.get("status") in normal]
    actions = [{"id": "test_report", "label": "查看测试报告", "url": url}] if url else []

    def card_for(items: list[dict], summary: str = overview) -> dict:
        if len(cases) == 1 and items:
            blocks = [{"type": "case_results", "items": items}]
            if context:
                blocks.append({"type": "kv", "items": [["测试应用 / 渠道", context]]})
            if not url:
                blocks.append({"type": "problem", "text": "报告链接未就绪，请在 Workbench 对话设置中配置可访问的报告地址。"})
        else:
            blocks = ([{"type": "summary", "text": summary}] if summary else []) + ([{"type": "case_results", "items": items}] if items else [])
        return {"version": 1, "template": "task_result", "card_type": "notification", "status": status,
                "header": {"title": "测试结果", "tone": "danger" if status == "failed" else "warn" if abnormal else "success"},
                "blocks": blocks, "actions": actions}

    def fits(items: list[dict], summary: str = overview) -> bool:
        return _measure(render_text(card_for(items, summary)), length_unit) <= limit - 20

    if not fits([]):
        # 优先保留完整 URL，不按字符切断报告链接。
        overhead = _measure(render_text(card_for([], "")), length_unit)
        overview = _clip(overview, max(0, limit - 24 - overhead), length_unit)
    pages, current = [], []
    for number, case in selected:
        verdict = str(case.get("status") or "pending")
        summary, advice = _case_detail(case) if verdict not in normal else ("", "")
        if show_limited:
            advice = ""
        row = {"number": number if len(cases) > 1 else "", "case_id": str(case.get("case_id") or ""),
               "name": _clip(str(case.get("name") or case.get("case_id") or "未命名").replace("\n", " "), 90, length_unit),
               "status": verdict, "status_label": statuses.get(verdict, "结果待确认"),
               "summary": _clip(summary, 100 if show_limited else 220, length_unit), "advice": _clip(advice, 140, length_unit)}
        if not fits(current + [row]) and current:
            pages.append(card_for(current))
            current = []
        if not fits([row]):
            row["advice"] = ""
            row["name"] = _clip(row["name"], 48, length_unit)
            available = limit - 24 - _measure(render_text(card_for([{**row, "summary": ""}])), length_unit)
            row["summary"] = _clip(summary, max(0, available), length_unit) if summary and available > 0 else ""
        if not fits([row]):
            # 极短渠道只发送计数和详情入口，不能伪造已展示全部结果。
            brief = card_for([], f"共 {len(cases)} 条：通过 {counts['pass']}，失败 {failed}，未能执行 {blocked}。详情见报告。")
            if _measure(render_text(brief), length_unit) > limit:
                brief['actions'] = []
                brief['blocks'].append({'type': 'problem', 'text': '报告链接过长，未能完整发送。请在 Workbench 查看对应任务。'})
            return [{"text": render_text(brief), "card": brief}]
        current.append(row)
    if current or not pages:
        pages.append(card_for(current))
    payloads = []
    for index, card in enumerate(pages, 1):
        if len(pages) > 1:
            card["header"]["title"] += f"（{index}/{len(pages)}）"
        payloads.append({"text": render_text(card), "card": card})
    return payloads


def _messages(doc: dict[str, Any], task_id: str, **kwargs) -> list[str]:
    return [item["text"] for item in _notification_payloads(doc, task_id, **kwargs)]


def _channel_capabilities(sub: dict, instance: str) -> dict:
    from mino_nexus.services.assistant_envelope import capabilities
    from mino_nexus.services.im_status import identity_channel, normalize_im_channels
    from mino_nexus.services.node_registry import get_registry
    node = get_registry().get_node(str(sub.get("node_id") or ""))
    rows = normalize_im_channels(node.plugins if node else [])
    for row in rows:
        if identity_channel(row.get("channel")) == identity_channel(sub.get("channel")) and str(row.get("instance_id") or "") == instance:
            return capabilities(row.get("capabilities"))
    return capabilities()


def _claim_notification(sub: dict, target_id: str) -> tuple[str, int]:
    import time, uuid
    from sqlalchemy.dialects.sqlite import insert
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.assistant_delivery import AssistantNotification as N
    from mino_nexus.core.execution_user import require_user_id
    now, owner, token = int(time.time()), require_user_id(), uuid.uuid4().hex
    with session_scope() as db:
        db.execute(insert(N).values(subscription_id=int(sub["id"]), owner_user_id=owner, target_id=target_id,
            state="pending", delivered_segments=0, attempts=0, next_retry_at=0, lease_until=0, lease_token="")
            .on_conflict_do_nothing(index_elements=["subscription_id"]))
        updated = db.query(N).filter(N.subscription_id == int(sub["id"]), N.owner_user_id == owner,
            N.state == "pending", N.next_retry_at <= now, N.lease_until <= now).update(
                {N.lease_token: token, N.lease_until: now + 120, N.attempts: N.attempts + 1}, synchronize_session=False)
        if not updated:
            return "", 0
        row = db.get(N, int(sub["id"]))
        return token, int(row.delivered_segments or 0)


def _progress(sub_id: int, token: str, *, delivered: int | None = None, state: str = "pending", error: str = "") -> bool:
    import time
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.assistant_delivery import AssistantNotification as N
    from mino_nexus.models.assistant import AssistantSubscription
    from mino_nexus.core.execution_user import require_user_id
    with session_scope() as db:
        row = db.get(N, sub_id)
        if row is None or row.owner_user_id != require_user_id() or row.lease_token != token or row.lease_until < int(time.time()):
            return False
        now = int(time.time())
        values = {N.state: state, N.last_error: error,
                  N.lease_until: now + 120 if state == "pending" and not error else 0,
                  N.next_retry_at: now + min(3600, 30 * 2 ** min(int(row.attempts or 1) - 1, 7)) if error else 0}
        if delivered is not None:
            values[N.delivered_segments] = delivered
        updated = db.query(N).filter(N.subscription_id == sub_id, N.owner_user_id == require_user_id(),
                                    N.lease_token == token, N.lease_until >= now).update(values, synchronize_session=False)
        if not updated:
            return False
        if state in ("done", "revoked"):
            sub = db.get(AssistantSubscription, sub_id)
            if sub is not None and sub.user_id == require_user_id():
                sub.status = state
        return True


def on_run_finished(run_doc: dict[str, Any]) -> None:
    if not isinstance(run_doc, dict):
        return
    keys = list(dict.fromkeys(str(run_doc.get(k) or "") for k in ("task_id", "run_id")))
    keys = [key for key in keys if key]
    if not keys:
        return
    from mino_nexus.core.execution_user import require_user_id
    from mino_nexus.services.assistant_gateway import validated_source, send_to_chat
    owner = require_user_id()
    doc = run_store.get(keys[0]) or run_doc
    if doc.get("owner_user_id") != owner:
        return
    from mino_nexus.services.assistant_reports import configuration
    base_url = configuration(owner)["effective_url"]
    with _SEND_LOCK:
        for key in keys:
            for sub in store.active_subscriptions_for("run_done", key):
                if sub.get("user_id") != owner:
                    continue
                token, first = _claim_notification(sub, keys[0])
                if not token:
                    continue
                sid, ctx = int(sub["id"]), sub.get("reply_ctx") or {}
                try:
                    instance = str(ctx.get("instance_id") or ctx.get("bot_uuid") or "")
                    if validated_source({**sub, "instance_id": instance}) != owner:
                        raise PermissionError("通知来源或绑定失效")
                    capability = _channel_capabilities(sub, instance)
                    outgoing = _notification_payloads(doc, keys[0], base_url=base_url,
                        max_characters=capability["max_characters"], length_unit=capability["length_unit"])
                    if ctx.get("chat_type") == "group":
                        from mino_nexus.services.assistant_conversation import group_policy
                        policy = group_policy({**ctx, "channel": sub.get("channel"), "chat_id": sub.get("chat_id")})
                        if not policy.get("enabled") or not policy.get("allow_execute"):
                            raise PermissionError("群策略已撤销")
                        if not policy.get("disclose_results"):
                            outgoing = [{"text": "本次测试任务已结束。详细结果请在私聊或 Workbench 查看。", "card": {"card_type": "notification", "status": "done", "header": {"title": "测试已结束"}, "blocks": [{"type": "summary", "text": "详细结果请在私聊或 Workbench 查看。"}], "actions": []}}]
                    for index in range(first, len(outgoing)):
                        store.require_im_binding(owner, str(sub.get("channel") or ""), str(sub.get("node_id") or ""), ctx)
                        if not _progress(sid, token, delivered=index):
                            return  # 被其他工作者取得租约，不能继续发送
                        send_to_chat(channel=str(sub.get("channel") or ""), node_id=str(sub.get("node_id") or ""),
                            chat_id=str(sub.get("chat_id") or ""), sender_id=str(sub.get("sender_id") or ""),
                            reply_ctx=ctx, text=outgoing[index]["text"], delivery_id=f"notify-{sid}-{keys[0]}-{index}",
                            card=outgoing[index]["card"] if capability["cards"] else None)
                        if not _progress(sid, token, delivered=index + 1):
                            return
                    _progress(sid, token, state="done")
                except PermissionError:
                    _progress(sid, token, state="revoked", error="binding_revoked")
                except Exception as exc:
                    _progress(sid, token, state="uncertain" if "未知" in str(exc) else "pending", error=type(exc).__name__)
                    SLog.w(TAG, f"通知已记录重试：{type(exc).__name__}")


def retry_pending_notifications() -> None:
    """启动后和定时恢复；包括完成事件发出前进程崩溃而尚未建立 delivery 的订阅。"""
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.assistant import AssistantSubscription
    from mino_nexus.core.execution_user import execution_user
    with session_scope() as db:
        subs = [(row.user_id, row.target_id) for row in db.query(AssistantSubscription).filter(
            AssistantSubscription.status == "active", AssistantSubscription.kind == "run_done").all()]
    for owner, target in dict.fromkeys(subs):
        if not owner:
            continue
        with execution_user(owner):
            doc = run_store.get(target)
            if doc and doc.get("owner_user_id") == owner and doc.get("status") in ("done", "failed", "cancelled"):
                on_run_finished(doc)
