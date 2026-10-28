"""资源认领事务上下文：由用例侧 `admission()` 设置，资源侧读取。"""
from __future__ import annotations

from contextvars import ContextVar

_claim = ContextVar("execution_resource_claim", default=None)


def _without_code(info: dict) -> dict:
    return {key: value for key, value in info.items() if key != "reason_code"}


def deny_claim(reason: str, reason_code: str = "", **detail) -> bool:
    """Keep the coarse ``reason`` and add the ledger code callers can schedule on."""
    request = _claim.get()
    if isinstance(request, dict):
        request["reason"] = reason
        request["reason_code"] = str(reason_code or reason)
        request["reason_detail"] = {key: value for key, value in detail.items()
                                    if value not in (None, "", {}, [])}
    return False


def claim_failure_code(default: str = "") -> str:
    request = _claim.get()
    if not isinstance(request, dict):
        return default
    return str(request.get("reason_code") or request.get("reason") or default)


def holder_reason(db, run_id: str, sn: str = "") -> dict:
    """Finer ledger reason for the run that still holds a device or account."""
    from mino_nexus.models.run import AppRegressionRun
    from mino_nexus.records.write_state import unresolved
    row = db.get(AppRegressionRun, run_id) if run_id else None
    if row is None and run_id:
        from mino_nexus.models.session_event import SessionMeta
        attempt = db.get(SessionMeta, run_id)
        if attempt is not None:
            parent = db.get(AppRegressionRun, attempt.run_id)
            if parent is not None and attempt.owner_user_id and attempt.owner_user_id == parent.owner_user_id:
                row = parent
    actions = unresolved(row.payload if row is not None else None, sn)
    action = actions[0] if actions else {}
    code = str(action.get("reason_code") or "")
    if not code and action.get("state") == "pending":
        code = "write_outcome_unknown"
    if not code and action.get("state") == "isolated":
        code = "write_safety_isolated"
    return {"reason_code": code, "holder_run_id": str((row.run_id if row is not None else run_id) or ""),
            "ledger_state": str(action.get("state") or ""),
            "action_ref": action.get("action_ref") if isinstance(action.get("action_ref"), dict) else {},
            "open_actions": len(actions)}


def admission_block(run_id: str, sn: str = "") -> dict:
    """Busy admission detail for schedulers. Coarse reason stays ``resource_busy`` / ``resource_isolated``."""
    from mino_nexus.core.database import session_scope
    with session_scope() as db:
        info = holder_reason(db, run_id, sn)
    code = str(info.get("reason_code") or "")
    coarse = "resource_isolated" if info.get("ledger_state") in {"pending", "isolated"} else "resource_busy"
    return {"reason": coarse, "reason_code": code or coarse, "reason_detail": {k: v for k, v in info.items() if v not in (None, "", {}, [])}}


def _same_attempt(row, request) -> bool:
    return all(getattr(row, field) == request.get(key) for field, key in (
        ("run_id", "run_id"), ("owner_user_id", "owner"), ("sn", "sn"),
        ("project_id", "project_id"), ("execution_unit_id", "execution_unit_id"),
        ("attempt_id", "attempt_id")))


def claim_in_transaction(db, account_id=""):
    request = _claim.get()
    if request is None:
        return True
    request["reason"] = ""
    request["reason_code"] = ""
    request["reason_detail"] = {}
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
    is_web = is_web_slot(request["sn"], request["platform"])
    keys = ["device:" + request["sn"] + (":" + request["run_id"] if is_web else "")]
    if account_id:
        keys.append("account:" + request["project_id"] + ":" + account_id)
    domains = db.query(ResourceDomain).filter_by(project_id=request["project_id"], env=request["env"], enabled=1).all()
    keys += ["domain:" + row.domain_id for row in domains if (not row.app_id or row.app_id == request["app_id"])
        and (not row.account_ids or account_id in row.account_ids)]
    if protected(request["run_id"], db=db, sn=request["sn"]):
        info = holder_reason(db, request["run_id"], request["sn"])
        return deny_claim("resource_isolated", info.get("reason_code") or "write_safety_isolated", **_without_code(info))
    for old in db.query(DeviceResourceLease).filter_by(sn=request["sn"]).all():
        if old.run_id != request["run_id"] and (not is_web or protected(old.run_id, db=db, sn=str(old.sn or ''))):
            isolated = protected(old.run_id, db=db, sn=str(old.sn or ""))
            info = holder_reason(db, old.run_id, str(old.sn or ""))
            code = info.get("reason_code") or ("write_safety_isolated" if isolated else "resource_device_lease_held")
            return deny_claim("resource_busy", code, holder_isolated=isolated, sn=str(old.sn or ""),
                              package_id=str(old.package_id or ""), **_without_code(info))
    rows = {key: db.get(ExecutionResource, key) for key in keys}
    for row in rows.values():
        if row and row.state != "released" and (not _same_attempt(row, request)
                or row.state != "held" or row.expires_at <= time.time()):
            expired = row.expires_at <= time.time()
            isolated = row.state != "held" or expired
            info = holder_reason(db, row.run_id, row.sn)
            if isolated:
                code = info.get("reason_code") or ("resource_lease_expired" if expired else "resource_state_isolated")
                coarse = "resource_isolated"
            else:
                code = info.get("reason_code") or "resource_held_by_other_attempt"
                coarse = "resource_busy"
            return deny_claim(coarse, code, resource_key=row.resource_key, state=row.state,
                              epoch=row.epoch, **_without_code(info))
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
    request["reason_code"] = ""
    request["reason_detail"] = {}
    return True
