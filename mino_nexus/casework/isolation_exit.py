"""One exit for sticky write isolation and the account leases that belong to it.

Stop proof alone does not free a device. Deleting ledger rows does not either:
``release()`` keeps an isolated or protected execution resource isolated, and the
non-Web claim key is ``device:<sn>``. When every open action has stop evidence
and a package baseline, this commits device-session invalidation, execution
resources, device leases, reservations, and pool account leases in one transaction.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import time


def baseline_mark(sn: str, package: str) -> dict:
    """Identity of the baseline the exit will apply. Does not touch the session row."""
    device = str(sn or "").strip()
    pkg = str(package or "").strip()
    if not device or not pkg:
        return {}
    return {"sn": device, "package_id": pkg, "session": "unknown"}


def lease_aliases(run_id: str, doc: dict | None, actions: list | None = None) -> set[str]:
    """Task, write-action, and case session ids that may still hold a pool lease."""
    aliases = {str(run_id or "").strip()}
    found = list(actions or [])
    safety = (doc or {}).get("write_safety") or {}
    if isinstance(safety, dict):
        found.extend(value for value in safety.values() if isinstance(value, dict))
    for action in found:
        ref = action.get("action_ref") if isinstance(action, dict) else None
        rid = str((ref or {}).get("run_id") or "").strip()
        if rid:
            aliases.add(rid)
    for case in (doc or {}).get("cases") or []:
        if isinstance(case, dict):
            rid = str(case.get("report_run_id") or "").strip()
            if rid:
                aliases.add(rid)
    aliases.discard("")
    return aliases


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _open_actions(doc: dict | None) -> list[tuple[str, dict]]:
    safety = (doc or {}).get("write_safety") or {}
    if not isinstance(safety, dict):
        return []
    return [(str(key), value) for key, value in safety.items()
            if isinstance(value, dict) and value.get("state") in {"pending", "isolated"}]


def _proof_ok(action: dict, doc: dict | None) -> bool:
    if action.get("stop_confirmed") is not True:
        return False
    recovery = action.get("recovery") if isinstance(action.get("recovery"), dict) else {}
    proof = recovery.get("proof") if isinstance(recovery.get("proof"), dict) else {}
    if (proof.get("contract") == "write_stop.v1" and proof.get("stop_confirmed") is True
            and proof.get("action_ref") == action.get("action_ref") and proof.get("sn") == action.get("sn")):
        return True
    receipt_id = str(recovery.get("receipt_id") or "")
    if recovery.get("source") == "late_stop_receipt" and receipt_id:
        receipt = ((doc or {}).get("write_reconciliation") or {}).get(receipt_id) or {}
        return (isinstance(receipt, dict) and receipt.get("stop_confirmed") is True
                and receipt.get("action_ref") == action.get("action_ref"))
    return False


def _is_web_lane(sn: str, doc: dict | None = None) -> bool:
    from mino_nexus.contract.execution.device import is_per_node_web_sn, is_web_slot

    device = str(sn or "").strip()
    plat = str((doc or {}).get("platform") or "")
    return is_per_node_web_sn(device) or is_web_slot(device, plat or "web")


def lane_release_block(doc: dict | None, sn: str) -> str:
    """Web lane 只要求停机证据，不要求设备包名基线。"""
    actions = [action for _, action in _open_actions(doc) if str(action.get("sn") or "") == sn]
    if not actions:
        return "baseline_not_required"
    for action in actions:
        if not _proof_ok(action, doc):
            return "stop_evidence_required"
    return ""


def _baseline_package(action: dict, doc: dict | None) -> str:
    recovery = action.get("recovery") if isinstance(action.get("recovery"), dict) else {}
    mark = recovery.get("baseline_required") if isinstance(recovery.get("baseline_required"), dict) else {}
    return str(mark.get("package_id") or (doc or {}).get("package") or "").strip()


def exit_ready(doc: dict | None) -> tuple[bool, str]:
    open_actions = _open_actions(doc)
    if not open_actions:
        return False, "nothing_open"
    for _, action in open_actions:
        if not _proof_ok(action, doc):
            return False, "stop_evidence_required"
        sn = str(action.get("sn") or "").strip()
        if _is_web_lane(sn, doc):
            continue
        if not sn or not _baseline_package(action, doc):
            return False, "baseline_package_missing"
    return True, ""


def _matching_accounts(db, aliases: set[str]) -> list:
    if not aliases:
        return []
    from mino_nexus.models.pool_account import PoolAccount
    found = []
    for account in db.query(PoolAccount).all():
        lease = account.lease if isinstance(account.lease, dict) else {}
        if str(lease.get("run_id") or "").strip() in aliases:
            found.append(account)
    return found


def held_account_ids(db, aliases: set[str]) -> list[str]:
    return [str(account.id) for account in _matching_accounts(db, aliases)]


def clear_alias_leases(db, aliases: set[str]) -> list[str]:
    """Clear pool leases directly. ``set_account_lease`` refuses while isolation is still protected."""
    cleared = []
    stamp = int(time.time())
    for account in _matching_accounts(db, aliases):
        account.lease = {}
        account.updated_at = stamp
        cleared.append(str(account.id))
    return cleared


def release_execution_resources(db, run) -> list[str]:
    from mino_nexus.models.execution_resource import ExecutionResource
    rows = (db.query(ExecutionResource)
            .filter_by(run_id=run.run_id, owner_user_id=run.owner_user_id)
            .filter(ExecutionResource.state != "released").all())
    keys = []
    for row in rows:
        row.epoch = int(row.epoch or 0) + 1
        row.state = "released"
        row.expires_at = 0
        keys.append(str(row.resource_key))
    return keys


def delete_device_leases(db, aliases: set[str]) -> None:
    from mino_nexus.models.resource_ops import DeviceResourceLease
    if aliases:
        db.query(DeviceResourceLease).filter(DeviceResourceLease.run_id.in_(aliases)).delete(synchronize_session=False)


def delete_reservations(db, run_id: str) -> None:
    from mino_nexus.models.device_reservation import DeviceReservation
    db.query(DeviceReservation).filter_by(run_id=run_id).delete(synchronize_session=False)


def apply_baselines(db, doc: dict | None) -> list[dict]:
    """Invalidate device sessions in the caller's transaction, not a separate commit."""
    from mino_nexus.models.device_app_session import DeviceAppSession
    applied = []
    seen: set[tuple[str, str]] = set()
    for _, action in _open_actions(doc):
        sn = str(action.get("sn") or "").strip()
        package = _baseline_package(action, doc)
        if _is_web_lane(sn, doc) or not sn or not package or (sn, package) in seen:
            continue
        seen.add((sn, package))
        row = db.query(DeviceAppSession).filter_by(sn=sn, package_id=package).first()
        stamp = _now()
        if row is None:
            row = DeviceAppSession(sn=sn, package_id=package, observed_at=stamp)
            db.add(row)
        row.session = "unknown"
        row.bound_account_id = ""
        row.stale = 1
        row.stale_reason = "write_recovered_unknown"
        row.lease_run_id = ""
        row.updated_at = stamp
        row.observed_at = stamp
        applied.append({"sn": sn, "package_id": package, "session": "unknown"})
    return applied


def _mark_recovered(doc: dict) -> None:
    stamp = _now()
    safety = dict(doc.get("write_safety") or {})
    for key, action in list(safety.items()):
        if not isinstance(action, dict) or action.get("state") not in {"pending", "isolated"}:
            continue
        recovery = dict(action.get("recovery") or {})
        recovery.update({
            "prior_reason_code": str(action.get("reason_code") or ""),
            "verification_kind": "stop_and_baseline",
            "resource_reset_verified": False,
            "account_leases_released": True,
            "released_at": stamp,
        })
        safety[key] = {**action, "state": "recovered", "reason_code": "", "recovery": recovery}
    doc["write_safety"] = safety


def _reservation_for_sn(key: str, sn: str) -> bool:
    return key == f"device:{sn}" or key.startswith(f"web:{sn}") or key.startswith(f"device:{sn}:")


def commit_baseline_release(task_id: str, *, actor: str, sns: set[str], observations: list) -> dict:
    """Screenshot already succeeded. Free these devices and their account leases together."""
    from sqlalchemy import text
    from sqlalchemy.orm.attributes import flag_modified
    from mino_nexus.core.database import session_scope
    from mino_nexus.core.log import SLog
    from mino_nexus.models.device_reservation import DeviceReservation
    from mino_nexus.models.execution_resource import ExecutionResource
    from mino_nexus.models.resource_ops import DeviceResourceLease
    from mino_nexus.models.run import AppRegressionRun
    from mino_nexus.casework import run_store

    task_id = str(task_id or "").strip()
    devices = {str(sn or "").strip() for sn in sns if str(sn or "").strip()}
    empty = {"committed": False, "blocked_reason": "", "verification_kind": "baseline_recheck",
             "resource_reset_verified": True, "business_result_unchanged": True,
             "account_leases_released": [], "account_leases_held": [],
             "device_resources_released": [], "baseline_applied": []}
    with run_store._LOCK:
        committed = False
        with session_scope() as db:
            db.execute(text("BEGIN IMMEDIATE"))
            run = db.get(AppRegressionRun, task_id)
            if run is None or run.owner_user_id != actor:
                return {**empty, "blocked_reason": "task_not_found"}
            if run.status in {"running", "queued"} or not run.finished_at:
                return {**empty, "blocked_reason": "task_still_running"}
            doc = deepcopy(run.payload or {})
            open_actions = [(key, action) for key, action in _open_actions(doc)
                            if str(action.get("sn") or "") in devices]
            if not open_actions:
                return {**empty, "blocked_reason": "baseline_not_required"}
            aliases = lease_aliases(task_id, doc, [action for _, action in open_actions])
            accounts = []
            stamp = int(time.time())
            for account in _matching_accounts(db, aliases):
                lease = account.lease if isinstance(account.lease, dict) else {}
                lease_sn = str(lease.get("sn") or "").strip()
                if lease_sn and lease_sn not in devices:
                    continue
                account.lease = {}
                account.updated_at = stamp
                accounts.append(str(account.id))
            rows = (db.query(ExecutionResource)
                    .filter_by(run_id=run.run_id, owner_user_id=run.owner_user_id)
                    .filter(ExecutionResource.sn.in_(devices), ExecutionResource.state != "released").all())
            keys = []
            for row in rows:
                row.epoch = int(row.epoch or 0) + 1
                row.state = "released"
                row.expires_at = 0
                keys.append(str(row.resource_key))
            if aliases:
                db.query(DeviceResourceLease).filter(
                    DeviceResourceLease.run_id.in_(aliases),
                    DeviceResourceLease.sn.in_(devices),
                ).delete(synchronize_session=False)
            for reservation in db.query(DeviceReservation).filter_by(run_id=task_id).all():
                key = str(reservation.resource_key or "")
                if any(_reservation_for_sn(key, sn) for sn in devices):
                    db.delete(reservation)
            package = str(doc.get("package") or "").strip()
            baselines = []
            if package:
                from mino_nexus.models.device_app_session import DeviceAppSession
                seen = set()
                for sn in sorted(devices):
                    if _is_web_lane(sn, doc) or (sn, package) in seen:
                        continue
                    seen.add((sn, package))
                    row = db.query(DeviceAppSession).filter_by(sn=sn, package_id=package).first()
                    now = _now()
                    if row is None:
                        row = DeviceAppSession(sn=sn, package_id=package, observed_at=now)
                        db.add(row)
                    row.session = "unknown"
                    row.bound_account_id = ""
                    row.stale = 1
                    row.stale_reason = "write_recovered_unknown"
                    row.lease_run_id = ""
                    row.updated_at = now
                    row.observed_at = now
                    baselines.append({"sn": sn, "package_id": package, "session": "unknown"})
            reviewed = _now()
            safety = dict(doc.get("write_safety") or {})
            open_keys = {key for key, _ in open_actions}
            for key, action in list(safety.items()):
                if key not in open_keys:
                    continue
                recovery = dict(action.get("recovery") or {})
                recovery.update({
                    "prior_reason_code": str(action.get("reason_code") or ""),
                    "resource_reset_verified": True,
                    "verification_kind": "baseline_recheck",
                    "reviewed_by": actor,
                    "at": reviewed,
                    "observations": observations,
                    "account_leases_released": True,
                })
                safety[key] = {**action, "state": "recovered", "stop_confirmed": True, "reason_code": "", "recovery": recovery}
            doc["write_safety"] = safety
            run.payload = doc
            flag_modified(run, "payload")
            db.flush()
            result = {**empty, "committed": True, "account_leases_released": accounts,
                      "device_resources_released": keys, "baseline_applied": baselines}
            committed = True
        if committed:
            run_store._LIVE.pop(task_id, None)
            SLog.i("IsolationExit", f"baseline task={task_id} accounts={len(result['account_leases_released'])} "
                   f"resources={len(result['device_resources_released'])}")
        return result


def try_commit_stop_baseline_exit(task_id: str) -> dict:
    """Release isolation and account leases together, or leave both held."""
    from sqlalchemy import text
    from mino_nexus.core.database import session_scope
    from mino_nexus.core.log import SLog
    from mino_nexus.models.run import AppRegressionRun
    from mino_nexus.casework import run_store

    task_id = str(task_id or "").strip()
    empty = {"committed": False, "blocked_reason": "", "verification_kind": "",
             "resource_reset_verified": False, "business_result_unchanged": True,
             "account_leases_released": [], "account_leases_held": [],
             "device_resources_released": [], "baseline_applied": []}
    with run_store._LOCK:
        committed = False
        with session_scope() as db:
            db.execute(text("BEGIN IMMEDIATE"))
            run = db.get(AppRegressionRun, task_id)
            if run is None:
                return {**empty, "blocked_reason": "task_not_found"}
            if run.status in {"running", "queued"} or not run.finished_at:
                return {**empty, "blocked_reason": "task_still_running"}
            doc = deepcopy(run.payload or {})
            ready, blocked = exit_ready(doc)
            aliases = lease_aliases(task_id, doc)
            if not ready:
                return {**empty, "blocked_reason": blocked, "account_leases_held": held_account_ids(db, aliases)}
            accounts = clear_alias_leases(db, aliases)
            keys = release_execution_resources(db, run)
            delete_device_leases(db, aliases)
            delete_reservations(db, task_id)
            baselines = apply_baselines(db, doc)
            _mark_recovered(doc)
            run.payload = doc
            from sqlalchemy.orm.attributes import flag_modified
            flag_modified(run, "payload")
            db.flush()
            result = {**empty, "committed": True, "verification_kind": "stop_and_baseline",
                      "account_leases_released": accounts, "device_resources_released": keys,
                      "baseline_applied": baselines}
            committed = True
        if committed:
            run_store._LIVE.pop(task_id, None)
            SLog.i("IsolationExit", f"released task={task_id} accounts={len(result['account_leases_released'])} "
                   f"resources={len(result['device_resources_released'])}")
        return result
