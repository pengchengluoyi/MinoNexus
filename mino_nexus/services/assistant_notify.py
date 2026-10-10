"""跑批结束后，把结果推回登记过订阅的 IM 会话。"""
from __future__ import annotations

from typing import Any

from mino_nexus.core.log import SLog
from mino_nexus.services import assistant_store as store
from mino_nexus.services.assistant_cards import build_card, render_wechat_text

TAG = "AssistantNotify"


def on_run_finished(run_doc: dict[str, Any]) -> None:
    if not isinstance(run_doc, dict):
        return
    keys = [str(run_doc.get("task_id") or ""), str(run_doc.get("run_id") or "")]
    keys = [k for k in keys if k]
    if not keys:
        return
    status = str(run_doc.get("status") or "")
    summary = f"任务 {keys[0]} 已结束" + (f"（{status}）" if status else "")
    card = build_card(template_id="case_run_finished", title="跑批结束", summary=summary, data={"task_id": keys[0]}, status="done")
    text = render_wechat_text(card)
    seen: set[int] = set()
    for key in keys:
        for sub in store.active_subscriptions_for("run_done", key):
            sid = int(sub.get("id") or 0)
            if sid in seen:
                continue
            seen.add(sid)
            channel = str(sub.get("channel") or "")
            try:
                from mino_nexus.services.assistant_gateway import send_to_chat

                send_to_chat(
                    channel=channel,
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
