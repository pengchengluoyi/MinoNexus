"""V5 binding persistence rules over the existing atomic session transaction."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json

WARNING_BYTES = 256 * 1024


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _merge(old, patch):
    merged = deepcopy(old)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def merge_context(current, patch):
    """Deep merge shared context, preserving the immutable attempt binding."""
    from mino_nexus.services.execution_binding import validate_binding
    if current is None:
        current = {}
    if not isinstance(current, dict) or not isinstance(patch, dict):
        raise ValueError("runtime_context_invalid")
    if 'resolved_case' in patch and current.get('resolved_case') is not None:
        if _json(current['resolved_case']) != _json(patch['resolved_case']):
            raise ValueError('resolved_case_immutable')
    existing = current.get("execution_binding")
    incoming = patch.get("execution_binding")
    if "execution_binding" in patch:
        validate_binding(incoming)
        if existing is not None and _json(existing) != _json(incoming):
            raise ValueError("execution_binding_immutable")
    merged = _merge(current, patch)
    if existing is not None:
        merged["execution_binding"] = deepcopy(existing)
    return merged


def binding_batch(current, patch, records):
    """Called under BEGIN IMMEDIATE; event + binding commit or roll back together."""
    context_patch = patch.get("runtime_context")
    if context_patch is None and "runtime_context" not in patch:
        return patch, records, None
    if isinstance(context_patch, dict) and isinstance(context_patch.get("runtime_state"), dict):
        old_state = (current or {}).get("runtime_state") or {}
        for reserved in ("execution_bound_ref", "execution_binding_commit_refs", "finalization_digest"):
            if reserved in context_patch["runtime_state"] and context_patch["runtime_state"][reserved] != old_state.get(reserved):
                raise ValueError("binding_runtime_receipt_immutable")
    context = merge_context(current, context_patch)
    incoming = context_patch.get("execution_binding")
    bound_records = [row for row in records if row.get("type") == "execution/bound"]
    if bound_records and incoming is None:
        raise ValueError("binding_event_without_binding")
    if incoming is not None:
        if len(bound_records) != 1:
            raise ValueError("binding_event_required")
        payload = bound_records[0].get("payload") or {}
        if payload.get("binding_id") != incoming["binding_id"] or payload.get("binding_revision") != incoming["binding_revision"]:
            raise ValueError("binding_event_mismatch")
        if (current or {}).get("execution_binding") is not None:
            if len(records) != 1 or set(patch) != {"runtime_context"} or set(context_patch) != {"execution_binding"}:
                raise ValueError("binding_idempotence_mixed_update")
            ref = ((current or {}).get("runtime_state") or {}).get("execution_bound_ref")
            if not isinstance(ref, dict) or not isinstance(ref.get("seq"), int):
                raise ValueError("binding_receipt_missing")
            refs = ((current or {}).get("runtime_state") or {}).get("execution_binding_commit_refs")
            return patch, [], deepcopy(refs) if isinstance(refs, list) else [deepcopy(ref)]
        byte_count = len(_json(incoming).encode("utf-8"))
        if byte_count > WARNING_BYTES:
            records = [*records, {"type": "binding_size_warning", "payload": {
                "binding_id": incoming["binding_id"], "binding_bytes": byte_count,
                "warning_bytes": WARNING_BYTES, "schema_version": incoming["schema_version"],
                "operation_count": len(incoming["operations"]), "unique_implementation_count": len(incoming["implementations"]),
                "plan_ref": incoming["plan_ref"], "source_revision": incoming["source_revision"]}}]
    return {**patch, "runtime_context": context}, records, None


def finalization_digest(records, patch):
    content = deepcopy(patch)
    if isinstance(content.get("verdict"), dict):
        content["verdict"] = {key: value for key, value in content["verdict"].items() if key != "event_refs"}
    return hashlib.sha256(_json({"records": records, "meta": content}).encode("utf-8")).hexdigest()


def persist_binding(session_id, binding, *, deadline=None):
    from mino_nexus.services.execution_binding import validate_binding
    from mino_nexus.services.session_atomic import append_batch
    value = validate_binding(binding)
    return append_batch(session_id, [{"type": "execution/bound", "payload": {
        "binding_id": value["binding_id"], "binding_revision": value["binding_revision"],
        "schema_version": value["schema_version"], "plan_ref": value["plan_ref"]}}],
        {"runtime_context": {"execution_binding": value}}, deadline, eligible=True)
