"""Atomic device/account/domain admission, using the account allocator's transaction."""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import threading
import time
import uuid
from sqlalchemy import text
from mino_nexus.core.database import session_scope
from mino_nexus.core.execution_user import require_user_id, execution_user
from mino_nexus.models.execution_resource import ExecutionResource, ResourceDomain

TTL = 90
_claim = ContextVar("execution_resource_claim", default=None)


def claim_in_transaction(db, account_id=""):
    request = _claim.get()
    if request is None:
        return True
    from mino_nexus.models.resource_ops import DeviceResourceLease
    from mino_nexus.services.run_write_safety import protected
    from mino_nexus.runtime.run_context import is_web_slot
    from mino_nexus.models.run import AppRegressionRun
    run = db.get(AppRegressionRun, request["run_id"])
    from mino_nexus.services.execution_policy import remaining
    if run is None or run.status not in {"running", "queued"} or remaining(run.payload) == 0:
        request["reason"] = "task_not_running"
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
        if old.run_id != request["run_id"] and (not is_web or protected(old.run_id, db=db)):
            request["reason"] = "resource_busy"
            return False
    rows = {key: db.get(ExecutionResource, key) for key in keys}
    for row in rows.values():
        if row and row.state != "released" and (row.run_id != request["run_id"] or row.sn != request["sn"]
                or row.state == "isolated" or row.expires_at < time.time()):
            request["reason"] = "resource_isolated" if row.state == "isolated" or row.expires_at < time.time() else "resource_busy"
            return False
    for key in keys:
        old = rows[key]
        epoch = (old.epoch + 1) if old else 1
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
    request["claimed"] = True
    request["reason"] = ""
    return True


@contextmanager
def admission(doc, case, *, project_id, package, platform):
    request = {"project_id": project_id, "package": package, "platform": platform,
        "run_id": doc["run_id"], "sn": case.get("sn") or doc.get("sn"), "case_id": case["case_id"],
        "execution_unit_id": case["execution_unit_id"], "app_id": doc.get("app_id", ""),
        "env": doc.get("env_profile") or "test", "owner": require_user_id(),
        "attempt_id": "resource-" + uuid.uuid4().hex, "claimed": False, "reason": ""}
    token = _claim.set(request)
    try:
        yield request
    finally:
        _claim.reset(token)


def claim_without_account():
    with session_scope() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        return claim_in_transaction(db)


def renew(request):
    from mino_nexus.models.pool_account import PoolAccount
    with session_scope() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        rows = db.query(ExecutionResource).filter_by(attempt_id=request["attempt_id"], owner_user_id=request["owner"]).all()
        if not rows or any(row.state != "held" or row.expires_at < time.time() for row in rows):
            for row in rows:
                row.state = "isolated"
            return False
        for row in rows:
            row.expires_at = time.time() + TTL
            aid = row.detail.get("account_id")
            account = db.get(PoolAccount, aid) if aid else None
            if account and (account.lease or {}).get("run_id") == request["run_id"]:
                account.lease = {**account.lease, "expires_at": datetime.fromtimestamp(time.time() + TTL, timezone.utc).isoformat()}
        return True


@contextmanager
def heartbeat(request):
    stopped = threading.Event()
    def loop():
        with execution_user(request["owner"]):
            while not stopped.wait(TTL / 3):
                if not renew(request):
                    break
    worker = threading.Thread(target=loop, daemon=True, name="resource-renewal")
    worker.start()
    try:
        yield
    finally:
        stopped.set()
        worker.join(timeout=2)


def release(run_id, sn=""):
    from mino_nexus.services.run_write_safety import protected
    with session_scope() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        query = db.query(ExecutionResource).filter_by(run_id=run_id)
        if sn:
            query = query.filter_by(sn=sn)
        for row in query.all():
            if protected(run_id, db=db, sn=row.sn):
                row.state = "isolated"
            else:
                row.state = "released"


def assert_current(run_id, sn, *, db):
    rows = db.query(ExecutionResource).filter_by(run_id=run_id, sn=sn).filter(ExecutionResource.state != "released").all()
    if rows and any(row.state != "held" or row.expires_at <= time.time() for row in rows):
        raise ValueError("resource_lease_expired")


def wait_for_admission(doc, case, request, acquire_account):
    from mino_nexus.services import run_store
    from mino_nexus.services.execution_policy import remaining
    end = case.get("resource_wait_deadline_at")
    if not end:
        end = time.time() + (doc.get("execution_policy") or {}).get("resource_wait_seconds", 120)
        run_store.patch_case(doc["run_id"], case["case_id"], target_sn=case.get("sn") or doc.get("sn"), resource_wait_deadline_at=end)
        case["resource_wait_deadline_at"] = end
    while True:
        if run_store.task_cancelled(doc["run_id"]) or remaining(doc) == 0:
            return False, "task_not_running"
        ok, reason = acquire_account(max(0, min(15000, int((end - time.time()) * 1000))))
        if ok and not request["claimed"]:
            ok = claim_without_account()
            reason = request["reason"] if not ok else ""
        if ok:
            return True, ""
        if time.time() >= end:
            return False, "resource_wait_deadline_exceeded"
        if request.get("reason") == "resource_isolated":
            return False, "resource_isolated"
        # No part of the resource set is held while waiting.
        time.sleep(min(1, max(0, end - time.time())))
