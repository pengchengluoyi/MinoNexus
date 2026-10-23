"""把工作项的当前状态同步成工作区里的一张卡片。

卡片只做可读摘要；审批、暂停、接管仍在工作视图里完成。写失败不能影响主流程。
"""
from __future__ import annotations

from typing import Any

from mino_nexus.assistant.work import work_store
from mino_nexus.core.database import session_scope
from mino_nexus.models.mino_bots import CollabTopic

_LABELS = {
    "draft": "草稿", "planning": "制定计划", "running": "执行中",
    "waiting_human": "等待确认", "blocked": "已阻塞", "paused": "已暂停",
    "done": "已完成", "failed": "失败", "cancelled": "已取消",
}


def _summary(work: dict[str, Any]) -> str:
    progress = work.get("progress") or {}
    parts = [_LABELS.get(str(work.get("status") or ""), str(work.get("status") or ""))]
    if progress.get("total"):
        parts.append(f"{progress.get('done') or 0}/{progress.get('total')} 步")
    if work.get("blocked_reason"):
        parts.append("卡住：" + str(work["blocked_reason"])[:120])
    current = next((s for s in (work.get("steps") or [])
                    if s.get("status") in ("running", "waiting")), None)
    if current:
        parts.append("当前：" + str(current.get("title") or current.get("kind") or ""))
    pending = [a for a in (work.get("approvals") or []) if a.get("status") == "pending"]
    if pending:
        parts.append("待确认：" + str(pending[0].get("title") or "计划")[:120])
    return " · ".join(part for part in parts if part)


def sync_card(work_id: str) -> None:
    try:
        work = work_store.detail(work_id, event_limit=0)
        if not work or not work.get("topic_id"):
            return
        owner = str(work.get("owner_user_id") or "")
        if not owner:
            return
        with session_scope() as db:
            topic = db.get(CollabTopic, str(work["topic_id"]))
            if not topic or str(topic.lead_user_id or "") != owner:
                return
            from mino_nexus.assistant.workspace.collab_workspace import message

            message(
                db, topic, body=_summary(work), kind="system", card="work",
                ref=str(work_id), author="bot", owner=owner, role="conductor",
                origin="work",
            )
    except Exception:
        # 卡片同步失败不能回滚或阻断工作状态；工作视图仍能读到真实状态。
        return
