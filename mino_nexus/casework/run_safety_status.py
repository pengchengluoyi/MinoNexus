"""Current resource safety is separate from the frozen historical verdict."""
import time
from mino_nexus.platform.identity import require_user_id


def status(task_id):
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.execution_resource import ExecutionResource
    from mino_nexus.models.run import AppRegressionRun
    from mino_nexus.casework.run_write_safety import unresolved
    with session_scope() as db:
        row = db.get(AppRegressionRun, task_id)
        if row is None or row.owner_user_id != require_user_id():
            raise KeyError("task_not_found")
        doc = row.payload
        actions = [{"action_ref": item.get("action_ref"), "sn": item.get("sn", ""),
            "state": item.get("state"), "execution_started": item.get("execution_started", "unknown"),
            "stop_confirmed": item.get("stop_confirmed") is True,
            "resource_reset_verified": (item.get("recovery") or {}).get("resource_reset_verified") is True,
            "reason_code": item.get("reason_code", "")}
            for item in unresolved(doc)]
        resources = [{"resource_key": r.resource_key, "execution_unit_id": r.execution_unit_id,
            "sn": r.sn, "state": r.state, "epoch": r.epoch, "expires_at": r.expires_at,
            "expired": r.expires_at <= time.time()} for r in db.query(ExecutionResource).filter_by(run_id=task_id).all()]
        isolated = bool(actions) or any(r["state"] == "isolated" or r["state"] == "held" and r["expired"] for r in resources)
        missing = []
        if (isolated and not actions) or any(not a["stop_confirmed"] for a in actions):
            missing.append("stop_evidence_required")
        if isolated:
            missing.append("resource_reset_verification_required")
        return {"task_id": task_id, "resource_isolated": isolated, "actions": actions, "resources": resources,
            "missing_evidence": missing, "business_result_unchanged": True,
            "reconciliation_count": len(doc.get("write_reconciliation") or {})}
