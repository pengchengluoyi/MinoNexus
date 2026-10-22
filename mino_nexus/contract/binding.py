"""Pure V5 execution binding resolution. No catalog, storage or device access.

Snapshots are supplied by authenticated callers. Compilation produces requirements
only; execution must resolve again against the current target and registration.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field
import hashlib
import json
from typing import Any

SCHEMA_VERSION = 1
CONTRACT = "execution_binding.v1"
MAX_DEPENDENCIES = 32
READ_EVENTS = frozenset({"plugin.gmail.fetch_otp", "probe", "probe_device_state", "screenshot", "get_foreground_app", "get_app_version",
                         "read_web_auth", "read_web_target_facts", "read_device_data", "hierarchy", "wait_ms", "assert_visual", "check_run_env"})
_IDENTITY = ("plan_ref", "project_id", "app_id", "owner_scope_ref", "requirements_ref",
             "source_revision", "timeout_profile_ref", "evidence_policy_ref")
_TARGET = ("node_id", "sn", "platform", "env_profile", "env_surface", "target_ref")
_GENERATIONS = ("node_connection_generation", "scout_process_generation", "capability_revision")


@dataclass
class BindingResolution:
    status: str
    binding: dict[str, Any] | None = None
    diagnostics: list[dict[str, Any]] = field(default_factory=list)

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


def effect_class(event_id: str) -> str:
    """Unknown UI events remain writes; registration cannot weaken this default."""
    return "read" if event_id in READ_EVENTS else "write"


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _diagnostic(code: str, *, category="configuration", operation=None, missing=(), recheck=True):
    op = operation or {}
    return {"reason_code": code, "responsibility": category,
            "case_ref": op.get("case_ref", ""), "step_ref": op.get("step_ref", ""),
            "clause_ref": op.get("clause_ref", ""), "operation_ref": op.get("operation_ref", ""),
            "missing_fields": list(missing), "recovery_entry": "workbench.execution_review",
            "recheckable": bool(recheck)}


def _dependency_checks(dependencies, facts, operation):
    if not isinstance(dependencies, list) or len(dependencies) > MAX_DEPENDENCIES:
        return [], [_diagnostic("dependency_contract_unsupported", operation=operation, recheck=False)]
    checks, diagnostics = [], []
    for dependency in dependencies:
        if not isinstance(dependency, dict) or set(dependency) - {"id", "mode", "refs"}:
            diagnostics.append(_diagnostic("dependency_contract_unsupported", operation=operation, recheck=False))
            continue
        mode, refs = dependency.get("mode", "all"), dependency.get("refs")
        if (mode not in {"all", "any"} or not dependency.get("id") or not isinstance(refs, list)
                or not refs or len(refs) > MAX_DEPENDENCIES or any(not isinstance(ref, str) or not ref for ref in refs)):
            diagnostics.append(_diagnostic("dependency_contract_unsupported", operation=operation, recheck=False))
            continue
        if any(ref in {operation.get("operation_ref"), operation.get("event_id")} for ref in refs):
            diagnostics.append(_diagnostic("dependency_contract_unsupported", operation=operation, recheck=False))
            continue
        rows = [facts.get(ref) for ref in refs]
        if any(isinstance(row, dict) and any(key in row for key in ("dependencies", "children", "refs")) for row in rows):
            diagnostics.append(_diagnostic("dependency_contract_unsupported", operation=operation, recheck=False))
            continue
        statuses = [row.get("status", "unknown") if isinstance(row, dict) else "unknown" for row in rows]
        ready = all(status == "ready" for status in statuses) if mode == "all" else "ready" in statuses
        status = "ready" if ready else "blocked" if (
            "blocked" in statuses if mode == "all" else all(s == "blocked" for s in statuses)
        ) else "needs_input"
        checks.append({"id": dependency["id"], "mode": mode, "refs": list(refs), "status": status,
                       "facts": [{"ref": ref, **{k: row[k] for k in ("status", "reason_code", "node_id",
                           "node_connection_generation", "scout_process_generation", "target_ref", "provider_id", "model")
                           if k in row and isinstance(row[k], str)}}
                           for ref, row in zip(refs, rows) if isinstance(row, dict)]})
        if not ready:
            diagnostics.append({**_diagnostic("dependency_blocked" if status == "blocked" else "dependency_unknown",
                category="resource" if status == "blocked" else "input", operation=operation,
                missing=[ref for ref, state in zip(refs, statuses) if state != "ready"]),
                "dependency_facts": deepcopy(checks[-1]["facts"])})
    return checks, diagnostics


def _supports_combination(rows, executor, platform, combination):
    # Exact supported combinations, never a product of independent advertised sets.
    if type(combination.get("contract_version")) is not int or combination["contract_version"] != 2:
        return False
    for row in rows:
        if not isinstance(row, dict) or row.get("id") != "input_text" or row.get("executor") != executor:
            continue
        if row.get("platform") != platform or type(row.get("contract_version")) is not int or row["contract_version"] != 2:
            continue
        for supported in row.get("supported_combinations") or []:
            if not isinstance(supported, dict) or supported.get("target_mode") != combination.get("target_mode"):
                continue
            if combination.get("write_mode") in (supported.get("write_modes") or []):
                return True
    return False


def _resolve_execution_binding(requirements, target, capability_snapshot, policy_snapshot, execution_contract):
    """Resolve a complete v1 binding or diagnostics, without side effects.

    ``events`` maps event IDs to catalog snapshots; ``executors`` is the current
    full manifest. Each operation references its compiled event and exact static
    input combination. No dynamic action parameters belong in this input.
    """
    if not all(isinstance(value, dict) for value in (requirements, target, capability_snapshot, policy_snapshot)):
        return BindingResolution("blocked", diagnostics=[_diagnostic("binding_input_invalid", recheck=False)])
    if execution_contract != CONTRACT:
        return BindingResolution("blocked", diagnostics=[_diagnostic("execution_contract_unsupported", recheck=False)])
    from mino_nexus.core.protocol import execution_mode as require_execution_mode
    try:
        mode = require_execution_mode(requirements.get("execution_mode"))
    except ValueError:
        return BindingResolution("blocked", diagnostics=[_diagnostic("execution_mode_unsupported", recheck=False)])
    if requirements.get("applicable") is False:
        return BindingResolution("not_applicable")
    if requirements.get("applicable") is not True:
        return BindingResolution("needs_input", diagnostics=[_diagnostic("applicability_unknown", category="input")])
    if type(capability_snapshot.get("execution_binding_version")) is not int or capability_snapshot["execution_binding_version"] != 1:
        return BindingResolution("blocked", diagnostics=[_diagnostic("execution_binding_unsupported", category="runtime")])
    missing = [name for name in _IDENTITY if not requirements.get(name)]
    missing += [name for name in _TARGET if not target.get(name)]
    missing += [name for name in _GENERATIONS if not capability_snapshot.get(name)]
    missing += [name for name in ("policy_ref", "caller", "allowed_phases") if not policy_snapshot.get(name)]
    if missing:
        return BindingResolution("needs_input", diagnostics=[_diagnostic("binding_fields_missing", category="input", missing=missing)])
    if capability_snapshot.get("node_id") != target["node_id"]:
        return BindingResolution("blocked", diagnostics=[_diagnostic("capability_target_mismatch")])
    for name in _GENERATIONS:
        if name in target and target[name] != capability_snapshot[name]:
            return BindingResolution("blocked", diagnostics=[_diagnostic("capability_snapshot_stale", category="runtime")])
    if target.get("online") is not True:
        return BindingResolution("blocked", diagnostics=[_diagnostic("target_offline", category="runtime")])
    if target.get("resource_state") in {"contended", "isolated"}:
        return BindingResolution("blocked", diagnostics=[_diagnostic(
            "resource_isolated" if target["resource_state"] == "isolated" else "resource_contended", category="resource")])
    scope = policy_snapshot.get("target_scope")
    if not isinstance(scope, dict) or not scope:
        return BindingResolution("needs_input", diagnostics=[_diagnostic("target_scope_missing", category="input")])
    actual_scope = {**requirements, **target}
    if any(actual_scope.get(key) != value for key, value in scope.items()):
        return BindingResolution("blocked", diagnostics=[_diagnostic("target_scope_mismatch", category="authorization")])
    operations = requirements.get("operations")
    if not isinstance(operations, list) or not operations:
        return BindingResolution("blocked", diagnostics=[_diagnostic("requirements_empty", recheck=False)])
    events = capability_snapshot.get("events")
    executors = capability_snapshot.get("executors")
    if not isinstance(events, dict) or not isinstance(executors, list):
        return BindingResolution("blocked", diagnostics=[_diagnostic("capability_snapshot_invalid")])
    available = {row["id"]: row for row in executors if isinstance(row, dict) and row.get("available") is True and row.get("id")}
    connected = target.get("connected_executors")
    if not isinstance(connected, list):
        return BindingResolution("needs_input", diagnostics=[_diagnostic("target_channels_unknown", category="input")])
    facts = capability_snapshot.get("dependency_facts", {})
    if not isinstance(facts, dict):
        return BindingResolution("blocked", diagnostics=[_diagnostic("dependency_snapshot_invalid")])
    diagnostics, bound, dependency_checks, seen = [], [], [], set()
    implementations = {}
    for operation in operations:
        if not isinstance(operation, dict) or not operation.get("operation_ref") or not operation.get("event_id"):
            diagnostics.append(_diagnostic("operation_contract_invalid", recheck=False)); continue
        ref = operation["operation_ref"]
        if ref in seen:
            diagnostics.append(_diagnostic("operation_ref_duplicate", operation=operation, recheck=False)); continue
        seen.add(ref)
        if operation.get("applicable") is False:
            dependency_checks.append({"operation_ref": ref, "status": "not_applicable"}); continue
        if operation.get("applicable") is not True:
            diagnostics.append(_diagnostic("applicability_unknown", category="input", operation=operation)); continue
        cid = operation["event_id"]
        cap = events.get(cid)
        if not isinstance(cap, dict):
            diagnostics.append(_diagnostic("event_not_registered", operation=operation)); continue
        if cap.get("enabled") is not True or cap.get("lifecycle") != "active" or cap.get("ui_coverable") is False:
            diagnostics.append(_diagnostic("event_disabled", operation=operation)); continue
        caller = (policy_snapshot.get("operation_callers") or {}).get(ref, policy_snapshot["caller"])
        if (cap.get("caller") != caller or operation.get("phase") not in policy_snapshot["allowed_phases"]
                or operation.get("phase") not in (cap.get("phases") or [])):
            diagnostics.append(_diagnostic("event_call_not_allowed", category="authorization", operation=operation)); continue
        if target["platform"] not in (cap.get("platforms") or []):
            diagnostics.append(_diagnostic("event_platform_unsupported", operation=operation)); continue
        if cid in policy_snapshot.get("denied_events", []):
            diagnostics.append(_diagnostic("event_policy_denied", category="authorization", operation=operation)); continue
        deps, errors = _dependency_checks(operation.get("dependencies", []), facts, operation)
        dependency_checks.extend({"operation_ref": ref, **item} for item in deps)
        diagnostics.extend(errors)
        candidates = []
        for implementation in cap.get("implementations") or []:
            if not isinstance(implementation, dict):
                continue
            executor = implementation.get("executor")
            runs_on = implementation.get("runs_on")
            local = runs_on == "nexus"
            manifests = policy_snapshot.get("local_executors", {}) if local else available
            manifest = manifests.get(executor)
            if (not implementation.get("id") or not isinstance(manifest, dict)
                    or manifest.get("available") is not True or (not local and executor not in connected)):
                continue
            provided = set(manifest.get("provides") or [])
            requires = implementation.get("requires_caps")
            if cid not in provided and not (isinstance(requires, list) and requires
                    and all(isinstance(item, str) and item in provided for item in requires)):
                continue
            permitted = policy_snapshot.get("allowed_executors")
            if permitted is not None and executor not in permitted:
                continue
            if target["platform"] not in implementation.get("platforms", cap["platforms"]):
                continue
            if runs_on not in {"scout", "nexus"} or not isinstance(implementation.get("low_level"), dict):
                continue
            if any(name not in implementation for name in ("params_contract", "result_contract", "device_hint_template")):
                continue
            if any(not isinstance(value, dict) or set(value) != {"version"} or type(value["version"]) is not int or value["version"] != 1
                   for value in (implementation["params_contract"], implementation["result_contract"])):
                continue
            combination = operation.get("input_combination")
            if cid == "input_text" and (not isinstance(combination, dict) or not _supports_combination(
                    capability_snapshot.get("runtime_capabilities", []), executor, target["platform"], combination)):
                continue
            candidates.append({"implementation_id": implementation["id"], "executor": executor,
                "runs_on": runs_on, "executor_order": [executor], "selected_impl": deepcopy(implementation),
                "low_level": deepcopy(implementation["low_level"]),
                "params_contract": deepcopy(implementation["params_contract"]),
                "result_contract": deepcopy(implementation["result_contract"]),
                "device_hint_template": deepcopy(implementation["device_hint_template"])})
        preferred = operation.get("implementation_id")
        if preferred:
            candidates = [candidate for candidate in candidates if candidate["implementation_id"] == preferred]
        if not candidates:
            diagnostics.append(_diagnostic("input_combination_unsupported" if cid == "input_text" else "implementation_unavailable", operation=operation)); continue
        candidate_refs = []
        for candidate in candidates:
            implementation_ref = "impl-" + hashlib.sha256(_canonical(candidate).encode("utf-8")).hexdigest()[:24]
            implementations.setdefault(implementation_ref, candidate)
            candidate_refs.append(implementation_ref)
        bound.append({"operation_ref": ref, "event_id": cid, "phase": operation["phase"],
            "caller": caller, "allowed_phases": list(cap["phases"]),
            "enabled": True, "lifecycle": "active", "effect_class": effect_class(cid),
            "input_combination": deepcopy(operation.get("input_combination", {})),
            "source_refs": deepcopy(operation.get("source_refs") or []),
            "requirement": operation.get("requirement", "required"),
            "requirement_clauses": deepcopy(operation.get("requirement_clauses") or []),
            "selected_ref": candidate_refs[0], "approved_candidate_refs": candidate_refs})
    # Optional observation/recovery alternatives never widen the binding. An
    # unavailable alternative is omitted, and therefore cannot be dispatched.
    conditional = {op["operation_ref"] for op in operations if isinstance(op, dict) and op.get("requirement") == "conditional"}
    optional = {op["operation_ref"] for op in operations if isinstance(op, dict) and op.get("optional") is True and op.get("requirement") != "required"} | conditional
    omitted = {row["operation_ref"] for row in diagnostics if row["operation_ref"] in optional}
    optional_diagnostics = [{**row, "blocking": False, "requirement_status": "conditional_pending_observation" if row["operation_ref"] in conditional else "unavailable_optional"} for row in diagnostics if row["operation_ref"] in omitted]
    diagnostics = [row for row in diagnostics if row["operation_ref"] not in omitted]
    bound = [row for row in bound if row["operation_ref"] not in omitted]
    used = {ref for row in bound for ref in row["approved_candidate_refs"]}
    implementations = {ref: value for ref, value in implementations.items() if ref in used}
    for group in requirements.get("required_operation_groups", []):
        if not isinstance(group, dict) or not isinstance(group.get("refs"), list) or not group["refs"]:
            diagnostics.append(_diagnostic("dependency_contract_unsupported", recheck=False))
        elif not any(row["operation_ref"] in group["refs"] for row in bound):
            diagnostics.append(_diagnostic("input_combination_unsupported", missing=group["refs"]))
    if diagnostics:
        status = "blocked" if any(row["responsibility"] != "input" for row in diagnostics) else "needs_input"
        return BindingResolution(status, diagnostics=diagnostics)
    if not bound:
        return BindingResolution("not_applicable")
    scheme = str(requirements.get("action_scheme") or "dom_preferred")
    if scheme == "dom":
        scheme = "dom_preferred"
    if scheme not in {"dom_preferred", "visual"}:
        return BindingResolution("blocked", diagnostics=[_diagnostic("action_scheme_invalid", recheck=False)])
    binding = {"schema_version": SCHEMA_VERSION, "binding_revision": 1,
        **{key: deepcopy(requirements[key]) for key in _IDENTITY},
        "target": {key: deepcopy(target[key]) for key in _TARGET},
        **{key: capability_snapshot[key] for key in _GENERATIONS},
        "execution_mode": mode,
        "execution_contract_version": CONTRACT, "action_scheme": scheme,
        "policy_ref": policy_snapshot["policy_ref"], "caller": policy_snapshot["caller"],
        "target_scope": deepcopy(scope), "operations": bound, "implementations": implementations,
        "dependency_checks": dependency_checks,
        "requirement_checks": [{"operation_ref": op["operation_ref"], "requirement": op.get("requirement", "required"),
            "clauses": deepcopy(op.get("requirement_clauses") or []),
            "status": "conditional_pending_observation" if op["operation_ref"] in conditional else "unavailable_optional" if op["operation_ref"] in omitted else "bound"} for op in operations],
        "lease_requirements": deepcopy(requirements.get("lease_requirements", [])),
        "created_at": requirements.get("created_at", "")}
    if not binding["created_at"]:
        return BindingResolution("needs_input", diagnostics=[_diagnostic("binding_fields_missing", category="input", missing=["created_at"])])
    try:
        encoded = _canonical(binding)
    except (TypeError, ValueError):
        return BindingResolution("blocked", diagnostics=[_diagnostic("binding_not_json", recheck=False)])
    binding["binding_id"] = "binding-" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:32]
    return BindingResolution("ready", binding=binding, diagnostics=optional_diagnostics)


def validate_binding(binding):
    """Validate persisted v1 identity and prohibit live observation/input data."""
    if not isinstance(binding, dict) or type(binding.get("schema_version")) is not int or binding["schema_version"] != SCHEMA_VERSION:
        raise ValueError("binding_schema_unsupported")
    required = {"binding_id", "binding_revision", "target", "operations", "implementations", "created_at", "action_scheme",
                "execution_mode", "execution_contract_version", "policy_ref", "caller", "target_scope",
                "dependency_checks", "lease_requirements", *_IDENTITY, *_GENERATIONS}
    if required - binding.keys() or binding.get("execution_contract_version") != CONTRACT:
        raise ValueError("binding_schema_invalid")
    if any(not isinstance(binding[key], str) or not binding[key] for key in (*_IDENTITY, *_GENERATIONS, "created_at", "policy_ref", "caller")):
        raise ValueError("binding_fields_invalid")
    if not isinstance(binding["target"], dict) or any(not isinstance(binding["target"].get(key), str) or not binding["target"][key] for key in _TARGET):
        raise ValueError("binding_target_invalid")
    from mino_nexus.core.protocol import execution_mode as require_execution_mode
    require_execution_mode(binding["execution_mode"])
    if binding["action_scheme"] not in {"dom_preferred", "visual"}:
        raise ValueError("binding_action_scheme_invalid")
    if type(binding["binding_revision"]) is not int or binding["binding_revision"] < 1:
        raise ValueError("binding_revision_invalid")
    if not isinstance(binding.get("operations"), list) or not binding["operations"]:
        raise ValueError("binding_operations_invalid")
    implementations = binding["implementations"]
    if not isinstance(implementations, dict) or not implementations:
        raise ValueError("binding_implementations_invalid")
    for ref, candidate in implementations.items():
        if not isinstance(candidate, dict):
            raise ValueError("binding_assembly_invalid")
        selected = candidate.get("selected_impl")
        if (not isinstance(selected, dict) or candidate.get("runs_on") not in {"scout", "nexus"}
                or candidate.get("executor_order") != [candidate.get("executor")]
                or selected.get("executor") != candidate.get("executor")
                or selected.get("id") != candidate.get("implementation_id")
                or any(selected.get(key) != candidate.get(key) for key in
                       ("runs_on", "low_level", "params_contract", "result_contract", "device_hint_template"))):
            raise ValueError("binding_assembly_invalid")
        expected_ref = "impl-" + hashlib.sha256(_canonical(candidate).encode("utf-8")).hexdigest()[:24]
        if ref != expected_ref:
            raise ValueError("binding_implementation_identity_mismatch")
    seen = set()
    for operation in binding["operations"]:
        if not isinstance(operation, dict) or not operation.get("operation_ref") or operation["operation_ref"] in seen:
            raise ValueError("binding_operation_invalid")
        seen.add(operation["operation_ref"])
        selected_ref = operation.get("selected_ref")
        selected = implementations.get(selected_ref)
        if (not isinstance(selected, dict) or selected.get("runs_on") not in {"scout", "nexus"}
                or selected.get("executor_order") != [selected.get("executor")]
                or not selected.get("implementation_id") or not operation.get("event_id")
                or operation.get("effect_class") != effect_class(operation["event_id"])):
            raise ValueError("binding_assembly_invalid")
        candidates = operation.get("approved_candidate_refs") or []
        if selected_ref not in candidates or any(ref not in implementations for ref in candidates):
            raise ValueError("binding_candidate_invalid")
    forbidden = {"focus_ref", "node_ref", "wire_step_idx", "step_idx", "image_base64", "screenshot",
                 "password", "otp_value", "sms_code", "access_token", "authorization", "token",
                 "api_key", "secret", "cookie", "credentials"}
    def check(value):
        if isinstance(value, dict):
            if forbidden & value.keys():
                raise ValueError("binding_contains_runtime_or_secret")
            for key, child in value.items():
                # Descriptive schemas contain field names, never action data or defaults.
                if key not in {"params_contract", "result_contract"}:
                    check(child)
        elif isinstance(value, list):
            for child in value:
                check(child)
    check(binding)
    body = {key: value for key, value in binding.items() if key != "binding_id"}
    expected = "binding-" + hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()[:32]
    if binding["binding_id"] != expected:
        raise ValueError("binding_identity_mismatch")
    return deepcopy(binding)


def resolve_execution_binding(requirements, target, capability_snapshot, policy_snapshot, execution_contract):
    """Malformed snapshots are diagnostics, not exceptions or partial bindings."""
    try:
        result = _resolve_execution_binding(requirements, target, capability_snapshot, policy_snapshot, execution_contract)
        if result.binding is not None:
            validate_binding(result.binding)
        return result
    except (ValueError, TypeError, KeyError, AttributeError):
        return BindingResolution("blocked", diagnostics=[_diagnostic("binding_input_invalid", recheck=False)])


def dispatch_projection(binding, operation_ref):
    """Static dispatch projection; dynamic params and wire IDs stay with RouterProxy."""
    value = validate_binding(binding)
    operation = next((row for row in value["operations"] if row["operation_ref"] == operation_ref), None)
    if operation is None:
        raise ValueError("operation_not_bound")
    selected = value["implementations"][operation["selected_ref"]]
    epochs = {key: value[key] for key in _GENERATIONS}
    return {"binding_id": value["binding_id"], "binding_revision": value["binding_revision"],
            "operation_ref": operation_ref, "event_id": operation["event_id"],
            "input_contract_version": operation.get("input_combination", {}).get("contract_version", 0),
            "effect_class": operation["effect_class"],
            "execution_contract_version": value["execution_contract_version"],
            **epochs, **deepcopy(selected)}
