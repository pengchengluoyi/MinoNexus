from collections import defaultdict
from copy import deepcopy

from mino_nexus.core.database import session_scope
from mino_nexus.platform.identity import require_user_id
from mino_nexus.models.session_event import SessionMeta
from mino_nexus.casework import run_store
from mino_nexus.core.stable_digest import digest


def frozen_identity(case):
    from mino_nexus.casework.v5_publication import source_view
    return {"case_revision_id": case.get("source_revision_id") or "", "source_digest": digest(source_view(case)),
        "plan_digest": digest(case.get("v5_publication") or (case.get("meta") or {}).get("step_program_keys") or {})}


def _facts(db, doc, case):
    identity = deepcopy(case.get("frozen_identity") or {})
    session_id = case.get("session_id") or case.get("report_run_id")
    session = db.get(SessionMeta, session_id) if session_id else None
    if session and (session.owner_user_id != require_user_id() or session.run_id != doc["run_id"] or session.case_id != case.get("case_id")):
        session = None
    context = (session.runtime_context or {}) if session else {}
    verdict = (session.verdict or {}) if session else (case.get("verdict") or {})
    build = str(context.get("app_version") or case.get("tested_build") or "")
    verification = str(verdict.get("verification_status") or verdict.get("verification") or verdict.get("verdict") or "unverified")
    return {"execution_unit_id": case.get("execution_unit_id") or "", "case_id": case.get("case_id") or "",
        "sn": case.get("sn") or doc.get("sn") or "", "status": case.get("status") or "unknown",
        "verification": verification, "tested_build": build, "frozen_identity": identity,
        "environment_baseline_digest": doc.get("environment_baseline_digest") or "",
        "platform": (doc.get("platforms_by_sn") or {}).get(case.get("sn")) or doc.get("platform") or "",
        "execution_mode": doc.get("execution_mode") or "", "env_profile": doc.get("env_profile") or "",
        "env_surface": doc.get("env_surface") or ""}


def compare(run_id, other_run_id=""):
    current = run_store.get(run_id)
    other_id = other_run_id or (current or {}).get("derived_from") or ""
    prior = run_store.get(other_id) if other_id else None
    if not current or not prior or current.get("owner_user_id") != require_user_id() or prior.get("owner_user_id") != require_user_id():
        raise ValueError("comparison_run_missing")
    if current.get("app_id") != prior.get("app_id"):
        raise ValueError("comparison_application_mismatch")
    with session_scope() as db:
        previous, latest = defaultdict(list), defaultdict(list)
        for case in prior.get("cases") or []:
            previous[(str(case.get("case_id") or ""), str(case.get("sn") or prior.get("sn") or ""))].append(_facts(db, prior, case))
        for case in current.get("cases") or []:
            latest[(str(case.get("case_id") or ""), str(case.get("sn") or current.get("sn") or ""))].append(_facts(db, current, case))
    items = []
    for key in sorted(set(previous) | set(latest)):
        before, after = previous.get(key, []), latest.get(key, [])
        reasons = []
        if len(before) != 1 or len(after) != 1:
            reasons.append("execution_unit_missing_or_ambiguous")
        else:
            left, right = before[0], after[0]
            for field in ("tested_build", "environment_baseline_digest", "platform", "execution_mode"):
                if not left[field] or not right[field]:
                    reasons.append(field + "_unknown")
                elif left[field] != right[field]:
                    reasons.append(field + "_changed")
            if not left["frozen_identity"] or not right["frozen_identity"]:
                reasons.append("case_identity_unknown")
            elif left["frozen_identity"] != right["frozen_identity"]:
                reasons.append("case_or_plan_changed")
        changed = len(before) == 1 and len(after) == 1 and (before[0]["status"], before[0]["verification"]) != (after[0]["status"], after[0]["verification"])
        items.append({"case_id": key[0], "sn": key[1], "comparable": not reasons, "reason_codes": reasons,
            "observed_change": changed, "inconsistent": changed and not reasons, "previous": before, "current": after})
    return {"run_id": run_id, "compared_to": other_id, "derived_from": current.get("derived_from") or "",
        "original_total": len(prior.get("cases") or []), "current_total": len(current.get("cases") or []),
        "items": items, "inconsistent_units": sum(item["inconsistent"] for item in items)}
