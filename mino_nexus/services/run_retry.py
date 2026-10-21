"""Evidence-based retry admission. Unknown effects never enter this path."""
from copy import deepcopy
import math
import time
from mino_nexus.core.execution_user import require_user_id

NO_EFFECT_CONTRACT = "execution.no_effect.v1"
FAILED = {"fail", "blocked", "declined", "product_fail", "env_blocked", "tool_blocked", "unverified"}


def _remaining(value, now):
    return max(0.0, value - now) if type(value) in (int, float) and math.isfinite(value) and value > 0 else None


def assess(doc, *, resources, now=None, proofs=None, consumed=()):
    from mino_nexus.services.run_write_safety import unresolved
    now = time.time() if now is None else now
    blockers = []
    def block(code, message):
        blockers.append({"reason_code": code, "message": message})
    if doc.get("status") not in {"done", "completed", "passed", "failed", "cancelled", "pass", "fail"}:
        block("task_not_terminal", "原任务尚未结束或终态不可确认")
    if unresolved(doc):
        block("write_outcome_unresolved", "仍有未结算或隔离的写动作；停止确认不等于复位")
    if any(row.get("state") != "released" for row in resources):
        block("resource_not_released", "原任务仍持有或隔离资源")
    remaining = _remaining((doc.get("execution_policy") or {}).get("batch_deadline_at"), now)
    if remaining is None:
        block("retry_budget_unavailable", "缺少有效原预算，不能为复跑重新计时")
    elif remaining == 0:
        block("batch_deadline_exceeded", "原批次预算已耗尽")
    units = []
    for case in doc.get("cases") or []:
        if case.get("status") not in FAILED:
            continue
        uid, reasons = case.get("execution_unit_id") or "", []
        # Retry admission is evidence-based, not a publication/version entitlement.
        policy = {"max": 1, "on": ["not_started"]}
        count, maximum = case.get("retry_count", 0), policy.get("max", 0)
        if not uid:
            reasons.append("execution_unit_identity_missing")
        if uid in consumed:
            reasons.append("execution_unit_already_retried")
        if type(maximum) is not int or maximum not in {1, 2} or policy.get("on") != ["not_started"]:
            reasons.append("publication_retry_not_allowed")
        elif type(count) is not int or count < 0 or count >= maximum:
            reasons.append("retry_limit_exceeded")
        attempt_left = _remaining(case.get("attempt_deadline_at"), now)
        if attempt_left is None:
            reasons.append("attempt_budget_unavailable")
        elif attempt_left == 0:
            reasons.append("attempt_deadline_exceeded")
        if not (proofs or {}).get(uid):
            reasons.append("no_effect_evidence_missing")
        units.append({"execution_unit_id": uid, "case_id": case.get("case_id") or "",
            "sn": case.get("sn") or doc.get("sn") or "", "status": case.get("status"),
            "eligible": not blockers and not reasons, "reason_codes": reasons,
            "retry_count": count, "published_retry_limit": maximum, "remaining_attempt_seconds": attempt_left})
    if not units:
        block("no_failed_execution_units", "没有失败执行单元可复跑")
    eligible = [u["execution_unit_id"] for u in units if u["eligible"]]
    return {"task_id": doc.get("run_id") or "", "eligible": bool(eligible), "eligible_unit_ids": eligible,
        "reason_code": blockers[0]["reason_code"] if blockers else ("" if eligible else "no_eligible_execution_units"),
        "blockers": blockers, "remaining_budget_seconds": remaining, "execution_units": units,
        "business_result_unchanged": True}


def _inspect(db, doc):
    from sqlalchemy import func
    from mino_nexus.models.execution_resource import ExecutionResource
    from mino_nexus.models.device_reservation import DeviceReservation
    from mino_nexus.models.run import AppRegressionRun
    from mino_nexus.models.session_event import SessionMeta
    resources = [{"state": r.state} for r in db.query(ExecutionResource).filter_by(run_id=doc["run_id"]).all()]
    resources += [{"state": "held"} for _ in db.query(DeviceReservation).filter_by(run_id=doc["run_id"]).all()]
    proofs = {}
    for case in doc.get("cases") or []:
        sid = case.get("session_id")
        session = db.get(SessionMeta, sid) if sid else None
        context = (session.runtime_context or {}) if session else {}
        proof = context.get("retry_safety") or {}
        proofs[case.get("execution_unit_id")] = bool(session and session.owner_user_id == doc["owner_user_id"]
            and session.run_id == doc["run_id"] and session.case_id == case.get("case_id")
            and session.finished_at and session.finalization_id == sid
            and context.get("execution_unit_id") == case.get("execution_unit_id")
            and context.get("sn") == case.get("sn") and context.get("executor_stopped") is True
            and proof == {"contract": NO_EFFECT_CONTRACT, "write_dispatch_attempted": False})
    children = db.query(AppRegressionRun).filter(AppRegressionRun.owner_user_id == doc["owner_user_id"],
        func.json_extract(AppRegressionRun.payload, '$.derived_from') == doc["run_id"]).all()
    consumed = {c.get("retry_of_execution_unit_id") for child in children for c in (child.payload or {}).get("cases") or []}
    return assess(doc, resources=resources, proofs=proofs, consumed=consumed)


def eligibility(task_id):
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.run import AppRegressionRun
    with session_scope() as db:
        row = db.get(AppRegressionRun, task_id)
        if row is None or row.owner_user_id != require_user_id():
            raise KeyError("task_not_found")
        return _inspect(db, row.payload)


def validate_admission(db, doc):
    """Recheck under the same SQLite write transaction as receipt and run creation."""
    from mino_nexus.models.run import AppRegressionRun
    parent = db.get(AppRegressionRun, doc.get("derived_from"))
    if parent is None or parent.owner_user_id != doc.get("owner_user_id"):
        raise ValueError("retry_parent_missing")
    assessment = _inspect(db, parent.payload)
    wanted = [c.get("retry_of_execution_unit_id") for c in doc.get("cases") or []]
    if not wanted or len(set(wanted)) != len(wanted) or not set(wanted) <= set(assessment["eligible_unit_ids"]):
        raise ValueError(assessment["reason_code"] or "retry_selection_not_eligible")
    if doc.get("execution_policy") != parent.payload.get("execution_policy"):
        raise ValueError("retry_budget_changed")
    for field in ("environment_baseline_digest", "package", "env_profile", "env_surface"):
        if doc.get(field) != parent.payload.get(field):
            raise ValueError("retry_context_changed")
    originals = {c["execution_unit_id"]: c for c in parent.payload["cases"]}
    for case in doc["cases"]:
        old = originals[case["retry_of_execution_unit_id"]]
        if any(case.get(field) != old.get(field) for field in (
                "case_id", "sn", "v5_publication", "resource_key", "frozen_identity", "attempt_deadline_at")):
            raise ValueError("retry_snapshot_changed")
        if case.get("retry_count") != old.get("retry_count", 0) + 1 or case.get("retry_of") != old.get("session_id"):
            raise ValueError("retry_lineage_changed")


def create(task_id, unit_ids, request_id):
    from mino_nexus.services import run_store, project_store
    from mino_nexus.services.case_compilation import digest
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.run_admission import RunAdmission
    from mino_nexus.models.run import AppRegressionRun
    from mino_nexus.services.access_scope import require_owned_device
    from mino_nexus.services.device_targets import select
    if not request_id or len(request_id) > 160 or not all(c.isalnum() or c in "_.:-" for c in request_id):
        raise ValueError("retry_request_id_required")
    if not unit_ids or len(unit_ids) != len(set(unit_ids)):
        raise ValueError("retry_execution_unit_selection_required")
    actor = require_user_id()
    request_digest = digest({"operation": "retry", "parent": task_id, "units": sorted(unit_ids)})
    with session_scope() as db:
        receipt = db.query(RunAdmission).filter_by(owner_user_id=actor, request_id=request_id).one_or_none()
        if receipt:
            if receipt.request_digest != request_digest:
                raise ValueError("run_request_conflict")
            existing = db.get(AppRegressionRun, receipt.run_id)
            if existing is None:
                raise ValueError("run_receipt_target_missing")
            return deepcopy(existing.payload), False
        parent = db.get(AppRegressionRun, task_id)
        if parent is None or parent.owner_user_id != actor:
            raise KeyError("task_not_found")
        old = deepcopy(parent.payload)
        assessment = _inspect(db, old)
        if not set(unit_ids) <= set(assessment["eligible_unit_ids"]):
            raise ValueError(assessment["reason_code"] or "retry_selection_not_eligible")
    selected = [c for c in old["cases"] if c.get("execution_unit_id") in unit_ids]
    sns = list(dict.fromkeys(c["sn"] for c in selected))
    for sn in sns:
        require_owned_device(sn)
    app = project_store.require_app(old["app_id"])
    _, targets = select(app, sns, surface=old.get("env_surface", ""), profile=old.get("env_profile", ""),
                        headless=True, action_scheme=old.get("action_scheme", "dom_preferred"), allow_queue=True)
    identity_fields = ("sn", "node_id", "platform", "os", "target_surface", "target_platform")
    if any(any(targets[sn].get(key) != ((old.get("device_targets") or {}).get(sn) or {}).get(key)
               for key in identity_fields) for sn in sns):
        raise ValueError("retry_target_changed")
    from mino_nexus.services import app_automation
    env = project_store.project_env(str(app.get("project_id") or ""))
    package = app_automation.package_for_app(app, env_profile=old.get("env_profile", ""),
        platform=old.get("platform") or targets[sns[0]]["platform"], surface=old.get("env_surface", ""))
    baseline = digest({"profile": old.get("env_profile", ""), "surface": old.get("env_surface", ""),
        "package": package, "environment": (env.get("profiles") or {}).get(old.get("env_profile")),
        "channels": env.get("channels") or []})
    if baseline != old.get("environment_baseline_digest") or package != old.get("package"):
        raise ValueError("retry_environment_changed")
    rid, cases = run_store.new_run_id(), []
    for index, case in enumerate(selected):
        seeded = run_store.seed_case(rid, case, index, sn=case["sn"], coverage=old.get("coverage", "once"))
        seeded.update(frozen_identity=deepcopy(case.get("frozen_identity") or {}),
            retry_of_execution_unit_id=case["execution_unit_id"], retry_of=case["session_id"],
            retry_count=case.get("retry_count", 0) + 1, attempt_deadline_at=case["attempt_deadline_at"])
        cases.append(seeded)
    doc = {key: deepcopy(old[key]) for key in ("app_id", "app_name", "execution_mode", "execution_policy",
        "environment_baseline_digest", "package", "playbook", "provider_id", "provider_name", "model_name",
        "playwright_headless", "env_profile", "env_surface", "action_scheme", "coverage", "platform", "run_type",
        "requirement_id", "release_id", "slot_id", "engine") if key in old}
    doc.update(run_id=rid, task_id=rid, owner_user_id=actor, plugin_user_id=actor, execution_mode="agent", engine="agent",
        client_request_id=request_id, request_digest=request_digest, derived_from=task_id, retry_contract="no_effect.v1",
        sn=sns[0], sns=sns, device_targets=targets, platforms_by_sn={sn: old["platforms_by_sn"][sn] for sn in sns},
        cases=cases, requested_total=len(cases), total=len(cases), skipped_cases=[],
        completed=0, passed=0, failed=0, status="running", started_at=run_store._now(), finished_at=None, error="")
    persisted = run_store.put(doc)
    return persisted, persisted["run_id"] == rid
