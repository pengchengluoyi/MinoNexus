"""Late transport receipts supplement the action ledger without rewriting a verdict."""
from __future__ import annotations

from copy import deepcopy


def merge_receipts(old, new):
    # Receipt identity is immutable; stale task projections cannot remove or replace it.
    return {**deepcopy((new or {}).get("write_reconciliation") or {}),
            **deepcopy((old or {}).get("write_reconciliation") or {})}


def record_late_result(identity: dict, result, *, receipt_id: str) -> bool:
    from mino_nexus.core.database import session_scope
    from mino_nexus.core.execution_user import current_user_id, execution_user
    from mino_nexus.models.session_event import SessionMeta
    from mino_nexus.services import run_store
    from mino_nexus.services.run_write_safety import _proof_mode_valid

    owner = identity.get("owner_user_id")
    if (not owner or current_user_id() not in {"", owner} or not receipt_id
            or (result.run_id, result.step_idx) != (identity["run_id"], identity["step_idx"])):
        return False
    ref = identity.get("action_ref") or {"run_id": result.run_id, "step_idx": result.step_idx}
    if (not isinstance(ref, dict) or not isinstance(ref.get("run_id"), str)
            or not ref["run_id"] or type(ref.get("step_idx")) is not int):
        return False
    with execution_user(owner):
        with session_scope() as db:
            session = db.get(SessionMeta, ref["run_id"])
            if session is None or session.owner_user_id != owner:
                return False
            task_id = session.run_id
        with run_store._LOCK:
            doc = run_store.get(task_id)
            key = f"{ref['run_id']}:{ref['step_idx']}"
            item = ((doc or {}).get("write_safety") or {}).get(key)
            if (not doc or doc.get("owner_user_id") != owner or not isinstance(item, dict)
                    or item.get("action_ref") != ref or item.get("sn") != identity.get("sn")
                    or item.get("node_id") != identity.get("node_id")
                    or not identity.get("scout_process_generation")):
                return False
            generation = identity["scout_process_generation"]
            stop_query = identity.get("capability_id") == "write_stop_status"
            if not stop_query and item.get("scout_process_generation") != generation:
                return False
            receipts = dict(doc.get("write_reconciliation") or {})
            if receipt_id in receipts:
                return True
            data = {**(result.extra or {}), **(result.data or {})}
            execution = data.get("execution")
            execution = execution if isinstance(execution, dict) else {}
            boundary = execution.get("execution_started", data.get("execution_started"))
            boundary = "yes" if boundary is True or type(boundary) is str and boundary == "yes" else "no" if boundary is False or type(boundary) is str and boundary == "no" else "unknown"
            proof = data.get("stop_evidence")
            proof = proof if isinstance(proof, dict) else {}
            stopped = bool(stop_query and proof.get("contract") == "write_stop.v1"
                and proof.get("stop_confirmed") is True and proof.get("action_ref") == ref
                and proof.get("sn") == item["sn"] and proof.get("scout_process_generation") == generation
                and _proof_mode_valid(proof, str(item.get("scout_process_generation") or ""), generation, doc))
            # Keep only protocol evidence, never response text, screenshots, input or secrets.
            receipts[receipt_id] = {"action_ref": deepcopy(ref), "request_ref": {
                    "run_id": result.run_id, "step_idx": result.step_idx},
                "node_id": identity["node_id"], "sn": identity["sn"],
                "scout_process_generation": generation,
                "node_connection_generation": identity.get("node_connection_generation", ""),
                "kind": "late_stop_receipt" if stop_query else "late_write_receipt",
                "status": str(getattr(result.status, "value", result.status)),
                "execution_started": boundary, "stop_confirmed": stopped,
                "resource_reset_verified": False, "at": run_store._now(),
                "business_result_unchanged": True}
            doc["write_reconciliation"] = receipts
            run_store.put(doc)
            return True
