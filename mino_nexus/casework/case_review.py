"""Revision-bound diagnostics/reviews. Human opinions never change execution facts."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import uuid
from sqlalchemy import text
from fastapi import HTTPException
from mino_nexus.core.database import session_scope
from mino_nexus.platform.identity import require_user_id
from mino_nexus.models.case import ProjectCase
from mino_nexus.models.case_review_record import CaseReviewRecord
from mino_nexus.casework.case_compilation import readiness
from mino_nexus.core.stable_digest import digest, now
from mino_nexus.platform.project_guard import owned

CATEGORIES = {"case_definition", "semantic_parsing", "capability_gap", "configuration", "execution_toolchain", "external_evidence"}
CONFIGURATION = {"compile_provider_unavailable", "control_not_published", "control_identity_conflict", "secret_source_unavailable",
    "sensitive_classification_missing", "baseline_missing", "resource_key_missing", "input_resource_missing",
    "target_offline", "target_channels_unknown", "binding_fields_missing", "target_scope_missing",
    "binding_target_missing", "resource_configuration_missing", "binding_requires_publication", "resource_version_missing", "invalid_locator", "invalid_cardinality", "node_offline", "busy", "resource_isolated"}
CAPABILITIES = {"unavailable_cap", "capability_disabled", "uncoverable_event", "platform_unsupported",
    "scheme_unsupported", "action_unmapped", "capability_unavailable", "event_not_registered", "event_disabled",
    "event_platform_unsupported", "implementation_unavailable", "input_combination_unsupported",
    "execution_binding_unsupported", "execution_contract_unsupported", "dependency_contract_unsupported", "unsupported_scope", "unsupported_resource_platform", "unsupported_resource_precondition"}
PARSING = {"layer_mismatch", "ambiguous_clause", "expected_unmapped", "required_check_missing",
    "uncovered", "quote_miss", "unmapped", "precondition_is_action", "required_operation_skipped",
    "compound_clause", "ui_partial"}
EXTERNAL = {"external_evidence_required", "manual_prerequisite_required", "evidence_boundary",
    "fixture_required", "ui_state_precondition", "device_state_setup_required",
    "device_session_setup_required", "fixture_id_required",
    "precondition_not_executable", "precondition_account_field_unavailable"}
TOOLCHAIN = {"catalog_version_changed", "invalid_after_operation", "invalid_operation_identity",
    "invalid_wait_policy", "operation_contract_invalid", "operation_ref_duplicate", "capability_snapshot_invalid",
    "capability_snapshot_stale", "capability_target_mismatch", "dependency_snapshot_invalid", "invalid_predicate",
    "sensitive_predicate_forbidden", "precondition_coverage_missing", "precondition_scene_mismatch"}


def _classification(item):
    code = str(item.get("reason_code") or item.get("reason") or "unmapped")
    if code in CONFIGURATION or (code == "missing_slot" and "credential" in str(item.get("detail"))):
        return "configuration", "configure", "needs_configuration"
    if code in CAPABILITIES:
        return "capability_gap", "platform_fix", "pending_review"
    if code in PARSING:
        return "semantic_parsing", "platform_fix", "pending_review"
    if code in TOOLCHAIN or code.startswith("compile_") or code.endswith("need_compile"):
        return "execution_toolchain", "wait" if code in {"compile_queued", "compile_running"} else "recover_compile" if code in {"compile_model_call_failed", "compile_model_response_incomplete", "compile_failed", "compile_interrupted", "compile_cancelled", "compile_required", "compile_artifact_unavailable"} else "platform_fix", "pending_review"
    if code in {"assertion_scope_missing", "expected_value_missing", "source_information_missing"}:
        return "case_definition", "edit_source", "needs_input"
    if code in EXTERNAL:
        return "external_evidence", "provide_evidence", "pending_review"
    if item.get("responsibility") in {"configuration", "runtime", "authorization", "input"} and item.get("origin") in {"execution_binding", "resource_preflight"}:
        return "configuration", "configure", "pending_review"
    # A failed mapping or a missing compiler parameter is not proof that the
    # business requirement is wrong. Keep the root unconfirmed.
    return None, "review", "pending_review"


def _item(case, raw, origin, context=None):
    code = str(raw.get("reason_code") or raw.get("reason") or "unmapped")
    category, action, status = _classification({**raw, "origin": origin})
    identity = [case.get("source_revision_id") or case.get("v5_draft_revision", 1),
        origin, context or {}, code, raw.get("phase"), raw.get("case_step"),
        raw.get("source_clause"), raw.get("point_ref") or raw.get("point_id"),
        raw.get("operation_ref") or raw.get("operation_id"), raw.get("control_id"), raw.get("detail")]
    if category is None or category == "semantic_parsing":
        message = "需复核系统理解、原句和核验范围；当前诊断不能证明原文有误。"
    elif category == "capability_gap":
        message = "当前动作或核验能力未满足要求，请维护平台能力或确认支持边界。"
    elif category == "execution_toolchain":
        message = "请处理编译任务或执行工具链状态；这不代表产品断言失败。"
    elif category == "configuration":
        message = "请检查目标、环境、控件或资源配置；临时不可用时等待后复检。"
    elif category == "external_evidence":
        message = "需要外部证据或人工前置，请确认核验范围并关联证据。"
    else:
        message = "请补充所标注的业务信息和断言范围，并保留原有要求。"
    if code == "compile_provider_unavailable":
        message = "编译账号没有可用于用例的模型配置，或该配置已停用。请在该账号的模型密钥页启用可用的用例模型，再显式恢复编译；无需修改原文。"
    elif code == "compile_prompt_unavailable":
        message = "编译提示词任务不可用，请维护者检查任务配置后恢复编译；无需修改原文。"
    elif code == "compile_model_call_failed":
        message = "模型调用未返回可用 JSON，请检查模型调度记录后显式恢复编译；无需修改原文。"
    elif code == "compile_span_schema_invalid":
        message = "模型的跨度或参数字段结构不符合编译合同。请维护者核对模型调度记录与提示词后重新编译；无需修改原文。"
    elif code == "compile_model_response_incomplete":
        message = "模型响应被截断或仅恢复了部分 JSON，未激活编译产物。请检查模型输出限制后显式恢复编译；无需修改原文。"
    missing = list(raw.get("missing_fields") or [])
    if not missing and code == "params_incomplete" and raw.get("detail"):
        missing = [str(raw["detail"])]
    if action == "edit_source" and not missing:
        missing = [{"prep": "precondition", "do": "steps_raw", "check": "expected_raw"}.get(raw.get("phase"), "expected_raw")]
    return {"id": digest(identity)[:24], "origin": origin, "reason_code": code,
        "category": category, "responsibility": "unconfirmed", "action": action,
        "phase": str(raw.get("phase") or ""), "case_step": raw.get("case_step"),
        "point_ref": str(raw.get("point_ref") or raw.get("point_id") or ""),
        "operation_ref": str(raw.get("operation_ref") or raw.get("operation_id") or ""),
        "control_id": raw.get("control_id"), "source_clause": str(raw.get("source_clause") or ""),
        "missing_fields": missing, "candidate": deepcopy(raw.get("candidate")), "review_status": status,
        "reviewer_user_id": "", "reviewed_at": "", "source_revision": int(case.get("v5_draft_revision") or 1),
        "revision_id": case.get("source_revision_id", ""), "message": message, "context": deepcopy(context or {})}


def requirements(case):
    """Saved compiler output only; no network/model calls."""
    meta = case.get("meta") or {}
    stored = (meta.get("ui_automation") or {}).get("blocked_by") or []
    # Preconditions are re-read through the deterministic classifier so legacy
    # v1 rows do not keep showing "precondition_not_executable" after the v2
    # setup/source split.
    from mino_nexus.casework.case_semantic_integrity import precondition_diagnostics
    result = [_item(case, row, "compiler") for row in stored
              if isinstance(row, dict) and str(row.get("phase") or "") != "prep"]
    result.extend(_item(case, row, "compiler") for row in precondition_diagnostics(case)
                  if isinstance(row, dict))
    issue = readiness(case)
    if issue:
        diagnostics = (case.get("compile_job") or {}).get("diagnostics") or []
        if issue == "compile_failed" and diagnostics:
            result = [_item(case, {"reason_code": row.get("reason_code") or issue}, "compile_job")
                      for row in diagnostics if isinstance(row, dict)]
        if not result or issue != "compile_failed" or not diagnostics:
            result = [_item(case, {"reason_code": issue}, "compile_job")]
    elif not result:
        automation = meta.get("ui_automation") or {}
        if automation.get("status") == "needs_setup":
            result.append(_item(case, {"reason_code": "fixture_required"}, "compiler"))
        elif automation.get("status") == "partial":
            result.append(_item(case, {"reason_code": "ui_partial"}, "compiler"))
        elif automation.get("coverable") is not True:
            result.append(_item(case, {"reason_code": "compile_required"}, "compiler"))
    return result


def _static(case):
    result = requirements(case)
    return list({item["id"]: item for item in result}.values())


def _state(case):
    from mino_nexus.casework.v5_publication import source_view
    return digest({"source": source_view(case), **{key: case.get(key) for key in ("source_revision_id", "v5_draft_revision", "latest_compile_job_id",
        "compile_state", "resource_key", "effective_artifact", "v5_publication")}})


def _record_view(row):
    return {"record_id": row.record_id, "item_id": row.item_id, "action": row.action,
        "review_status": row.review_status, "source_revision": row.source_revision,
        "revision_id": row.revision_id, "reviewer_user_id": row.owner_user_id,
        "reviewed_at": row.reviewed_at, "reason": row.reason, "evidence": deepcopy(row.evidence), "context": deepcopy(row.context)}


def describe(case):
    from mino_nexus.casework.v5_publication import source_view
    source = source_view(case)
    for field, rows in (("steps_raw", "steps"), ("expected_raw", "expected")):
        if not source.get(field):
            source[field] = "\n".join(case.get(rows) or [])
    items = _static(case)
    with session_scope() as db:
        actor = owned(db, case["project_id"], write=False)
        records = db.query(CaseReviewRecord).filter_by(project_id=case["project_id"], case_id=case["case_id"],
            ).order_by(CaseReviewRecord.reviewed_at, CaseReviewRecord.record_id).all()
        records = [r for r in records if not r.context or r.owner_user_id == actor]
        current = [r for r in records if r.source_revision == int(case.get("v5_draft_revision") or 1)
            and r.revision_id == str(case.get("source_revision_id") or "")]
        snapshots = [r for r in current if r.action == "recheck_snapshot"]
        scoped = {digest(r.context): r for r in snapshots if r.context}
        for observation in scoped.values():
            stale = observation.evidence.get("state_digest") != _state(case) or (datetime.now(timezone.utc) - datetime.fromisoformat(observation.reviewed_at)).total_seconds() > 60
            for item in deepcopy(observation.evidence.get("context_requirements") or []):
                item.update(freshness="stale" if stale else "as_of", observed_at=observation.reviewed_at)
                if stale:
                    item.update(review_status="pending_review", action="recheck")
                items.append(item)
        for item in items:
            matched = [r for r in current if r.item_id == item["id"] and r.action != "recheck_snapshot"]
            if matched:
                last = matched[-1]
                # Reappearing diagnostics reopen after an earlier apparent fix.
                if last.review_status in {"fixed_pending_run", "verified_resolved"}:
                    item["review_status"] = "pending_review"
                else:
                    item.update(review_status=last.review_status, reviewer_user_id=last.owner_user_id, reviewed_at=last.reviewed_at)
                    if last.evidence.get("confirmed_category"):
                        item.update(category=last.evidence["confirmed_category"], responsibility="confirmed_by_reviewer")
        history = [_record_view(r) for r in records if r.action != "recheck_snapshot"]
        latest = {r.item_id: r for r in current if r.item_id != '*' and r.owner_user_id == actor}
        resolved = bool(not items and latest and all(r.review_status == 'verified_resolved'
            and r.evidence.get('state_digest') == _state(case) for r in latest.values()))
        pending_run = any(r.review_status in {'fixed_pending_run', 'verified_resolved'} for r in latest.values())
    return {"project_id": case["project_id"], "case_id": case["case_id"], "owner_user_id": actor,
        "expected_revision": int(case.get("v5_draft_revision") or 1),
        "expected_job_id": case.get("latest_compile_job_id", ""), "source": source,
        "requirements": items, "status": "pending_review" if items else "no_current_diagnostic",
        "compile_job": deepcopy(case.get("compile_job")), "history": history,
        "source_history": deepcopy((case.get("meta") or {}).get("review_history") or []),
        "execution_verified": resolved,
        "resolution_status": 'verified_resolved' if resolved
            else 'fixed_pending_run' if not items and pending_run
            else 'pending_review'}


def sync(case, previous=None):
    result = deepcopy(case)
    meta = dict(result.get("meta") or {})
    meta["review_requirements"] = requirements(result)
    meta["review_history"] = deepcopy(((previous or {}).get("meta") or {}).get("review_history") or [])
    result["meta"] = meta
    return result


def _locked(db, project_id, case_id, expected_revision, expected_job_id=None, state=None):
    from mino_nexus.casework.case_store import _public
    owned(db, project_id)
    row = db.query(ProjectCase).filter_by(project_id=project_id, case_id=case_id).one_or_none()
    if row is None:
        raise ValueError("case_missing")
    case = _public(row)
    if (int(case.get("v5_draft_revision") or 1) != expected_revision
            or expected_job_id is not None and str(case.get("latest_compile_job_id") or "") != expected_job_id
            or state is not None and _state(case) != state):
        raise ValueError("case_revision_conflict")
    return row, case


def _append(db, case, *, item_id, action, status, reason, evidence=None, context=None):
    db.add(CaseReviewRecord(record_id="review-" + uuid.uuid4().hex, owner_user_id=require_user_id(),
        project_id=case["project_id"], case_id=case["case_id"], source_revision=int(case.get("v5_draft_revision") or 1),
        revision_id=str(case.get("source_revision_id") or ""), item_id=item_id, action=action,
        review_status=status, reason=reason, evidence=deepcopy(evidence or {}), context=deepcopy(context or {}), reviewed_at=now()))


def supplement(project_id, case_id, expected_revision, source, reason):
    from mino_nexus.casework.case_store import get_case, _row_fields, _persist_fields
    from mino_nexus.knowledge.control_registry import _assert_runtime_writable
    from mino_nexus.casework.v5_publication import source_view
    _assert_runtime_writable()
    if not reason.strip():
        raise ValueError("review_reason_required")
    old = get_case(project_id, case_id)
    if not old:
        raise ValueError("case_missing")
    candidate = deepcopy(old)
    for field in ("precondition", "steps_raw", "expected_raw"):
        if not isinstance(source.get(field), str):
            raise ValueError("review_source_incomplete")
        candidate[field] = source[field]
    candidate["steps"] = [s.strip() for s in source["steps_raw"].splitlines() if s.strip()]
    candidate["expected"] = [s.strip() for s in source["expected_raw"].splitlines() if s.strip()]
    fields = _row_fields(candidate, project_id=project_id, requirement_id=str(old.get("requirement_id") or ""),
        sort_index=int(old.get("index") or 0), force_recompile_resource_key=candidate["precondition"] != old.get("precondition"))
    with session_scope() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        row, current = _locked(db, project_id, case_id, expected_revision, state=_state(old))
        row = _persist_fields(db, fields, row, origin={"kind": "supplement", "reason": reason.strip(), "reviewer_user_id": require_user_id()})
        from mino_nexus.casework.case_store import _public
        saved = _public(row)
        extra = deepcopy(row.extra)
        meta = dict(extra.get("meta") or {})
        history = deepcopy((current.get("meta") or {}).get("review_history") or [])
        history.append({"source_revision": expected_revision, "new_revision": saved.get("source_revision"),
            "revision_id": saved.get("source_revision_id"), "job_id": saved.get("latest_compile_job_id"),
            "reviewer_user_id": require_user_id(), "reviewed_at": now(), "reason": reason.strip(),
            "before": source_view(current), "after": source_view(candidate), "review_status": "queued"})
        meta["review_history"] = history
        extra["meta"] = meta
        row.extra = extra
    return describe(get_case(project_id, case_id))


def review(project_id, case_id, expected_revision, expected_job_id, item_id, reason, *, category=None):
    from mino_nexus.casework.case_store import get_case
    from mino_nexus.knowledge.control_registry import _assert_runtime_writable
    _assert_runtime_writable()
    if not reason.strip() or category is not None and category not in CATEGORIES:
        raise ValueError("invalid_review_decision")
    case = get_case(project_id, case_id)
    if case is None:
        raise ValueError("case_missing")
    item = next((x for x in describe(case)["requirements"] if x["id"] == item_id), None)
    if item is None:
        raise ValueError("review_item_missing")
    with session_scope() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        _, current = _locked(db, project_id, case_id, expected_revision, expected_job_id, _state(case))
        _append(db, current, item_id=item_id, action="confirm", status="confirmed" if category else "pending_review",
            reason=reason.strip(), evidence={"diagnostic": item, "confirmed_category": category}, context=item.get("context"))
    return describe(get_case(project_id, case_id))


def _context_checks(case, context):
    from mino_nexus.resources.access_scope import require_owned_device
    from mino_nexus.casework.execution_binding_runtime import binding_target_config, resolve_live
    from mino_nexus.casework.execution_requirements import ordinary_requirements
    from mino_nexus.resources.node_registry import get_registry
    app_id, sn = context.get("app_id", ""), context.get("sn", "")
    try:
        binding_target_config(case["project_id"], app_id)
    except ValueError as exc:
        raise ValueError("review_target_missing") from exc
    require_owned_device(sn)
    try:
        snapshot = get_registry().execution_snapshot(sn)
        if not snapshot:
            return [_item(case, {"reason_code": "binding_target_unavailable", "responsibility": "runtime"}, "execution_binding", context)]
        requirements = ordinary_requirements(case, app_id, platform=snapshot["device"]["platform"],
            action_scheme=context.get("action_scheme") or "dom_preferred", provider_id=context.get("provider_id") or "")
        result = resolve_live(requirements, context)
        # The shared live resolver includes actual resource/model/OTP/environment
        # dependency facts. It never leases resources or invokes an executor.
        return [_item(case, row, "resource_preflight" if row.get("dependency_ref") or str(row.get("reason_code") or "").startswith("dependency_") else "execution_binding", context) for row in result.diagnostics]
    except (ValueError, KeyError) as exc:
        diagnostics = getattr(exc, "diagnostics", None) or [{"reason_code": getattr(exc, "reason_code", "binding_context_invalid"), "responsibility": "configuration"}]
        return [_item(case, row, "execution_binding", context) for row in diagnostics]


def recheck(project_id, case_id, expected_revision, expected_job_id, reason, *, context=None, evidence_session_id=""):
    from mino_nexus.casework.case_store import get_case
    from mino_nexus.knowledge.control_registry import _assert_runtime_writable
    _assert_runtime_writable()
    if not reason.strip():
        raise ValueError("review_reason_required")
    case = get_case(project_id, case_id)
    if case is None:
        raise ValueError("case_missing")
    previous = describe(case)["requirements"]
    with session_scope() as db:
        actor = owned(db, project_id)
        records = db.query(CaseReviewRecord).filter_by(project_id=project_id, case_id=case_id, owner_user_id=actor,
            source_revision=int(case.get("v5_draft_revision") or 1), revision_id=str(case.get("source_revision_id") or "")).order_by(CaseReviewRecord.reviewed_at).all()
        review_ids = {r.record_id for r in records}
        pending_boundaries = [r.reviewed_at for r in records
            if r.review_status in {"pending_review", "confirmed", "needs_input", "needs_configuration"}]
        last_by_item = {r.item_id: r for r in records if r.item_id != "*"}
        for record in last_by_item.values():
            if record.evidence.get("diagnostic") and record.review_status not in ({"verified_resolved"} if evidence_session_id else {"fixed_pending_run", "verified_resolved"}):
                item = record.evidence["diagnostic"]
                if not any(r["id"] == item["id"] for r in previous):
                    previous.append(deepcopy(item))
    context = {k: str(v) for k, v in (context or {}).items() if k in {"app_id", "sn", "env_profile", "env_surface", "action_scheme", "provider_id"}}
    if context:
        context = {"env_profile": "", "env_surface": "", "action_scheme": "dom_preferred", "provider_id": "", **context}
    contextual = _context_checks(case, context) if context else []
    current = _static(case) + contextual
    with session_scope() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        _, current_case = _locked(db, project_id, case_id, expected_revision, expected_job_id, _state(case))
        locked_reviews = db.query(CaseReviewRecord).filter_by(project_id=project_id, case_id=case_id, owner_user_id=actor,
            source_revision=int(current_case.get("v5_draft_revision") or 1), revision_id=str(current_case.get("source_revision_id") or "")).all()
        if {r.record_id for r in locked_reviews} != review_ids:
            raise ValueError('review_revision_conflict')
        verified = False
        if evidence_session_id:
            from mino_nexus.models.session_event import SessionMeta
            session = db.get(SessionMeta, evidence_session_id)
            from mino_nexus.casework.review_execution_evidence import validate
            evidence_proof = validate(db, session, case=current_case, actor=require_user_id(),
                context=context, boundaries=[(case.get('v5_publication') or {}).get('published_at') or '', *pending_boundaries])
            verified = True
        _append(db, current_case, item_id="*", action="recheck_snapshot", status="pending_review" if current else "fixed_pending_run",
            reason=reason.strip(), context=context, evidence={"context_requirements": contextual, "state_digest": _state(case),
                "checked_item_ids": [r["id"] for r in current], "verification_kind": "read_only_preflight", "execution_verified": False})
        for item in previous:
            # An omitted target context cannot clear target-specific diagnostics.
            if item.get("context") and item["context"] != context:
                continue
            active = next((x for x in current if x["id"] == item["id"]), None)
            _append(db, current_case, item_id=item["id"], action="recheck",
                status="pending_review" if active else "verified_resolved" if verified else "fixed_pending_run",
                reason=reason.strip(), context=context, evidence={"diagnostic": item,
                    "still_present": bool(active), "execution_verified": verified, "session_id": evidence_session_id,
                    "proof": evidence_proof if verified else None, "state_digest": _state(current_case)})
    return describe(get_case(project_id, case_id))
