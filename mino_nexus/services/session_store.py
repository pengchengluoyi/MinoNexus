"""Session Event Log 持久化。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from mino_nexus.models.session_event import SessionEvent, SessionMeta


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
            row = SessionMeta(
                session_id=session_id,
                run_id=run_id,
                case_id=case_id,
                app_id=app_id,
                status="running",
                event_count=0,
                started_at=_now(),
                format_version=1,
            )
            db.add(row)
            db.commit()
            db.refresh(row)
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
    from mino_nexus.core.database import SessionLocal, ensure_db

    ensure_db()
    db = SessionLocal()
    try:
        meta = (
            db.query(SessionMeta)
            .filter(SessionMeta.session_id == session_id)
            .first()
        )
        if meta is None:
            raise ValueError(f"session not open: {session_id}")
        seq = int(meta.event_count or 0) + 1
        db.add(
            SessionEvent(
                session_id=session_id,
                seq=seq,
                ts=_now(),
                type=str(type or "").strip(),
                turn=int(turn or 0),
                phase=str(phase or ""),
                payload=dict(payload or {}),
            )
        )
        meta.event_count = seq
        db.commit()
        return seq
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


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
        if row is None:
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
    if cid and "::" not in rid:
        want = f"{rid}::{cid}"
        if get_meta(want) is not None:
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
    if not sid:
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
