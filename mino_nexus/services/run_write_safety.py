"""N0 write safety in the existing run record; not the N2 binding contract."""
from __future__ import annotations
from typing import Any


class WriteAdmissionError(RuntimeError):
    def __init__(self, reason_code: str, *, blocking_actions=()):
        self.reason_code = reason_code
        self.blocking_actions = [dict(item) for item in blocking_actions]
        messages = {
            "task_not_running": "任务已不在执行中，未派发新的写动作",
            "run_write_stopped": "当前设备存在未结算或已隔离的写动作，未派发新的写动作；保留原动作与资源状态",
        }
        super().__init__(messages.get(reason_code, "当前任务不可派发新的写动作；保留原动作与资源状态"))


def unresolved(doc: dict | None, sn: str = "") -> list[dict]:
    values = (doc or {}).get("write_safety") or {}
    return [v for v in values.values() if isinstance(v, dict)
            and v.get("state") in {"pending", "isolated"} and (not sn or v.get("sn") == sn)]


def merge(old: dict | None, new: dict | None) -> dict:
    # Settled receipts cannot be regressed by a stale run projection; isolation is sticky.
    rank = {"pending": 0, "settled": 1, "isolated": 2, "recovered": 3}
    values = dict((old or {}).get("write_safety") or {})
    for key, value in ((new or {}).get("write_safety") or {}).items():
        if isinstance(value, dict) and rank.get(value.get("state"), -1) >= rank.get((values.get(key) or {}).get("state"), -1):
            values[key] = dict(value)
    return values


def protected(run_id: str, *, db=None, sn: str = "") -> bool:
    if not run_id:
        return False
    from mino_nexus.models.run import AppRegressionRun
    try:
        if db is not None:
            row = db.get(AppRegressionRun, run_id)
            return bool(row and unresolved(row.payload, sn))
        from mino_nexus.core.database import session_scope
        with session_scope() as session:
            return protected(run_id, db=session, sn=sn)
    except Exception:
        return True  # 状态不可读不能成为释放设备或账号的依据。


def begin(task_id: str, request, *, node_id: str, scout_process_generation: str = "") -> str:
    from mino_nexus.services import run_store
    from mino_nexus.loop.observe.session_log import SessionPersistenceError
    key = f"{request.run_id}:{request.step_idx}"
    with run_store._LOCK:
        try:
            doc = run_store.get(task_id)
        except Exception as exc:
            raise SessionPersistenceError("write_intent_read") from exc
        if not doc or not run_store.task_is_live(doc):
            raise WriteAdmissionError("task_not_running")
        blockers = unresolved(doc, request.sn)
        if blockers:
            raise WriteAdmissionError("run_write_stopped", blocking_actions=[
                {"action_ref": item.get("action_ref"), "capability_id": item.get("capability_id"), "state": item.get("state")}
                for item in blockers[:16]])
        values = dict(doc.get("write_safety") or {})
        if key in values:
            raise SessionPersistenceError("write_identity_reused")
        values[key] = {"state": "pending", "sn": request.sn, "node_id": node_id,
                       "action_ref": {"run_id": request.run_id, "step_idx": request.step_idx},
                       "capability_id": request.capability_id, "stop_confirmed": False,
                       "scout_process_generation": scout_process_generation}
        doc["write_safety"] = values
        try:
            run_store.put(doc)
        except Exception as exc:
            raise SessionPersistenceError("write_intent") from exc
    return key


def finish(task_id: str, key: str, raw: dict[str, Any], *, passed: bool) -> bool:
    from mino_nexus.services import run_store
    from mino_nexus.loop.observe.session_log import SessionPersistenceError
    receipt = raw.get("execution")
    value = receipt.get("execution_started") if isinstance(receipt, dict) else raw.get("execution_started")
    boundary = "yes" if value is True or type(value) is str and value == "yes" else "no" if value is False or type(value) is str and value == "no" else "unknown"
    control = raw.get("execution_control") if isinstance(raw.get("execution_control"), dict) else {}
    isolated = bool(control.get("resource_isolated")) or bool(control.get("stop_run")) or boundary == "unknown" or (not passed and boundary != "no")
    with run_store._LOCK:
        try:
            doc = run_store.get(task_id)
        except Exception as exc:
            raise SessionPersistenceError("write_receipt_read") from exc
        values = dict((doc or {}).get("write_safety") or {})
        if not doc or key not in values:
            raise SessionPersistenceError("write_receipt_missing")
        values[key] = {**values[key], "state": "isolated" if isolated else "settled",
                       "execution_started": boundary, "stop_confirmed": not isolated,
                       "reason_code": str(control.get("reason_code") or ("write_outcome_unknown" if isolated else ""))}
        doc["write_safety"] = values
        try:
            run_store.put(doc)
        except Exception as exc:
            raise SessionPersistenceError("write_receipt") from exc
    return isolated


_CLOCK_SKEW_SEC = 120


def _finished_epoch(doc: dict) -> float:
    from datetime import datetime
    try:
        return datetime.fromisoformat(str(doc.get("finished_at") or "")).timestamp()
    except ValueError:
        return 0.0


def _proof_mode_valid(proof: dict, generation: str, current_generation: str, doc: dict) -> bool:
    if proof.get("mode") == "same_process":
        if doc.get("execution_mode") == "v5" and proof.get("lane_terminated") is not True:
            return False
        # A legacy intent without generation counts only if this process still holds the exact action record.
        return generation == current_generation or (not generation and proof.get("action_present") is True)
    if proof.get("mode") != "retired_generation" or generation == current_generation:
        return False
    if not (proof.get("action_present") is False and proof.get("single_instance_lock") is True
            and proof.get("foreign_processes") == 0):
        return False
    if generation:
        return True
    # Without a recorded generation, the current process must have started after the task ended.
    started, finished = proof.get("process_started_at"), _finished_epoch(doc)
    return bool(finished) and type(started) in (int, float) and started > finished + _CLOCK_SKEW_SEC


async def recover_stopped(task_id: str):
    """Explicit owner-authorized recheck. Old generations recover only with retirement proof."""
    import uuid
    from mino_nexus.core import protocol as P
    from mino_nexus.core.execution_user import require_user_id
    from mino_nexus.services import run_store
    from mino_nexus.services.node_registry import get_registry
    actor = require_user_id()
    doc = run_store.get(task_id)
    if not doc or doc.get("owner_user_id") != actor:
        raise ValueError("task_not_found")
    if run_store.task_is_live(doc):
        raise ValueError("task_still_running")
    if not doc.get("write_safety"):
        raise ValueError("recovery_evidence_unsupported")
    outcomes, baseline, attempts = [], [], set()
    for item in unresolved(doc):
        ref, sn = item.get("action_ref") or {}, item.get("sn", "")
        node, _ = get_registry().resolve(sn)
        generation = str(item.get("scout_process_generation") or "")
        current_generation = str(getattr(node, "scout_process_generation", "") or "")
        if node is None or not node.send or node.owner_user_id != actor or node.node_id != item.get("node_id") or not current_generation:
            outcomes.append({"action_ref": ref, "recovered": False, "reason_code": "stop_evidence_unavailable"})
            continue
        mode = "same_process" if generation == current_generation else "retired_generation"
        req = P.Execute(run_id=ref["run_id"], step_idx=-(uuid.uuid4().int % (2**48) + 1),
            sn=sn, capability_id="write_stop_status",
            params={"action_ref": ref, "scout_process_generation": generation, "mode": mode,
                    "execution_mode": str(doc.get("execution_mode") or "legacy")})
        result = await node.send(P.MsgType.EXECUTE, req, timeout=5)
        proof = (result.data or {}).get("stop_evidence", {}) if result else {}
        valid = (proof.get("contract") == "write_stop.v1" and proof.get("stop_confirmed") is True
                 and proof.get("action_ref") == ref and proof.get("sn") == sn
                 and proof.get("scout_process_generation") == current_generation
                 and _proof_mode_valid(proof, generation, current_generation, doc))
        with run_store._LOCK:
            current = run_store.get(task_id)
            key = f"{ref['run_id']}:{ref['step_idx']}"
            stored = ((current or {}).get("write_safety") or {}).get(key)
            live_node, _ = get_registry().resolve(sn)
            if valid and current and not run_store.task_is_live(current) and stored == item and live_node is node and node.scout_process_generation == current_generation:
                # Stale before the put: the put may free the reservation for the next task.
                marked = _require_fresh_baseline(sn, str(current.get("package") or ""))
                current["write_safety"][key] = {**item, "state": "recovered", "stop_confirmed": True,
                    "recovery": {"mode": proof.get("mode"), "proof": proof, "reviewed_by": actor, "at": run_store._now(),
                                 "baseline_required": marked}}
                run_store.put(current)  # Durable evidence precedes reservation/lease release.
                if marked:
                    baseline.append(marked)
                attempts.add(str(ref.get("run_id") or ""))
            else:
                valid = False
        reason = proof.get("reason_code") or ("worker_completed" if valid else "stop_unconfirmed")
        if not valid and reason in {"worker_completed", "predecessor_retired"}:
            reason = "stop_unconfirmed"
        outcomes.append({"action_ref": ref, "recovered": valid, "mode": proof.get("mode") or mode, "reason_code": reason})
    current = run_store.get(task_id)
    if current and not unresolved(current):
        from mino_nexus.services.account_lease import release_run_lease
        from mino_nexus.services.run_resource_release import release_run_resource_holdings
        release_run_resource_holdings(current, run_id=task_id)
        if current.get("execution_mode") == "v5":
            for attempt in attempts - {"", task_id}:
                release_run_lease(attempt, app_id=str(current.get("app_id") or ""))  # strict accounts lease per attempt
    return {"task_id": task_id, "actions": outcomes, "resource_isolated": bool(unresolved(current)),
            "business_result_unchanged": True, "baseline_required": baseline}


def _require_fresh_baseline(sn: str, package: str) -> dict:
    """A recovered unknown write leaves page, login state and identity unproven for the next task."""
    if not sn or not package:
        return {}
    from mino_nexus.services.device_app_session_store import upsert_session
    upsert_session(sn, package, session="unknown", clear_binding=True, stale=True,
                   stale_reason="write_recovered_unknown", lease_run_id="", source="write_recovery")
    return {"sn": sn, "package_id": package, "session": "unknown"}
