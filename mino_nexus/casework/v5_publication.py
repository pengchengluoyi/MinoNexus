"""Review/freeze existing text-compiled cases; no handwritten plan entrance."""
from __future__ import annotations

import copy
from datetime import datetime

from mino_nexus.contract.web_v5_contract import validate_expected, validate_locator

from mino_nexus.contract.case_view import SOURCE_FIELDS, source_view  # noqa: F401,E402


def guard_case_row(row):
    """Server-owned publications survive every save/import/compiled bypass."""
    from mino_nexus.casework.case_store import get_case
    out = copy.deepcopy(row)
    pid, cid = str(out.get("project_id") or ""), str(out.get("case_id") or "")
    old = get_case(pid, cid) if pid and cid else None
    for key in ("v5_publication", "v5_plan_state", "v5_draft_revision"):
        out.pop(key, None)
    if old:
        publication = old.get("v5_publication")
        revision = int(old.get("v5_draft_revision") or 1)
        changed = source_view(old) != source_view(out)
        out["v5_draft_revision"] = revision + int(changed)
        if publication:
            out["v5_publication"] = copy.deepcopy(publication)
            # Historical evidence is retained without controlling current execution.
    else:
        out["v5_draft_revision"] = 1
    return out


def _expected(check):
    if check.get("expected"):
        return copy.deepcopy(check["expected"])
    payload = check.get("assert") or {}
    rules = payload.get("rules") or {}
    if check.get("key_ref") == "expected.ui_absent" and (payload.get("element") or {}).get("name"):
        return {"predicate": "not_visible"}
    if (check.get("key_ref") == "expected.ui_copy" and rules.get("text_match") == "exact"
            and rules.get("match") == "equals"):
        literals = [rules[key] for key in ("title", "body") if rules.get(key) is not None]
        if len(literals) == 1 and isinstance(literals[0], str):
            return {"predicate": "text_equals", "value": literals[0]}
    rule = str(rules.get("rule") or "")
    if rule in {"visible", "not_visible", "secret_nonempty"}:
        return {"predicate": rule}
    if rule == "exists":
        return {"predicate": "visible"}
    if rule in {"count_equals", "secret_length_matches"}:
        return {"predicate": rule, "value": rules.get("count" if rule == "count_equals" else "length")}
    if rule in {"text_equals", "value_equals"}:
        return {"predicate": rule, "value": rules.get("text" if rule == "text_equals" else "value")}
    if rule == "equals" and check.get("assert_type") in {"text", "value", "count"}:
        kind = check["assert_type"]
        value = rules.get(kind, rules.get("expected_value"))
        return {"predicate": {"text": "text_equals", "value": "value_equals", "count": "count_equals"}[kind], "value": value}
    raise ValueError("expected_unmapped")


def preview(case, *, compile_draft=False):
    from mino_nexus.casework.case_step_key_compiler import compile_case_step_program_keys
    from mino_nexus.casework.v5_controls import list_controls, audit
    from mino_nexus.casework.case_step_key_compiler import _block_dependencies
    pid = str(case.get("project_id") or "")
    meta = case.get("meta") or {}
    compiled = compile_case_step_program_keys(case) if compile_draft else {
        "step_program_keys": meta.get("v5_draft_step_program_keys", meta.get("step_program_keys", {})),
        "key_compile_warnings": meta.get("key_compile_warnings", [])}
    errors, actions, checks, controls = [], [], [], {}
    from mino_nexus.casework.case_compilation import readiness
    issue = readiness(case) if not compile_draft else None
    if issue:
        errors.append({"reason_code": issue})
    from mino_nexus.casework.v5_capabilities import snapshot as capability_snapshot
    try:
        capabilities = capability_snapshot()
    except ValueError as exc:
        capabilities = {}
        errors.append({"reason_code": str(exc)})
    identities = {x["control_id"]: x for x in list_controls(pid)}
    duplicate_ids = {x["control_id"] for x in audit(pid)}
    if compiled.get("key_compile_warnings"):
        errors.append({"reason_code": "unresolved_compile_warning"})
    resource = case.get("resource_key")
    from mino_nexus.casework.v5_resources import validate as validate_resource, validate_input_ref
    baseline = {}
    try:
        baseline = validate_resource(resource)
    except ValueError as exc:
        errors.append({"reason_code": str(exc)})
    bundles = compiled.get("step_program_keys") or {}
    for step_number, bundle in sorted(bundles.items(), key=lambda x: int(x[0])):
        do = bundle.get("do_program_plan") or {}
        ck = bundle.get("check_program_plan") or {}
        if do.get("block_dependencies") != _block_dependencies(do, str(case.get("app_id") or pid)):
            errors.append({"reason_code": "catalog_version_changed", "step": step_number})
        last_id = ""
        for operation in do.get("steps") or []:
            cid = operation.get("control_id")
            action_type = operation.get("action_type")
            if action_type not in {"click", "input", "check"} or operation.get("kind") == "flow_block":
                errors.append({"reason_code": "action_unmapped", "operation_id": operation.get("id")})
                continue
            oid = str(operation.get("id") or "")
            last_id = oid
            params = operation.get("params") or {}
            rules = params.get("rules") or {}
            payload = {"text": params.get("text", rules.get("text", rules.get("value", "")))} if action_type == "input" else {}
            if action_type == "input" and not any(k in params for k in ("text",)) and not any(k in rules for k in ("text", "value", "resource_ref")):
                errors.append({"reason_code": "input_payload_unmapped", "operation_id": oid})
            if "resource_ref" in rules:
                payload = {"resource_ref": copy.deepcopy(rules["resource_ref"])}
                try:
                    validate_input_ref(pid, payload["resource_ref"])
                except ValueError as exc:
                    errors.append({"reason_code": str(exc), "operation_id": oid})
            actions.append({"operation_id": oid, "case_step": int(step_number), "control_id": cid,
                            "capability_id": "input_target" if action_type == "input" else "tap_target", "payload": payload})
        for point in ck.get("checkpoints") or []:
            try:
                expected = _expected(point)
                cid = point.get("control_id")
                validate_expected(expected, sensitive=bool(identities.get(cid, {}).get("sensitive")))
                from mino_nexus.knowledge.check_observation_policy import freeze_policy
                wait = freeze_policy(expected, point.get("wait_policy"))
                if not 1 <= int(wait.get("max_reads", 0)) <= 3 or not 1 <= int(wait.get("timeout_ms", 0)) <= 3000:
                    raise ValueError("invalid_wait_policy")
                checks.append({"point_id": str(point.get("point_id") or point.get("id") or ""),
                    "control_id": cid, "expected": expected, "after_operation_id": point.get("after_operation_id") or last_id,
                    "required": not bool(point.get("optional")), "wait_policy": wait,
                    "verifier_kind": "dom", "predicate_contract": "web.atomic.v1", "case_step": int(step_number)})
            except (ValueError, TypeError) as exc:
                errors.append({"reason_code": str(exc), "point_id": point.get("id")})
    for row in actions + checks + ([baseline] if baseline else []):
        cid = row.get("control_id")
        control = identities.get(cid)
        if cid in duplicate_ids:
            errors.append({"reason_code": "control_identity_conflict", "control_id": cid})
        elif not control or control.get("publish_state") != "published":
            errors.append({"reason_code": "control_not_published", "control_id": cid})
        else:
            try:
                validate_locator(control.get("locator_spec"))
                controls[cid] = control
                if row in actions and control["locator_spec"]["playwright"]["cardinality"] != "one":
                    raise ValueError("invalid_cardinality")
                if row in actions and row.get("payload", {}).get("resource_ref", {}).get("field") in {"password", "otp"} and not control.get("sensitive"):
                    raise ValueError("sensitive_classification_missing")
                if row in actions and row["capability_id"] == "input_target" and control.get("sensitive"):
                    if "resource_ref" not in row["payload"]:
                        raise ValueError("secret_source_unavailable")
            except (ValueError, TypeError) as exc:
                errors.append({"reason_code": str(exc), "control_id": cid})
    ids = [x["operation_id"] for x in actions]
    points = [x["point_id"] for x in checks]
    if not actions or len(ids) != len(set(ids)) or "" in ids:
        errors.append({"reason_code": "invalid_operation_identity"})
    if not any(x["required"] for x in checks) or len(points) != len(set(points)) or "" in points:
        errors.append({"reason_code": "required_check_missing"})
    if any(x["after_operation_id"] not in ids for x in checks):
        errors.append({"reason_code": "invalid_after_operation"})
    check_plan = {"schema": "mino.check_program_plan.v2", "plan_revision": int(case.get("v5_draft_revision") or 1), "checkpoints": checks}
    return {"eligible": not errors, "reason_code": "" if not errors else "v5_ineligible", "errors": errors,
            "actions": actions, "check_program_plan": check_plan, "controls": controls,
            "draft_revision": int(case.get("v5_draft_revision") or 1), "source": source_view(case),
            "source_revision_id": case.get("source_revision_id"), "source_revision": case.get("source_revision"),
            "compile_job_id": case.get("latest_compile_job_id"), "compile_job": copy.deepcopy(case.get("compile_job")),
            "effective_artifact": copy.deepcopy(case.get("effective_artifact")),
            "resource_key": copy.deepcopy(resource), "compiled_keys": compiled.get("step_program_keys", {}),
            "capabilities": capabilities,
            "baseline": baseline,
            "baseline_controls": [{"control_id": c["control_id"], "target_desc": c["target_desc"]}
                for c in identities.values() if c.get("publish_state") == "published" and c.get("locator_spec")],
            "provenance": {"compiler": "case-step-compile", "verifier_build": "web-atomic-1",
                "plan_source": "text", "dispatch_log_refs": meta.get("compile_provenance", []),
                "catalog_versions": {n: {"do": b.get("do_program_plan", {}).get("catalog_version"),
                    "check": b.get("check_program_plan", {}).get("catalog_version"),
                    "compile_contract_revision": b.get("do_program_plan", {}).get("compile_contract_revision")}
                    for n, b in bundles.items()}}}


def publish(project_id, case_id, expected_revision, *, retry_policy=None, reversible=False, expected_compile_job_id=""):
    from mino_nexus.knowledge.control_registry import _assert_runtime_writable
    _assert_runtime_writable()
    from mino_nexus.platform.identity import require_user_id
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.case import ProjectCase
    from mino_nexus.casework.case_store import get_case
    from sqlalchemy import text
    actor = require_user_id()
    case = get_case(project_id, case_id)
    if not case:
        raise ValueError("case_missing")
    # A revision can be recompiled without changing its source number. Pin the
    # exact job the reviewer saw, not whichever job is current on POST arrival.
    if case.get("source_revision_id") and (not expected_compile_job_id
            or case.get("latest_compile_job_id") != expected_compile_job_id):
        raise ValueError("compile_revision_conflict")
    reviewed = preview(case)
    if not reviewed["eligible"]:
        raise ValueError("v5_ineligible")
    policy = retry_policy or {"on": [], "max": 0}
    if (not isinstance(policy, dict) or type(policy.get("max")) is not int
            or not (policy == {"on": [], "max": 0} or
                    policy in ({"on": ["not_started"], "max": 1}, {"on": ["not_started"], "max": 2}))):
        # Unknown effects still require a reset proof; reversible is not evidence.
        raise ValueError("retry_reset_verification_required")
    with session_scope() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        row = db.query(ProjectCase).filter_by(project_id=project_id, case_id=case_id).one()
        extra = dict(row.extra or {})
        if int(extra.get("v5_draft_revision") or 1) != int(expected_revision):
            raise ValueError("case_revision_conflict")
        if source_view({**extra, "name": row.name, "platform": row.platform, "precondition": row.precondition,
                        "steps": row.steps, "expected": row.expected, "steps_raw": row.steps_raw, "expected_raw": row.expected_raw}) != reviewed["source"]:
            raise ValueError("case_revision_conflict")
        if extra.get("resource_key") != reviewed["resource_key"]:
            raise ValueError("resource_revision_conflict")
        from mino_nexus.casework.case_compilation import readiness
        if extra.get("source_revision_id") and (readiness(extra)
                or extra.get("latest_compile_job_id") != reviewed.get("compile_job_id")
                or extra.get("source_revision_id") != reviewed.get("source_revision_id")
                or extra.get("effective_artifact") != reviewed.get("effective_artifact")):
            raise ValueError("compile_revision_conflict")
        current_meta = extra.get("meta") or {}
        current_keys = current_meta.get("v5_draft_step_program_keys", current_meta.get("step_program_keys", {}))
        if current_keys != reviewed["compiled_keys"]:
            raise ValueError("case_revision_conflict")
        from mino_nexus.casework.v5_capabilities import snapshot as capability_snapshot
        if capability_snapshot() != reviewed["capabilities"]:
            raise ValueError("catalog_revision_conflict")
        from mino_nexus.models.ui_control import UiControl
        from mino_nexus.knowledge.control_registry import _public
        for cid, snapshot in reviewed["controls"].items():
            controls = db.query(UiControl).filter_by(project_id=project_id, control_id=cid).all()
            if len(controls) != 1 or _public(controls[0]) != snapshot:
                raise ValueError("control_revision_conflict")
        reviewed.update(frozen_revision=int((extra.get("v5_publication") or {}).get("frozen_revision") or 0) + 1,
            published_by=actor, published_at=datetime.now().astimezone().isoformat(), retry_policy=policy, reversible=bool(reversible))
        # Ordinary consumers and the new frozen publication must share this
        # compiled revision; retaining the previous plan would mix old actions
        # with newly reviewed source after a supplement.
        published_meta = dict(extra.get("meta") or {})
        published_meta["step_program_keys"] = copy.deepcopy(reviewed["compiled_keys"])
        extra["meta"] = published_meta
        extra.update(v5_publication=reviewed, v5_plan_state="published", v5_has_unpublished_changes=False)
        row.extra = extra
    return get_case(project_id, case_id)


def configure_baseline(project_id, case_id, expected_revision, control_id):
    from mino_nexus.knowledge.control_registry import _assert_runtime_writable
    from mino_nexus.platform.identity import require_user_id
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.case import ProjectCase
    from mino_nexus.models.ui_control import UiControl
    from mino_nexus.casework.v5_resources import validate as validate_resource
    from mino_nexus.casework.case_store import get_case
    from sqlalchemy import text
    _assert_runtime_writable()
    actor = require_user_id()
    with session_scope() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        row = db.query(ProjectCase).filter_by(project_id=project_id, case_id=case_id).one_or_none()
        if row is None:
            raise ValueError("case_missing")
        from mino_nexus.casework.case_store import _public
        previous = _public(row)
        extra = copy.deepcopy(row.extra or {})
        if int(extra.get("v5_draft_revision") or 1) != expected_revision:
            raise ValueError("case_revision_conflict")
        controls = db.query(UiControl).filter_by(project_id=project_id, control_id=control_id).all()
        if len(controls) != 1 or controls[0].publish_state != "published":
            raise ValueError("control_not_published")
        validate_locator(controls[0].locator_spec)
        resource = copy.deepcopy(extra.get("resource_key") or {})
        resource["v5_baseline"] = {"control_id": control_id}
        validate_resource(resource)
        extra.update(resource_key=resource, v5_draft_revision=expected_revision + 1)
        meta = dict(extra.get("meta") or {})
        meta["resource_key"] = resource
        extra["meta"] = meta
        if extra.get("v5_publication"):
            extra["v5_plan_state"] = "needs_revalidate"
        extra["v5_baseline_updated_by"] = actor
        row.extra = extra
        row.updated_at = datetime.now().astimezone().isoformat()
        from mino_nexus.casework.case_compilation import record_change
        record_change(db, row, previous, origin={"kind": "baseline_configuration", "control_id": control_id})
    return get_case(project_id, case_id)
