"""Session Event Log 持久化。"""
from __future__ import annotations

from mino_nexus.core.execution_user import current_user_id, require_user_id
from datetime import datetime
from typing import Any

from mino_nexus.models.session_event import SessionEvent, SessionMeta
from mino_nexus.services.session_atomic import append_batch


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def open_meta(
    *,
    session_id: str,
    run_id: str,
    case_id: str = "",
    app_id: str = "",
) -> SessionMeta:
    from mino_nexus.core.database import SessionLocal, ensure_db

    ensure_db()
    db = SessionLocal()
    try:
        row = db.query(SessionMeta).filter(SessionMeta.session_id == session_id).first()
        if row is None:
            from mino_nexus.core.build_info import identity
            provenance = identity()
            row = SessionMeta(
                session_id=session_id,
                owner_user_id=require_user_id(),
                run_id=run_id,
                case_id=case_id,
                app_id=app_id,
                status="running",
                event_count=1,
                started_at=_now(),
                format_version=1,
            )
            db.add(row)
            db.add(SessionEvent(session_id=session_id, seq=1, ts=row.started_at,
                                type="session/provenance", turn=0, phase="",
                                payload={"schema_version": 1, "nexus": provenance}))
            db.commit()
            db.refresh(row)
        if row.owner_user_id != require_user_id():
            raise PermissionError("session_owner_mismatch")
        if row.run_id != run_id or row.case_id != case_id or row.app_id != app_id:
            raise ValueError("session_identity_reused")
        if row.finished_at or row.finalization_id:
            raise ValueError("session_already_finalized")
        return row
    finally:
        db.close()


def append_event(
    *,
    session_id: str,
    type: str,
    payload: dict[str, Any],
    turn: int = 0,
    phase: str = "",
) -> int:
    from mino_nexus.services.session_atomic import append_batch
    return append_batch(session_id, [{"type": type, "payload": payload,
                                     "turn": turn, "phase": phase}])[0]["seq"]


def is_open(session_id: str) -> bool:
    meta = get_meta(session_id)
    return bool(meta and not str(meta.get("finished_at") or "").strip())


def force_close(
    *,
    session_id: str,
    status: str,
    summary: str = "",
    step_count: int = 0,
) -> bool:
    """从循环外关闭 session（取消 / 节点断开 / 批次结束兜底）。"""
    sid = str(session_id or "").strip()
    if not sid:
        return False
    meta = get_meta(sid)
    if meta is None or not is_open(sid):
        return False
    body = {
        "status": str(status or "unknown"),
        "summary": str(summary or "")[:2000],
        "step_count": int(step_count or 0),
        "forced": True,
    }
    try:
        if (meta.get("runtime_context") or {}).get("execution_binding") is not None:
            finalize_bound(session_id=sid, body=body, turn=0, phase="done")
            return True
        append_event(
            session_id=sid,
            type="session/end",
            payload=body,
            turn=0,
            phase="done",
        )
        close_meta(session_id=sid, status=body["status"], summary=body["summary"])
        return True
    except Exception:
        return False


def close_meta(
    *,
    session_id: str,
    status: str,
    summary: str = "",
) -> None:
    from mino_nexus.core.database import SessionLocal, ensure_db

    ensure_db()
    db = SessionLocal()
    try:
        meta = db.query(SessionMeta).filter(SessionMeta.session_id == session_id).first()
        if meta is None:
            return
        if meta.owner_user_id != require_user_id():
            raise PermissionError("session_owner_mismatch")
        if meta.finalization_id:
            raise ValueError("session_already_finalized")
        if (meta.runtime_context or {}).get("execution_binding") is not None:
            raise ValueError("bound_session_requires_atomic_finalization")
        meta.status = str(status or "unknown")
        meta.summary = str(summary or "")[:2000]
        meta.finished_at = _now()
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _meta_dict(row: SessionMeta) -> dict[str, Any]:
    return {
        "session_id": row.session_id,
        "run_id": row.run_id,
        "case_id": row.case_id or "",
        "app_id": row.app_id or "",
        "status": row.status or "",
        "event_count": int(row.event_count or 0),
        "started_at": row.started_at or "",
        "finished_at": row.finished_at or "",
        "format_version": int(row.format_version or 1),
        "summary": row.summary or "",
        "runtime_context": dict(row.runtime_context or {}),
        "verdict": dict(row.verdict or {}),
        "finalization_id": row.finalization_id or "",
        "binding_record": binding_record(row.runtime_context),
    }


def list_sessions(
    *,
    app_id: str = "",
    run_id: str = "",
    case_id: str = "",
    status: str = "",
    limit: int = 30,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    from mino_nexus.core.database import SessionLocal, ensure_db

    ensure_db()
    db = SessionLocal()
    try:
        q = db.query(SessionMeta)
        if current_user_id():
            q = q.filter(SessionMeta.owner_user_id == current_user_id())
        aid = str(app_id or "").strip()
        rid = str(run_id or "").strip()
        cid = str(case_id or "").strip()
        st = str(status or "").strip()
        if aid:
            q = q.filter(SessionMeta.app_id == aid)
        if rid:
            q = q.filter(SessionMeta.run_id == rid)
        if cid:
            q = q.filter(SessionMeta.case_id == cid)
        if st:
            q = q.filter(SessionMeta.status == st)
        total = int(q.count())
        lim = max(1, min(100, int(limit or 30)))
        off = max(0, int(offset or 0))
        rows = (
            q.order_by(SessionMeta.started_at.desc(), SessionMeta.session_id.desc())
            .offset(off)
            .limit(lim)
            .all()
        )
        return [_meta_dict(row) for row in rows], total
    finally:
        db.close()


def get_meta(session_id: str) -> dict[str, Any] | None:
    from mino_nexus.core.database import SessionLocal, ensure_db

    sid = str(session_id or "").strip()
    if not sid:
        return None
    ensure_db()
    db = SessionLocal()
    try:
        row = db.query(SessionMeta).filter(SessionMeta.session_id == sid).first()
        if row is None or (current_user_id() and row.owner_user_id != current_user_id()):
            return None
        return _meta_dict(row)
    finally:
        db.close()


def resolve_session_id(run_id: str, *, case_id: str = "") -> str:
    """把 batch run_id / report_run_id 解析成 session_id，供回放读 log。"""
    rid = str(run_id or "").strip()
    if not rid:
        return ""
    if get_meta(rid) is not None:
        return rid
    cid = str(case_id or "").strip()
    if not cid and "::" in rid:
        _batch, _, tail = rid.partition("::")
        cid = str(tail.split("::", 1)[0] or "").strip()
        want = f"{_batch}::{cid}" if _batch and cid else rid
        if want != rid and get_meta(want) is not None:
            return want
        if cid and get_meta(rid) is None:
            # 明确要某条 case 的 session，但库里没有 —— 禁止回退到同批其它 case
            return rid
    if cid and "::" not in rid:
        want = f"{rid}::{cid}"
        if get_meta(want) is not None:
            return want
        return want
    batch = rid.partition("::")[0]
    items, _ = list_sessions(run_id=batch, case_id=cid, limit=20)
    if len(items) == 1:
        return str(items[0].get("session_id") or "")
    for row in items:
        sid = str(row.get("session_id") or "")
        if sid and (not cid or str(row.get("case_id") or "") == cid):
            return sid
    return rid


def read_events(
    session_id: str,
    *,
    from_seq: int = 0,
    limit: int = 500,
) -> list[dict[str, Any]]:
    from mino_nexus.core.database import SessionLocal, ensure_db

    sid = str(session_id or "").strip()
    if not sid or (current_user_id() and get_meta(sid) is None):
        return []
    ensure_db()
    db = SessionLocal()
    try:
        q = (
            db.query(SessionEvent)
            .filter(SessionEvent.session_id == sid, SessionEvent.seq >= int(from_seq or 0))
            .order_by(SessionEvent.seq.asc())
            .limit(max(1, min(2000, int(limit or 500))))
        )
        return [
            {
                "session_id": row.session_id,
                "seq": int(row.seq),
                "ts": row.ts,
                "type": row.type,
                "turn": int(row.turn or 0),
                "phase": row.phase or "",
                "payload": dict(row.payload or {}),
            }
            for row in q.all()
        ]
    finally:
        db.close()


def binding_record(context):
    """Historical view only: never reconstruct a binding from today's registry."""
    value = (context or {}).get("execution_binding")
    if value is None:
        return {"status": "legacy", "message": "旧合同记录，未记录统一绑定"}
    if not isinstance(value, dict) or type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        return {"status": "unsupported", "message": "不支持该绑定详情版本"}
    from mino_nexus.services.execution_binding import validate_binding
    try:
        validate_binding(value)
    except ValueError:
        return {"status": "invalid", "message": "绑定记录校验失败，保留原始记录"}
    return {"status": "supported", "schema_version": 1, "binding_id": value["binding_id"]}


def finalize_bound(*, session_id, body, turn=0, phase="done"):
    """Ordinary outcomes retain their guarantee level; only commit is atomic."""
    from mino_nexus.services.execution_binding_store import finalization_digest
    from mino_nexus.services.run_write_safety import protected
    meta = get_meta(session_id)
    if not meta or not (meta.get("runtime_context") or {}).get("execution_binding"):
        raise ValueError("binding_not_committed")
    body = dict(body)
    target_sn = str(meta['runtime_context']['execution_binding'].get('target', {}).get('sn') or '')
    if protected(meta["run_id"], sn=target_sn):
        body.update(status="blocked", summary="写动作结果未确认，资源仍隔离", reason_code="write_outcome_unknown")
    records = [{"type": "session/end", "payload": body, "turn": turn, "phase": phase}]
    patch = {"status": str(body.get("status") or "unknown"), "summary": str(body.get("summary") or "")[:2000],
             "verdict": {"verification_status": "unverified", "outcome": str(body.get("status") or "unknown"),
                         "reason_code": body.get("reason_code") or "ordinary_execution_result",
                         "evidence_scope": (meta.get("verdict") or {}).get("evidence_scope") or {"session_id": session_id, "through_seq": meta["event_count"]},
                         "binding_id": meta["runtime_context"]["execution_binding"]["binding_id"]}}
    # Stable content identity; identical retries succeed, changed outcomes conflict.
    patch["finalization_id"] = "ordinary-" + finalization_digest(records, patch)
    try:
        return append_batch(session_id, records, patch, eligible=True)
    except ValueError as exc:
        if str(exc) != "task_cancelled" or body.get("status") in {"cancelled", "cancel"}:
            raise
        return finalize_bound(session_id=session_id,
            body={**body, "status": "cancelled", "reason_code": "task_cancelled", "summary": "任务已取消"},
            turn=turn, phase=phase)
