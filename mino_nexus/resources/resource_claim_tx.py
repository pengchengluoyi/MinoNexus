"""资源认领事务上下文：由用例侧 `admission()` 设置，资源侧读取。"""
from __future__ import annotations

from contextvars import ContextVar

_claim = ContextVar("execution_resource_claim", default=None)


def _same_attempt(row, request) -> bool:
    return all(getattr(row, field) == request.get(key) for field, key in (
        ("run_id", "run_id"), ("owner_user_id", "owner"), ("sn", "sn"),
        ("project_id", "project_id"), ("execution_unit_id", "execution_unit_id"),
        ("attempt_id", "attempt_id")))


def claim_in_transaction(db, account_id=""):
    request = _claim.get()
    if request is None:
        return True
    import time
    from datetime import datetime, timezone

    from mino_nexus.models.execution_resource import ExecutionResource, ResourceDomain
    from mino_nexus.models.resource_ops import DeviceResourceLease
    from mino_nexus.resources.resource_pool import DEFAULT_LEASE_TTL_SEC as TTL
    from mino_nexus.records.write_state import protected
    from mino_nexus.contract.execution.device import is_web_slot
    from mino_nexus.models.run import AppRegressionRun
    run = db.get(AppRegressionRun, request["run_id"])
    from mino_nexus.resources.run_budget import remaining
    if (run is None or run.owner_user_id != request["owner"]
            or run.status not in {"running", "queued"} or remaining(run.payload) == 0):
        request["reason"] = "task_not_running"
        return False
    # Recheck persisted limits inside the account/resource allocation transaction.
    # A callback may have waited for a lock past its caller's remaining budget.
    cases = [case for case in run.payload.get("cases") or []
             if case.get("execution_unit_id") == request["execution_unit_id"]]
    if len(cases) != 1 or cases[0].get("status") not in {"pending", "running"}:
        request["reason"] = "execution_unit_not_pending"
        return False
    case = cases[0]
    if remaining(run.payload, case) == 0:
        request["reason"] = "attempt_deadline_exceeded"
        return False
    wait_end = case.get("resource_wait_deadline_at")
    if wait_end is not None and time.time() >= wait_end:
        request["reason"] = "resource_wait_deadline_exceeded"
        return False
    is_web = is_web_slot(request["sn"], request["platform"])
    keys = ["device:" + request["sn"] + (":" + request["run_id"] if is_web else "")]
    if account_id:
        keys.append("account:" + request["project_id"] + ":" + account_id)
    domains = db.query(ResourceDomain).filter_by(project_id=request["project_id"], env=request["env"], enabled=1).all()
    keys += ["domain:" + row.domain_id for row in domains if (not row.app_id or row.app_id == request["app_id"])
        and (not row.account_ids or account_id in row.account_ids)]
    if protected(request["run_id"], db=db, sn=request["sn"]):
        request["reason"] = "resource_isolated"
        return False
    for old in db.query(DeviceResourceLease).filter_by(sn=request["sn"]).all():
        if old.run_id != request["run_id"] and (not is_web or protected(old.run_id, db=db, sn=str(old.sn or ''))):
            request["reason"] = "resource_busy"
            return False
    rows = {key: db.get(ExecutionResource, key) for key in keys}
    for row in rows.values():
        if row and row.state != "released" and (not _same_attempt(row, request)
                or row.state != "held" or row.expires_at <= time.time()):
            request["reason"] = "resource_isolated" if row.state != "held" or row.expires_at <= time.time() else "resource_busy"
            return False
    epochs = {}
    for key in keys:
        old = rows[key]
        epoch = old.epoch if old and old.state == "held" else (old.epoch + 1 if old else 1)
        epochs[key] = epoch
        db.merge(ExecutionResource(resource_key=key, project_id=request["project_id"], owner_user_id=request["owner"],
            run_id=request["run_id"], execution_unit_id=request["execution_unit_id"], attempt_id=request["attempt_id"],
            sn=request["sn"], epoch=epoch, expires_at=time.time() + TTL, state="held",
            detail={"account_id": account_id, "package": request["package"]}))
    existing = db.query(DeviceResourceLease).filter_by(sn=request["sn"], package_id=request["package"], run_id=request["run_id"]).first()
    if existing is None:
        existing = DeviceResourceLease(sn=request["sn"], package_id=request["package"], run_id=request["run_id"],
            project_id=request["project_id"], case_id=request["case_id"], platform=request["platform"],
            leased_at=datetime.now(timezone.utc).isoformat())
        db.add(existing)
    existing.expires_at = datetime.fromtimestamp(time.time() + TTL, timezone.utc).isoformat()
    request["resource_epochs"] = epochs
    request["claimed"] = True
    request["reason"] = ""
    return True
