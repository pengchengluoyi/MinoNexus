"""议题 digest 与通知（§3.8）。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from mino_nexus.core.log import SLog
from mino_nexus.models.mino_bots import CollabTopic, CollabWorkItem

TAG = "CollabDigest"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def refresh_topic_digest(db, topic_id: str) -> dict[str, Any]:
    topic = db.get(CollabTopic, topic_id)
    if topic is None:
        return {}
    rows = (
        db.query(CollabWorkItem)
        .filter_by(topic_id=topic_id)
        .order_by(CollabWorkItem.updated_at.desc())
        .all()
    )
    changed: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    next_items: list[dict[str, Any]] = []
    for wi in rows:
        st = str(wi.status or "")
        row = {
            "work_item_id": wi.work_item_id,
            "title": wi.title or "",
            "action_kind": wi.action_kind or "",
            "status": st,
            "origin": wi.origin or "",
        }
        if st == "done":
            ev = wi.evidence if isinstance(wi.evidence, dict) else {}
            if ev:
                evidence.append({"work_item_id": wi.work_item_id, **ev})
            changed.append({**row, "summary": (wi.last_result or {}).get("message") or ""})
        elif st == "blocked":
            blocked.append({**row, "reason": wi.blocked_reason or ""})
        elif st in ("pending", "waiting", "running"):
            next_items.append(row)
        elif st == "failed":
            changed.append({**row, "summary": wi.blocked_reason or "失败"})
    digest = {
        "changed": changed[:12],
        "blocked": blocked[:8],
        "evidence": evidence[:12],
        "next": next_items[:12],
        "updated_at": _now(),
    }
    topic.digest = digest
    topic.updated_at = _now()
    db.flush()
    return digest


def maybe_notify_collab(
    project_id: str,
    topic_id: str,
    *,
    reason: str,
    message: str,
    policy: dict[str, Any] | None = None,
) -> None:
    from mino_nexus.services.collab_policy import get_policy

    pol = policy or get_policy(project_id)
    channels = pol.get("notify") if isinstance(pol.get("notify"), list) else ["workbench"]
    text = str(message or "").strip()
    if not text:
        return
    payload = {
        "event": "collab_notify",
        "project_id": project_id,
        "topic_id": topic_id,
        "reason": reason,
        "message": text[:2000],
    }
    if "workbench" in [str(c).lower() for c in channels]:
        try:
            from mino_nexus.loop.observe.agent_stream import emit_testing_task

            emit_testing_task(payload)
        except Exception as exc:
            SLog.w(TAG, f"workbench notify: {exc}")
    if "feishu" in [str(c).lower() for c in channels]:
        SLog.i(TAG, f"feishu collab notify skipped (no route): {reason}")


def on_work_item_terminal(
    db,
    topic: CollabTopic,
    wi: CollabWorkItem,
    *,
    policy: dict[str, Any] | None = None,
) -> None:
    refresh_topic_digest(db, topic.topic_id)
    st = str(wi.status or "")
    if st == "blocked":
        maybe_notify_collab(
            topic.project_id,
            topic.topic_id,
            reason="blocked",
            message=f"议题「{topic.title}」需要补充信息：{wi.blocked_reason or wi.title}",
            policy=policy,
        )
    elif st == "failed":
        maybe_notify_collab(
            topic.project_id,
            topic.topic_id,
            reason="failed",
            message=f"议题「{topic.title}」工作项失败：{wi.title} — {wi.blocked_reason or '见证据'}",
            policy=policy,
        )
    rows = db.query(CollabWorkItem).filter_by(topic_id=topic.topic_id).all()
    openish = [r for r in rows if str(r.status or "") not in ("done", "failed", "blocked")]
    if not openish and rows:
        maybe_notify_collab(
            topic.project_id,
            topic.topic_id,
            reason="all_done",
            message=f"议题「{topic.title}」全部工作项已完成。",
            policy=policy,
        )
