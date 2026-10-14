"""Observed counts and explicit denominators; never infer labels from summaries."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone


def weekly(project_id, *, days=7):
    from mino_nexus.core.database import session_scope
    from mino_nexus.core.execution_user import require_user_id
    from mino_nexus.models.session_event import SessionMeta, SessionEvent
    from mino_nexus.models.project import App
    from mino_nexus.services.session_atomic import counters
    cutoff = (datetime.now().astimezone() - timedelta(days=days)).isoformat()
    with session_scope() as db:
        app_ids = [row[0] for row in db.query(App.id).filter_by(project_id=project_id)]
        sessions = db.query(SessionMeta).filter(SessionMeta.owner_user_id == require_user_id(),
            SessionMeta.app_id.in_(app_ids), SessionMeta.started_at >= cutoff).all()
        sessions = [s for s in sessions if (s.runtime_context or {}).get("execution_mode") == "v5"
                    and (s.runtime_context or {}).get("purpose") != "control_probe"]
        reasons, verification, links = Counter(), Counter(), []
        writes = unknown = 0
        ids = [s.session_id for s in sessions]
        for event in db.query(SessionEvent).filter(SessionEvent.session_id.in_(ids), SessionEvent.type == "device/action"):
            writes += 1
            unknown += int((event.payload or {}).get("raw_response", {}).get("execution", {}).get("reason_code") == "effect_unknown")
        for session in sessions:
            verdict = session.verdict or {}
            reasons[verdict.get("reason_code") or "none"] += 1
            verification[verdict.get("verification_status") or "unverified"] += 1
            links.append({"session_id": session.session_id, "verdict": verdict, "finished_at": session.finished_at})
        complete = sum(bool(s.finalization_id and (s.verdict or {}).get("event_refs")) for s in sessions
                       if (s.verdict or {}).get("verification_status") == "verified_pass")
        passed = verification["verified_pass"]
        return {"days": days, "attempts": len(sessions), "by_reason": dict(reasons), "by_verification": dict(verification),
            "flaky_passes": sum(bool((s.verdict or {}).get("flaky")) for s in sessions),
            "effect_unknown": {"count": unknown, "denominator": writes},
            "evidence_complete": {"count": complete, "denominator": passed}, "sessions": links,
            "storage": {"scope": "current_process_all_projects", **counters()},
            "positive_false_reject": {"count": None, "denominator": None, "reason": "requires_curated_labels"},
            "secret_leaks": {"count": None, "reason": "requires_sentinel_acceptance"}}
