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


def _messages(doc: dict[str, Any], task_id: str) -> list[str]:
    cases = [case for case in doc.get("cases") or [] if isinstance(case, dict)]
    passed = sum(case.get("status") == "pass" for case in cases)
    failed = sum(case.get("status") == "fail" for case in cases)
    judged = passed + failed
    run_status = str(doc.get("status") or "")
    header = f"跑批结束：任务 {task_id}（{_RUN_STATUS.get(run_status, run_status or '未知')}）"
    if not cases:
        return [header + "\n未获取到用例明细，请在任务中心查看。"]
    header += (f"\n共 {len(cases)} 条，通过 {passed}，失败 {failed}，"
               f"其他 {len(cases) - judged}；通过率 {passed / judged:.0%}，失败率 {failed / judged:.0%}"
               if judged else f"\n共 {len(cases)} 条，暂无已判定通过/失败的用例")
    messages = [header]
    for index, case in enumerate(cases, 1):
        name = str(case.get("name") or case.get("case_id") or "未命名").replace("\n", " ")[:100]
        status = str(case.get("status") or "pending")
        line = f"{index}. {name}：{_STATUS.get(status, status)}"
        summary = " ".join(str(case.get("summary") or "").split())
        if summary:
            if "flow_block:fuse_repeat_tap" in summary:
                line += "；操作因重复点击保护而停止，不能据此判断 UI 展示是否正确"
            line += f"；执行摘要：{summary[:500]}" + ("…" if len(summary) > 500 else "")
        if len(messages[-1]) + len(line) + 1 > 1600:
            messages.append(f"任务 {task_id} 用例结果（续）")
        messages[-1] += "\n" + line
    return messages


def on_run_finished(run_doc: dict[str, Any]) -> None:
    if not isinstance(run_doc, dict):
        return
    keys = [str(run_doc.get("task_id") or ""), str(run_doc.get("run_id") or "")]
    keys = [k for k in keys if k]
    if not keys:
        return
    with _SEND_LOCK:
        doc = run_store.get(keys[0]) or run_doc
        messages = _messages(doc, keys[0])
        seen: set[int] = set()
        for key in keys:
            for sub in store.active_subscriptions_for("run_done", key):
                from mino_nexus.core.execution_user import current_user_id
                from mino_nexus.services.assistant_gateway import validated_source
                if sub.get("user_id") != current_user_id() or doc.get("owner_user_id") != current_user_id():
                    continue
                ctx = sub.get("reply_ctx") or {}
                instance = str(ctx.get("instance_id") or ctx.get("bot_uuid") or "")
                source = {**sub, "instance_id": instance}
                if validated_source(source) != current_user_id():
                    continue
                if store.find_identity(str(sub.get("channel") or ""), str(ctx.get("tenant") or ""),
                    str(sub.get("sender_id") or ""), instance_id=instance, node_id=str(sub.get("node_id") or "")) != current_user_id():
                    continue
                sid = int(sub.get("id") or 0)
                if sid in seen:
                    continue
                seen.add(sid)
                try:
                    from mino_nexus.services.assistant_gateway import send_to_chat

                    for text in messages:
                        store.require_im_binding(current_user_id(), str(sub.get("channel") or ""),
                            str(sub.get("node_id") or ""), ctx)
                        send_to_chat(
                            channel=str(sub.get("channel") or ""),
                            node_id=str(sub.get("node_id") or ""),
                            chat_id=str(sub.get("chat_id") or ""),
                            sender_id=str(sub.get("sender_id") or ""),
                            reply_ctx=sub.get("reply_ctx") if isinstance(sub.get("reply_ctx"), dict) else {},
                            text=text,
                        )
                except Exception as exc:
                    SLog.w(TAG, f"notify failed: {type(exc).__name__}")
                    continue
                if sid:
                    store.finish_subscription(sid)
