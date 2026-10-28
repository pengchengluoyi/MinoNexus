"""Atomic device/account/domain admission, using the account allocator's transaction."""
from contextlib import contextmanager
from datetime import datetime, timezone
import threading
import time
from mino_nexus.core.ids import prefixed
from sqlalchemy import text
from mino_nexus.core.database import session_scope
from mino_nexus.platform.identity import require_user_id, execution_user
from mino_nexus.models.execution_resource import ExecutionResource, ResourceDomain

TTL = 90
from mino_nexus.resources.resource_claim_tx import _claim, claim_in_transaction


@contextmanager
def admission(doc, case, *, project_id, package, platform):
    request = {"project_id": project_id, "package": package, "platform": platform,
        "run_id": doc["run_id"], "sn": case.get("sn") or doc.get("sn"), "case_id": case["case_id"],
        "execution_unit_id": case["execution_unit_id"], "app_id": doc.get("app_id", ""),
        "env": doc.get("env_profile") or "test", "owner": require_user_id(),
        "attempt_id": prefixed("resource"), "claimed": False, "reason": ""}
    token = _claim.set(request)
    try:
        yield request
    finally:
        _claim.reset(token)


def claim_without_account():
    with session_scope() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        return claim_in_transaction(db)


def _same_attempt(row, request):
    return all(getattr(row, field) == request.get(key) for field, key in (
        ("run_id", "run_id"), ("owner_user_id", "owner"), ("sn", "sn"),
        ("project_id", "project_id"), ("execution_unit_id", "execution_unit_id"), ("attempt_id", "attempt_id")))


def _attempt_rows(db, request):
    rows = db.query(ExecutionResource).filter_by(attempt_id=request["attempt_id"], owner_user_id=request["owner"]).all()
    expected = request.get("resource_epochs") or {}
    valid = bool(expected) and {row.resource_key: row.epoch for row in rows} == expected
    return rows, valid and all(_same_attempt(row, request) for row in rows)


def renew(request):
    from mino_nexus.models.pool_account import PoolAccount
    from mino_nexus.models.run import AppRegressionRun
    from mino_nexus.models.resource_ops import DeviceResourceLease
    from mino_nexus.casework.execution_policy import remaining
    with session_scope() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        rows, valid = _attempt_rows(db, request)
        current_time = time.time()
        run = db.get(AppRegressionRun, request["run_id"])
        valid = (valid and run is not None and run.owner_user_id == request["owner"]
                 and run.status in {"running", "queued"} and remaining(run.payload) != 0
                 and all(row.state == "held" and row.expires_at > current_time for row in rows))
        accounts = {}
        if valid:
            for account_id in {row.detail.get("account_id") for row in rows} - {"", None}:
                account = db.get(PoolAccount, account_id)
                lease = account.lease or {} if account else {}
                try:
                    expiry = datetime.fromisoformat(str(lease.get("expires_at") or "")).timestamp()
                except (TypeError, ValueError, OverflowError):
                    expiry = 0
                if (account is None or account.locked or account.project_id != request["project_id"]
                        or account.env != request["env"] or expiry <= current_time
                        or any(lease.get(key) != request[key] for key in ("run_id", "case_id", "sn"))):
                    valid = False
                    break
                accounts[account_id] = account
        if not valid:
            for row in rows:
                if (_same_attempt(row, request) and row.state == "held"
                        and (request.get("resource_epochs") or {}).get(row.resource_key) == row.epoch):
                    row.state = "isolated"
            request["reason"] = "resource_lease_lost"
            return False
        expiry = current_time + TTL
        for row in rows:
            row.expires_at = expiry
        expires_at = datetime.fromtimestamp(expiry, timezone.utc).isoformat()
        for account in accounts.values():
            account.lease = {**account.lease, "expires_at": expires_at}
        for lease in db.query(DeviceResourceLease).filter_by(run_id=request["run_id"], sn=request["sn"],
                package_id=request["package"], case_id=request["case_id"]).all():
            lease.expires_at = expires_at
        return True


@contextmanager
def heartbeat(request):
    stopped = threading.Event()
    def loop():
        with execution_user(request["owner"]):
            while not stopped.wait(TTL / 3):
                try:
                    renewed = renew(request)
                except Exception:
                    request["reason"] = "resource_renewal_failed"
                    break
                if not renewed:
                    break
    token = _claim.set(request)
    worker = None
    try:
        if not renew(request):
            raise ValueError("resource_lease_lost")
        worker = threading.Thread(target=loop, daemon=True, name="resource-renewal")
        worker.start()
        yield
    finally:
        stopped.set()
        if worker is not None:
            worker.join(timeout=2)
        _claim.reset(token)


def release(run_id, sn="", *, attempt_id=""):
    from mino_nexus.records.write_state import protected
    with session_scope() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        query = db.query(ExecutionResource).filter_by(run_id=run_id)
        if sn:
            query = query.filter_by(sn=sn)
        if attempt_id:
            query = query.filter_by(attempt_id=attempt_id)
        for row in query.all():
            if row.state == "released":
                continue
            if row.state == "isolated" or row.expires_at <= time.time() or protected(run_id, db=db, sn=row.sn):
                # Expiry/unknown effects require explicit reset verification.
                # Ordinary cleanup is not evidence that the resource is clean.
                row.state = "isolated"
            else:
                row.state = "released"


def assert_current(run_id, sn, *, db):
    request = _claim.get()
    if request is not None:
        if request["run_id"] != run_id or request["sn"] != sn or request.get("reason"):
            raise ValueError("resource_lease_lost")
        rows, valid = _attempt_rows(db, request)
        if not valid or any(row.state != "held" or row.expires_at <= time.time() for row in rows):
            raise ValueError("resource_lease_lost")
    rows = db.query(ExecutionResource).filter_by(run_id=run_id, sn=sn).filter(ExecutionResource.state != "released").all()
    if rows and any(row.state != "held" or row.expires_at <= time.time() for row in rows):
        raise ValueError("resource_lease_expired")

