"""回放只读已有 session_events，不再重新决策。"""
from __future__ import annotations

from typing import Any

from mino_nexus.records.eventlog import store as session_store


def replay_session(session_id: str, *, sn: str = "") -> dict[str, Any]:
    del sn
    meta = session_store.get_meta(session_id) or {}
    if not meta:
        return {"ok": False, "code": 404, "reason": "session 不存在"}
    events = session_store.read_events(session_id, limit=2000)
    return {
        "ok": True,
        "code": 200,
        "mode": "read",
        "session_id": session_id,
        "run_id": str(meta.get("run_id") or ""),
        "case_id": str(meta.get("case_id") or ""),
        "events": events,
    }


def fork_session(
    session_id: str,
    *,
    from_turn: int,
    sn: str = "",
    provider_id: str = "",
) -> dict[str, Any]:
    del sn, provider_id
    meta = session_store.get_meta(session_id) or {}
    if not meta:
        return {"ok": False, "code": 404, "reason": "session 不存在"}
    events = session_store.read_events(session_id, limit=2000)
    kept = [row for row in events if int(row.get("turn") or 0) <= int(from_turn or 0)]
    return {
        "ok": True,
        "code": 200,
        "mode": "read",
        "session_id": session_id,
        "fork_from_turn": int(from_turn or 0),
        "run_id": str(meta.get("run_id") or ""),
        "case_id": str(meta.get("case_id") or ""),
        "events": kept,
    }
