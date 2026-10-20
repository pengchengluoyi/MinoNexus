"""协作事件：emit 写工作项 / 唤醒 worker（§3.4）。"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from mino_nexus.core.log import SLog
from mino_nexus.models.mino_bots import CollabTopic, CollabWorkItem

TAG = "CollabEvents"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def emit(kind: str, **ctx: Any) -> None:
    # Only referenced run completion is part of the V7 workspace contract.
    # Source, compilation and resource events must not awaken an execution queue.
    if kind != 'run_finished':
        return
    from mino_nexus.services.collab_workspace import notify_run_finished
    try:
        notify_run_finished(ctx.get('doc') or {})
    except Exception as exc:
        SLog.w(TAG, f"workspace notification: {type(exc).__name__}: {exc}")
    try:
        from mino_nexus.services.workspace_automation import on_finished
        on_finished(ctx.get('doc') or {})
    except Exception as exc:
        SLog.w(TAG, f"workspace automation: {type(exc).__name__}: {exc}")


def on_run_finished(doc: dict[str, Any]) -> None:
    emit("run_finished", doc=doc)


def _on_run_finished(doc: dict[str, Any]) -> None:
    from mino_nexus.services.collab_worker import bump_waiting_for_run

    run_id = str(doc.get("run_id") or "").strip()
    if not run_id:
        return
    bump_waiting_for_run(run_id)
    run_type = str(doc.get("run_type") or "manual")
    if run_type == "collab":
        return
    from mino_nexus.services.collab_policy import get_policy

    app_id = str(doc.get("app_id") or "").strip()
    project_id = str(doc.get("project_id") or "").strip()
    if not project_id and app_id:
        try:
            from mino_nexus.services import project_store as ps

            project_id = str(ps.require_app(app_id).get("project_id") or "")
        except Exception:
            project_id = ""
    if not app_id or not project_id:
        return
    pol = get_policy(project_id, app_id=app_id)
    if not pol.get("watch_manual_runs"):
        return
    failed_cases = [
        str(c.get("case_id") or "")
        for c in (doc.get("cases") or [])
        if isinstance(c, dict) and str(c.get("status") or "") in ("fail", "failed", "blocked")
    ]
    failed_cases = [c for c in failed_cases if c]
    if not failed_cases:
        return
    from mino_nexus.core.database import session_scope

    now = _now()
    with session_scope() as db:
        for cid in failed_cases[:5]:
            topics = _topics_for_case(db, project_id, cid)
            if not topics:
                continue
            topic = topics[0]
            wi_id = str(uuid.uuid4())
            db.add(
                CollabWorkItem(
                    work_item_id=wi_id,
                    topic_id=topic.topic_id,
                    task_id="",
                    title=f"手动跑批失败：{cid}",
                    goal="非协作 run 失败，自动诊断",
                    acceptance="分类失败原因",
                    assignee_kind="bot",
                    holder_user_id=str(topic.lead_user_id or doc.get("owner_user_id") or ""),
                    input_version=topic.input_version or "v1",
                    idempotency_key=f"manual_run:{run_id}:{cid}",
                    status="pending",
                    action_kind="diagnose",
                    action_payload={"run_id": run_id, "case_ids": [cid]},
                    case_ids=[cid],
                    origin="run_failed",
                    created_at=now,
                    updated_at=now,
                )
            )
        db.flush()
    _wake()


def _topics_for_case(db, project_id: str, case_id: str) -> list[CollabTopic]:
    cid = str(case_id or "").strip()
    if not cid:
        return []
    out: list[CollabTopic] = []
    for row in db.query(CollabTopic).filter_by(project_id=project_id, status="open").all():
        anchor = row.anchor if isinstance(row.anchor, dict) else {}
        if str(anchor.get("kind") or "") == "case" and str(anchor.get("case_id") or "") == cid:
            out.append(row)
    return out


def _expire_waiting(db, topic_id: str) -> None:
    now = _now()
    for wi in db.query(CollabWorkItem).filter_by(topic_id=topic_id, status="waiting").all():
        wi.status = "failed"
        wi.blocked_reason = "superseded"
        ev = dict(wi.evidence or {}) if isinstance(wi.evidence, dict) else {}
        ev["expired"] = True
        wi.evidence = ev
        wi.updated_at = now


def _bump_topic_version(db, topic: CollabTopic) -> str:
    from mino_nexus.services.mino_bots import _next_input_version

    ver = _next_input_version(str(topic.input_version or "v1"))
    topic.input_version = ver
    topic.updated_at = _now()
    return ver


def _on_case_changed(
    *,
    project_id: str,
    case_id: str = "",
    case_ids: list[str] | None = None,
    requirement_id: str = "",
    **_ctx: Any,
) -> None:
    pid = str(project_id or "").strip()
    ids = [str(x).strip() for x in (case_ids or []) if str(x).strip()]
    if case_id:
        ids.append(str(case_id).strip())
    ids = list(dict.fromkeys(ids))
    if not pid or not ids:
        return
    from mino_nexus.core.database import session_scope
    from mino_nexus.services.case_store import get_case

    touched_topics: set[str] = set()
    with session_scope() as db:
        for cid in ids:
            for topic in _topics_for_case(db, pid, cid):
                if topic.topic_id in touched_topics:
                    continue
                touched_topics.add(topic.topic_id)
                _expire_waiting(db, topic.topic_id)
                ver = _bump_topic_version(db, topic)
                case = get_case(pid, cid) or {}
                app_id = str(
                    (topic.anchor or {}).get("app_id")
                    or topic.app_id
                    or case.get("app_id")
                    or ""
                ).strip()
                compile_slot = f"recompile:{cid}:{ver}"
                run_slot = f"rerun:{cid}:{ver}"
                holder = str(topic.lead_user_id or "").strip()
                specs = [
                    {
                        "_slot": compile_slot,
                        "title": f"重新编译：{case.get('name') or cid}",
                        "action_kind": "compile",
                        "action_payload": {"case_ids": [cid]},
                        "origin": "case_changed",
                    },
                    {
                        "_slot": run_slot,
                        "title": f"重新执行：{case.get('name') or cid}",
                        "action_kind": "case_run",
                        "action_payload": {"app_id": app_id, "case_ids": [cid], "async_exec": True},
                        "depends_on_slots": [compile_slot],
                        "origin": "case_changed",
                    },
                ]
                _sync_specs_db(db, topic, holder, ver, specs, suffix="case_changed")
        db.flush()
    if touched_topics:
        _wake()


def _sync_specs_db(
    db,
    topic: CollabTopic,
    holder_user_id: str,
    ver: str,
    specs: list[dict[str, Any]],
    *,
    suffix: str,
) -> None:
    from mino_nexus.models.task import Task
    from mino_nexus.services.mino_bots import _idempotency_key, _spec_slot

    slot_to_id: dict[str, str] = {}
    uid = holder_user_id
    now = _now()
    for idx, spec in enumerate(specs):
        title = str(spec.get("title") or "工作项").strip()
        action_kind = str(spec.get("action_kind") or "none")
        payload = dict(spec.get("action_payload") or {})
        slot = _spec_slot(spec, idx)
        ikey = _idempotency_key(topic.topic_id, ver, "", f"{suffix}:{slot}")
        dep_slots = spec.get("depends_on_slots") if isinstance(spec.get("depends_on_slots"), list) else []
        depends_on: list[str] = []
        for ds in dep_slots:
            wid = slot_to_id.get(str(ds))
            if wid:
                depends_on.append(wid)
        existing = db.query(CollabWorkItem).filter_by(topic_id=topic.topic_id, idempotency_key=ikey).first()
        if existing is not None:
            if str(existing.status or "") not in ("running", "waiting"):
                existing.status = "pending"
                existing.depends_on = depends_on
                existing.action_payload = payload
                existing.updated_at = now
            slot_to_id[slot] = existing.work_item_id
            continue
        task_row = Task(
            id=str(uuid.uuid4()),
            app_id=str(topic.app_id or ""),
            name=title[:200],
            task_type="collab_work_item",
            status="pending",
            created_at=now,
        )
        db.add(task_row)
        wi = CollabWorkItem(
            work_item_id=str(uuid.uuid4()),
            topic_id=topic.topic_id,
            task_id=task_row.id,
            title=title,
            goal=str(spec.get("goal") or title),
            acceptance=str(spec.get("acceptance") or ""),
            assignee_kind="bot",
            holder_user_id=uid,
            input_version=ver,
            idempotency_key=ikey,
            status="pending",
            action_kind=action_kind,
            action_payload=payload,
            depends_on=depends_on,
            origin=str(spec.get("origin") or suffix),
            case_ids=list(payload.get("case_ids") or []) if isinstance(payload.get("case_ids"), list) else [],
            created_at=now,
            updated_at=now,
        )
        db.add(wi)
        slot_to_id[slot] = wi.work_item_id


def _on_doc_synced(
    *,
    project_id: str,
    app_id: str = "",
    source_id: str = "",
    title: str = "",
    body_excerpt: str = "",
    owner_user_id: str = "",
    change_summary: str = "",
    **_ctx: Any,
) -> None:
    pid = str(project_id or "").strip()
    sid = str(source_id or "").strip()
    if not pid or not sid:
        return
    from mino_nexus.core.database import session_scope

    aid = str(app_id or "").strip()
    holder = str(owner_user_id or "").strip()
    now = _now()
    topic_id = ""
    with session_scope() as db:
        topic: CollabTopic | None = None
        for row in db.query(CollabTopic).filter_by(project_id=pid).all():
            anchor = row.anchor if isinstance(row.anchor, dict) else {}
            if str(anchor.get("kind") or "") == "doc" and str(anchor.get("source_id") or "") == sid:
                topic = row
                break
        if topic is None:
            topic_id = str(uuid.uuid4())
            topic = CollabTopic(
                topic_id=topic_id,
                project_id=pid,
                app_id=aid,
                title=str(title or "需求文档")[:200],
                goal=str(body_excerpt or change_summary or "")[:4000],
                status="open",
                lead_user_id=holder,
                input_version="v1",
                anchor={"kind": "doc", "source_id": sid, "app_id": aid},
                created_at=now,
                updated_at=now,
            )
            db.add(topic)
        else:
            topic_id = topic.topic_id
            if change_summary:
                topic.goal = (str(topic.goal or "") + "\n\n[文档更新] " + change_summary)[:4000]
            elif body_excerpt:
                topic.goal = body_excerpt[:4000]
            topic.updated_at = now
        topic_id = topic.topic_id
        db.flush()
    if not topic_id or not holder:
        return

    def _run_pipeline() -> None:
        from mino_nexus.core.execution_user import execution_user
        from mino_nexus.services.mino_bots import run_topic_pipeline

        sess = {"user_id": holder}
        try:
            with execution_user(holder):
                run_topic_pipeline(
                    pid,
                    topic_id,
                    sess,
                    goal=str(body_excerpt or change_summary or title or ""),
                    bump_version=True,
                    engine="langgraph",
                )
        except Exception as exc:
            SLog.w(TAG, f"doc_sync pipeline: {exc}")
        _wake()

    from mino_nexus.core.execution_user import user_thread

    user_thread(target=_run_pipeline, daemon=True, name=f"collab-doc-{sid[:8]}").start()


def _on_message(**_ctx: Any) -> None:
    """由 post_message 直接调 orchestrate，此处仅占位。"""
    return


def _wake() -> None:
    try:
        from mino_nexus.services.collab_worker import wake

        wake()
    except Exception:
        pass
