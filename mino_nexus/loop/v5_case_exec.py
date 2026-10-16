"""Deterministic execution of reviewed text plans. Runtime has no model calls."""
from __future__ import annotations

import copy
import threading
import time
import uuid

from mino_nexus.core.schemas import PlanEvent, EventStatus
from mino_nexus.core.secret_redaction import register_inputs
from mino_nexus.loop.router_proxy import RouterProxy
from mino_nexus.loop.step_pointer import compute_case_wall_budget_sec
from mino_nexus.services import session_store
from mino_nexus.services.control_registry import registry_runtime, get_control
from mino_nexus.services.session_atomic import append_batch
from mino_nexus.services.v5_execution import ExecutionControl
from mino_nexus.services.web_v5_contract import CONTRACTS, PROFILE, evaluate, requested_facts


def _input_payload(payload, project_id, attempt_id, sn, *, deadline):
    if "resource_ref" not in payload:
        return payload
    ref = payload["resource_ref"]
    if not isinstance(ref, dict) or ref.get("field") not in {"username", "email", "phone", "password", "otp"}:
        raise ValueError("secret_source_unavailable")
    from mino_nexus.services.project_store import find_test_account
    from mino_nexus.services.pool_account_store import try_claim_account_lease
    account_id = str(ref.get("account_id") or "")
    account = find_test_account(project_id=project_id, account_id=account_id)
    if not account or account.get("locked"):
        raise ValueError("secret_source_unavailable")
    from datetime import datetime, timedelta, timezone
    expiry = datetime.now(timezone.utc) + timedelta(seconds=max(1, deadline - time.monotonic()) + 30)
    if not try_claim_account_lease(project_id, account_id, {"run_id": attempt_id, "sn": sn, "expires_at": expiry.isoformat()}):
        raise ValueError("resource_busy")
    return {"text": str(account.get(ref["field"]) or "")}


def _attempt(task_id, case, doc, number, retry_of):
    frozen = copy.deepcopy(case.get("v5_publication") or {})
    source_case = {**case, **frozen.get("source", {})}
    budget = ExecutionControl(compute_case_wall_budget_sec(source_case))
    attempt_id = f"{case.get('report_run_id') or task_id + '::' + case['case_id']}::attempt-{number}-{uuid.uuid4().hex[:8]}"
    sn = case.get("sn") or doc.get("sn")
    pid = str(case.get("project_id") or "")
    router = RouterProxy(sn, run_id=attempt_id, task_id=task_id, target_package=doc.get("package", ""),
        playwright_headless=True, execution_mode="v5", execution_control=budget)
    evidence, action_refs = [], {}
    generation, verification, reason, stopped = "", "unverified", "", False
    opened_dispatched = False
    context = {"env_profile": str(doc.get("env_profile") or ""), "env_surface": str(doc.get("env_surface") or ""), "action_scheme": "dom_preferred",
        "provider_id": str(doc.get("provider_id") or ""), "execution_mode": "v5", "plan_source": "text", "frozen_revision": frozen.get("frozen_revision"),
        "publication": frozen, "controls": frozen.get("controls", {}), "predicate_contract": "web.atomic.v1",
        "verifier_build": "web-atomic-1", "project_id": pid, "sn": sn, "node_id": str((doc.get("device_targets", {}).get(sn) or {}).get("node_id") or ""),
        "attempt": number, "retry_of": retry_of, "timeout_profile": PROFILE,
        "wall_budget_sec": budget.deadline - budget.started, "runnable_now": {"ready": False, "reason_code": "preflight_pending"}}
    session_store.open_meta(session_id=attempt_id, run_id=task_id, case_id=case["case_id"], app_id=doc.get("app_id", ""))

    def commit(records, patch=None, *, final=False):
        try:
            deadline = time.monotonic() + 2
            if not final:
                deadline = min(budget.deadline, deadline)
            return append_batch(attempt_id, records, patch, deadline, eligible=True)
        except Exception as exc:
            raise ValueError("store_error") from exc

    def dispatch(cap, params=None, *, seconds=3, reserve=0, seq=0, operation_ref=''):
        nonlocal opened_dispatched
        if not operation_ref:
            operation_ref = {'probe': 'system:probe', 'open_web_v5': 'system:open',
                'screenshot': 'system:screenshot', 'close_web_v5': 'system:close'}.get(cap, '')
        operation = next((row for row in (router.execution_binding or {}).get('operations', [])
                          if row['operation_ref'] == operation_ref), {})
        event = PlanEvent(seq=seq, capability_id=cap, params=params or {}, expected_executor="playwright")
        result = router.dispatch(event, timeout_sec=seconds, reserve_sec=reserve,
                                 operation_ref=operation_ref, binding_phase=operation.get('phase', ''))
        raw = result.raw_response
        if cap == 'open_web_v5' and raw.get('action_ref'):
            opened_dispatched = True
        record = {"type": "device/action" if cap in {"open_web_v5", "tap_target", "input_target"} else "device/observation",
                  "payload": {"capability_id": cap, "status": result.status.value, "logical_seq": seq,
                              "request": event.params, "raw_response": raw}}
        refs = commit([record])
        raw["event_ref"] = refs[0]
        if raw.get("lane_id") is not None:
            context["lane_id"] = raw["lane_id"]
        return result

    try:
        commit([], {"runtime_context": context})
        if not frozen or case.get("v5_plan_state") == "needs_revalidate":
            raise ValueError("plan_invalid")
        if case.get("resource_key") != frozen.get("resource_key"):
            raise ValueError("resource_version_changed")
        from mino_nexus.services.v5_resources import validate as validate_resource
        if validate_resource(case.get("resource_key")) != frozen.get("baseline"):
            raise ValueError("baseline_missing")
        if frozen.get("check_program_plan", {}).get("schema") != "mino.check_program_plan.v2":
            raise ValueError("plan_invalid")
        from mino_nexus.services.v5_capabilities import snapshot as capability_snapshot
        if capability_snapshot() != frozen.get("capabilities"):
            raise ValueError("catalog_version_changed")
        router.v5_capabilities = copy.deepcopy(frozen["capabilities"])
        controls = frozen["controls"]
        # Pin current revisions before execution. Later changes cannot alter this copy.
        for cid, control in controls.items():
            current = get_control(pid, cid)
            if not current or current.get("revision") != control["revision"] or current.get("publish_state") != "published":
                raise ValueError("control_version_changed")
        from mino_nexus.services.execution_binding_runtime import strict_requirements
        requirements = strict_requirements(case, str(doc.get('app_id') or ''))
        router.bind_execution(requirements, session_id=attempt_id,
            env_profile=str(doc.get('env_profile') or ''), env_surface=str(doc.get('env_surface') or ''),
            deadline=min(budget.deadline, time.monotonic() + 2))
        probe = dispatch("probe", seconds=3)
        if probe.status != EventStatus.PASS or probe.raw_response.get("contracts") != CONTRACTS:
            raise ValueError("capability_unavailable")
        context["runnable_now"] = {"ready": False, "reason_code": "baseline_pending"}
        commit([], {"runtime_context": context})
        checks = frozen["check_program_plan"]["checkpoints"]
        from mino_nexus.services.check_observation_policy import validate_frozen, source_ready
        for check in checks:
            validate_frozen(check)
        remaining_checks = sum(c["wait_policy"]["timeout_ms"] / 1000 for c in checks if c["required"])
        url = str(doc.get("package") or "")
        opened = dispatch("open_web_v5", {"url": url}, seconds=11, reserve=remaining_checks + 9)
        if opened.status != EventStatus.PASS:
            raise ValueError(budget.reason_code or "effect_unknown")
        generation = opened.raw_response.get("observation", {}).get("session_generation", "")
        if not generation:
            raise ValueError("observation_missing")
        baseline = frozen["baseline"]
        baseline_receipt = dispatch("read_web_target_facts", {"control": controls[baseline["control_id"]],
            "requested_facts": ["visible"]}, seconds=3, operation_ref='system:baseline')
        baseline_origin = baseline_receipt.raw_response.get("observation") or {}
        baseline_facts = baseline_receipt.raw_response.get("target_facts") or {}
        baseline_fresh = (baseline_origin.get("run_id") == attempt_id and baseline_origin.get("session_generation") == generation
                          and baseline_origin.get("after_step_idx") == opened.raw_response["action_ref"]["step_idx"]
                          and baseline_facts.get("control_id") == baseline["control_id"]
                          and baseline_facts.get("control_revision") == controls[baseline["control_id"]]["revision"])
        if baseline_receipt.status != EventStatus.PASS or not baseline_fresh or evaluate(baseline["expected"], baseline_facts)["truth"] != "true":
            raise ValueError("baseline_unavailable")
        context["baseline"] = {"verified": True, "event_ref": baseline_receipt.raw_response["event_ref"], "session_generation": generation}
        context["runnable_now"] = {"ready": True, "reason_code": "", "checked_at": session_store._now(),
            "details": ["frozen_plan", "resource_version", "control_revisions", "web_v5_contract", "node_pin", "headless", "fresh_context", "logged_out_baseline"]}
        commit([], {"runtime_context": context})
        for index, action in enumerate(frozen["actions"], 1):
            control = controls[action["control_id"]]
            if action["capability_id"] == "input_target":
                classification = dispatch("read_web_target_facts", {"control": control, "requested_facts": []},
                    seconds=1, operation_ref='classify:' + action['operation_id'])
                if classification.raw_response.get("target_facts", {}).get("sensitive") and not control.get("sensitive"):
                    raise ValueError("sensitive_classification_missing")
            payload = _input_payload(action["payload"], pid, attempt_id, sn, deadline=budget.deadline)
            params = {"control": control, "payload": payload}
            register_inputs(params)
            receipt = dispatch(action["capability_id"], params, seconds=6, reserve=remaining_checks + 6,
                               seq=index, operation_ref='action:' + action['operation_id'])
            if receipt.status != EventStatus.PASS:
                raise ValueError(budget.reason_code or "effect_unknown")
            action_ref = receipt.raw_response.get("action_ref")
            if not action_ref:
                raise ValueError("execution_metadata_missing")
            action_refs[action["operation_id"]] = action_ref
            for check in [c for c in checks if c["after_operation_id"] == action["operation_id"]]:
                window = min(budget.deadline - 6, time.monotonic() + check["wait_policy"]["timeout_ms"] / 1000)
                point_evidence, evaluated = [], {"truth": "unavailable"}
                invalid_source = False
                for read in range(check["wait_policy"]["max_reads"]):
                    left = window - time.monotonic()
                    if left <= 0:
                        break
                    observation = dispatch("read_web_target_facts", {"control": controls[check["control_id"]],
                        "requested_facts": requested_facts(check["expected"]), "after_action": action_ref,
                        "observation_contract": check["wait_policy"].get("observation_contract", "")},
                        seconds=left, operation_ref='check:' + check['point_id'])
                    raw = observation.raw_response
                    facts, origin = raw.get("target_facts") or {}, raw.get("observation") or {}
                    fresh = (origin.get("run_id") == attempt_id and origin.get("session_generation") == generation
                        and type(origin.get("after_step_idx")) is int and origin["after_step_idx"] >= action_ref["step_idx"]
                        and facts.get("control_id") == check["control_id"]
                        and facts.get("control_revision") == controls[check["control_id"]]["revision"]
                        and source_ready(check, facts))
                    invalid_source = invalid_source or bool(origin and facts and not fresh)
                    evaluated = evaluate(check["expected"], facts if fresh and observation.status == EventStatus.PASS else {})
                    point_evidence.append({"read": read + 1, "event_ref": raw.get("event_ref"), "observation": origin,
                        "facts": facts, "fresh": fresh, "evaluation": evaluated})
                    if evaluated["truth"] == "true":
                        break
                    if read + 1 < check["wait_policy"]["max_reads"]:
                        threading.Event().wait(max(0, min(.2, window - time.monotonic())))
                item = {"point_id": check["point_id"], "required": check["required"], "action_ref": action_ref,
                        "control_id": check["control_id"], "truth": evaluated["truth"], "reads": point_evidence}
                evidence.append(item)
                if check["required"]:
                    remaining_checks -= check["wait_policy"]["timeout_ms"] / 1000
                    if evaluated["truth"] != "true":
                        verification = "verified_fail" if evaluated["truth"] == "false" else "unverified"
                        raise ValueError("assertion_mismatch" if verification == "verified_fail" else "evidence_invalid" if invalid_source else "observation_missing")
        required = {c["point_id"] for c in checks if c["required"]}
        consumed = {e["point_id"] for e in evidence if e["required"] and e["truth"] == "true"}
        if not required or required != consumed:
            raise ValueError("required_check_not_consumed")
        screenshot = dispatch("screenshot", {"sensitive_controls": [c for c in controls.values() if c.get("sensitive")]}, seconds=3)
        origin = screenshot.raw_response.get("observation") or {}
        last_step = max(ref["step_idx"] for ref in action_refs.values())
        if (screenshot.status != EventStatus.PASS or origin.get("run_id") != attempt_id or
            origin.get("session_generation") != generation or origin.get("after_step_idx", -1) < last_step or not screenshot.raw_response.get("image_base64")):
            raise ValueError("artifact_missing")
        context["screenshot_event_ref"] = screenshot.raw_response["event_ref"]
        verification = "verified_pass"
        from mino_nexus.services import run_store
        if run_store.task_cancelled(task_id):
            raise ValueError("task_cancelled")
    except Exception as exc:
        reason = budget.reason_code or (str(exc) if isinstance(exc, ValueError) else "store_error" if isinstance(exc, (TimeoutError,)) else "execution_error")
        budget.stop(reason)
        if reason != "assertion_mismatch":
            verification = "unverified"
    finally:
        # Stop evidence is required before releasing a leased account or retrying.
        try:
            if not opened_dispatched:
                stopped = True  # No lane or UI action was requested before this admission failure.
            else:
                close = router.dispatch(PlanEvent(seq=0, capability_id="close_web_v5"), timeout_sec=1,
                                        operation_ref='system:close', binding_phase='cleanup')
                stopped = bool(close.raw_response.get("executor_stopped"))
            if not stopped and verification == "verified_pass":
                verification, reason = "unverified", "lane_quarantined"
        except Exception:
            if verification == "verified_pass":
                verification, reason = "unverified", "lane_quarantined"
        if stopped:
            from mino_nexus.services.pool_account_store import clear_leases_for_run
            clear_leases_for_run(pid, attempt_id)
        budget.terminate()
    context.update(session_generation=generation, elapsed_ms=int((time.monotonic() - budget.started) * 1000), executor_stopped=stopped)
    verdict = {"verification_status": verification, "reason_code": reason, "legacy_error_kind": "",
               "consumed_required": [e["point_id"] for e in evidence if e["required"]], "flaky": number > 1 and verification == "verified_pass"}
    try:
        commit([{"type": "verification/evidence", "payload": {"checks": evidence, "action_refs": action_refs}},
                {"type": "verification/verdict", "payload": verdict}],
            {"runtime_context": context, "verdict": verdict, "finalization_id": attempt_id,
             "status": "pass" if verification == "verified_pass" else "fail", "summary": reason or verification,
             "finished_at": session_store._now()}, final=True)
    except Exception:
        # Never redo UI actions to compensate for a storage fault.
        verdict.update(verification_status="unverified", reason_code="store_error", flaky=False)
    return {**verdict, "session_id": attempt_id, "attempt_run_id": attempt_id, "executor_stopped": stopped,
            "status": "pass" if verdict["verification_status"] == "verified_pass" else "fail",
            "summary": verdict["reason_code"] or verdict["verification_status"], "elapsed_ms": context["elapsed_ms"]}


def run_case(task_id, case, doc):
    attempts = []
    frozen = case.get("v5_publication") or {}
    policy = frozen.get("retry_policy") or {"max": 0, "on": []}
    with registry_runtime("v5"):
        for number in range(1, min(2, int(policy.get("max", 0))) + 2):
            result = _attempt(task_id, case, doc, number, attempts[-1]["attempt_run_id"] if attempts else "")
            attempts.append(result)
            if result["reason_code"] != "effect_unknown" or "effect_unknown" not in policy.get("on", []) or not result["executor_stopped"]:
                break
    return {**result, "attempts": attempts, "flaky": result["status"] == "pass" and len(attempts) > 1}


def run_case_list(task_id, cases, doc):
    from mino_nexus.services import run_store
    from mino_nexus.loop.observe.agent_stream import emit_testing_task
    for case in cases:
        if run_store.task_cancelled(task_id):
            break
        cid = case["case_id"]
        run_store.patch_case(task_id, cid, status="running")
        result = run_case(task_id, case, doc)
        run_store.patch_case(task_id, cid, **result)
        emit_testing_task({"event": "case_finished", "run_id": task_id, "task_id": task_id, "case_id": cid, **result})
