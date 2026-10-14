"""Deadline-bounded atomic event/evidence commits with independent counters."""
from __future__ import annotations

import threading
import time
from collections import Counter

_lock = threading.Lock()
_counts = Counter()


def counters():
    with _lock:
        return dict(_counts)


def append_batch(session_id, records, meta_patch=None, deadline=None, *, eligible=False):
    from sqlalchemy import text
    from mino_nexus.core.database import SessionLocal, ensure_db
    from mino_nexus.core.execution_user import require_user_id
    from mino_nexus.core.secret_redaction import sanitize
    from mino_nexus.models.session_event import SessionEvent, SessionMeta
    from mino_nexus.services.session_store import _now
    deadline = time.monotonic() + 5 if deadline is None else deadline
    prefix = "eligible" if eligible else "legacy"
    with _lock:
        _counts[prefix + "_append_attempts"] += 1
    db = None
    try:
        if time.monotonic() >= deadline:
            raise TimeoutError("store_deadline_exceeded")
        ensure_db()
        db = SessionLocal()
        remaining_ms = max(1, int((deadline - time.monotonic()) * 1000))
        db.execute(text(f"PRAGMA busy_timeout = {remaining_ms}"))
        db.execute(text("BEGIN IMMEDIATE"))
        meta = db.get(SessionMeta, session_id)
        if meta is None:
            raise ValueError("session_not_open")
        if meta.owner_user_id != require_user_id():
            raise PermissionError("session_owner_mismatch")
        patch = dict(meta_patch or {})
        if set(patch) - {"runtime_context", "verdict", "finalization_id", "status", "summary", "finished_at"}:
            raise ValueError("invalid_meta_patch")
        if meta.finalization_id:
            if patch.get("finalization_id") == meta.finalization_id:
                return list((meta.verdict or {}).get("event_refs") or [])
            raise ValueError("session_already_finalized")
        refs = []
        seq = int(meta.event_count or 0)
        for record in records:
            seq += 1
            db.add(SessionEvent(session_id=session_id, seq=seq, ts=_now(),
                type=str(record["type"]), turn=int(record.get("turn", 0)),
                phase=str(record.get("phase", "")), payload=sanitize(record.get("payload", {}))))
            refs.append({"session_id": session_id, "seq": seq})
        if patch.get("finalization_id"):
            verdict = dict(patch.get("verdict") or {})
            if verdict.get("verification_status") == "verified_pass":
                from mino_nexus.models.run import AppRegressionRun
                task = db.get(AppRegressionRun, meta.run_id)
                if task is not None and (task.payload or {}).get("status") == "cancelled":
                    raise ValueError("task_cancelled")
            if verdict.get("verification_status") not in {"verified_pass", "verified_fail", "unverified"}:
                raise ValueError("invalid_verdict")
            patch["verdict"] = {**verdict, "event_refs": refs}
        for key, value in patch.items():
            setattr(meta, key, sanitize(value))
        meta.event_count = seq
        db.flush()
        if time.monotonic() >= deadline:
            raise TimeoutError("store_deadline_exceeded")
        db.commit()
        return refs
    except Exception:
        if db is not None:
            db.rollback()
        with _lock:
            _counts[prefix + "_store_errors"] += 1
        raise
    finally:
        if db is not None:
            db.close()
