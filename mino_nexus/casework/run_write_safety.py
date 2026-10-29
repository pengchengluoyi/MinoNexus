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
            "resource_lease_lost": "当前执行的资源租约已失效，未派发新的写动作",
            "resource_lease_expired": "当前设备的资源租约已过期或隔离，未派发新的写动作",
        }
        super().__init__(messages.get(reason_code, "当前任务不可派发新的写动作；保留原动作与资源状态"))


# `unresolved` / `protected` 已下沉到 records/write_state.py（只读、不发放资源）。
# 这里 re-export，既有 `from ...run_write_safety import protected` 的调用点不变。
from mino_nexus.records.write_state import protected, unresolved  # noqa: E402,F401


def merge(old: dict | None, new: dict | None) -> dict:
    # Settled receipts cannot be regressed by a stale run projection; isolation is sticky.
    rank = {"pending": 0, "settled": 1, "isolated": 2, "recovered": 3}
    values = dict((old or {}).get("write_safety") or {})
    for key, value in ((new or {}).get("write_safety") or {}).items():
        if isinstance(value, dict) and rank.get(value.get("state"), -1) >= rank.get((values.get(key) or {}).get("state"), -1):
            previous = values.get(key) or {}
            identity_fields = ("action_ref", "sn", "node_id", "scout_process_generation", "capability_id")
            if any(field in previous and field in value and previous[field] != value[field]
                   for field in identity_fields):
                continue  # Never attach another action's proof to this ledger key.
            merged = dict(value)
            # Stop evidence binds the immutable action, not the age of a task snapshot.
            # A stale isolated projection must not erase a persisted proof.
            if previous:
                for field in identity_fields:
                    if field in previous:
                        merged[field] = previous[field]
            if previous.get("stop_confirmed") is True and previous.get("recovery"):
                merged["stop_confirmed"] = True
                merged["recovery"] = previous["recovery"]
                if merged.get("state") == "isolated":
                    merged["reason_code"] = "resource_reset_verification_required"
            values[key] = merged
    return values


def begin(task_id: str, request, *, node_id: str, scout_process_generation: str = "") -> str:
    from mino_nexus.casework import run_store
    from mino_nexus.records.eventlog.writer import SessionPersistenceError
    key = f"{request.run_id}:{request.step_idx}"
    with run_store._LOCK:
        try:
            doc = run_store.get(task_id)
        except Exception as exc:
            raise SessionPersistenceError("write_intent_read") from exc
        if not doc or not run_store.task_is_live(doc):
            raise WriteAdmissionError("task_not_running")
        from mino_nexus.casework.execution_policy import remaining
        if remaining(doc) == 0:
            raise WriteAdmissionError("batch_deadline_exceeded")
        from mino_nexus.core.database import session_scope
        from mino_nexus.casework.execution_resources import assert_current
        with session_scope() as db:
            try:
                assert_current(task_id, request.sn, db=db)
            except ValueError as exc:
                raise WriteAdmissionError(str(exc)) from exc
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
    from mino_nexus.casework import run_store
    from mino_nexus.records.eventlog.writer import SessionPersistenceError
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
    from mino_nexus.platform.identity import require_user_id
    from mino_nexus.casework import run_store
    from mino_nexus.resources.node_registry import get_registry
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
        if (not isinstance(ref, dict) or not isinstance(ref.get("run_id"), str)
                or not ref.get("run_id") or type(ref.get("step_idx")) is not int):
            outcomes.append({"action_ref": {}, "recovered": False, "stop_confirmed": False,
                             "reason_code": "stop_action_identity_unavailable"})
            continue
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
                    "execution_mode": str(doc.get("execution_mode") or "")})
        try:
            result = await node.send(P.MsgType.EXECUTE, req, timeout=5)
        except Exception:
            outcomes.append({"action_ref": ref, "recovered": False, "stop_confirmed": False,
                             "reason_code": "stop_evidence_unavailable"})
            continue
        proof = (result.data or {}).get("stop_evidence", {}) if result else {}
        proof = proof if isinstance(proof, dict) else {}
        valid = (result is not None and result.run_id == req.run_id and result.step_idx == req.step_idx
                 and proof.get("contract") == "write_stop.v1" and proof.get("stop_confirmed") is True
                 and proof.get("action_ref") == ref and proof.get("sn") == sn
                 and proof.get("scout_process_generation") == current_generation
                 and _proof_mode_valid(proof, generation, current_generation, doc))
        with run_store._LOCK:
            current = run_store.get(task_id)
            key = f"{ref['run_id']}:{ref['step_idx']}"
            stored = ((current or {}).get("write_safety") or {}).get(key)
            live_node, _ = get_registry().resolve(sn)
            if valid and current and not run_store.task_is_live(current) and stored == item and live_node is node and node.scout_process_generation == current_generation:
                # A stopped worker does not prove business data was reset. Record the
                # baseline identity only; session invalidation and account-lease release
                # commit together once every open action is ready.
                from mino_nexus.casework.isolation_exit import baseline_mark
                marked = baseline_mark(sn, str(current.get("package") or ""))
                current["write_safety"][key] = {**item, "state": "isolated", "stop_confirmed": True,
                    "reason_code": "resource_reset_verification_required",
                    "recovery": {"mode": proof.get("mode"), "proof": proof, "reviewed_by": actor, "at": run_store._now(),
                                 "baseline_required": marked, "resource_reset_verified": False}}
                run_store.put(current)
                if marked:
                    baseline.append(marked)
                attempts.add(str(ref.get("run_id") or ""))
            else:
                valid = False
        reason = proof.get("reason_code") or ("worker_completed" if valid else "stop_unconfirmed")
        if not valid and reason in {"worker_completed", "predecessor_retired"}:
            reason = "stop_unconfirmed"
        outcomes.append({"action_ref": ref, "recovered": False, "stop_confirmed": valid,
                         "resource_reset_verified": False, "mode": proof.get("mode") or mode,
                         "reason_code": "resource_reset_verification_required" if valid else reason})
    current = run_store.get(task_id)
    if current and not unresolved(current):
        from mino_nexus.resources.accounts.lease import release_run_lease
        from mino_nexus.casework.run_resource_release import release_run_resource_holdings
        release_run_resource_holdings(current, run_id=task_id)
        if current.get("execution_mode") == "v5":
            for attempt in attempts - {"", task_id}:
                release_run_lease(attempt, app_id=str(current.get("app_id") or ""))  # strict accounts lease per attempt
    # Stop proof does not free the device. The screenshot baseline is the exit.
    return {"task_id": task_id, "actions": outcomes, "resource_isolated": protected(task_id),
            "business_result_unchanged": True, "baseline_required": baseline,
            "isolation_exit": {"committed": False, "blocked_reason": "baseline_recheck_required" if protected(task_id) else "",
                               "release_path": "write-safety/baseline"}}


async def confirm_baseline(task_id: str):
    """结束任务后拍一张当前画面，确认设备还能观察，再解除这台设备上的隔离。不改原任务结论。"""
    from mino_nexus.core.ids import prefixed
    from mino_nexus.loop.router_proxy import RouterProxy
    from mino_nexus.platform.identity import require_user_id
    from mino_nexus.casework import run_store
    from mino_nexus.records.eventlog import store as session_store
    from mino_nexus.records.eventlog.atomic import append_batch
    from mino_nexus.resources.access_scope import require_owned_device

    actor = require_user_id()
    doc = run_store.get(task_id)
    if not doc or doc.get("owner_user_id") != actor:
        raise ValueError("task_not_found")
    if run_store.task_is_live(doc):
        raise ValueError("task_still_running")
    actions = unresolved(doc)
    if not actions:
        raise ValueError("baseline_not_required")
    sns = sorted({str(item.get("sn") or "") for item in actions if str(item.get("sn") or "")})
    if not sns:
        raise ValueError("baseline_device_missing")
    shots = []
    for sn in sns:
        require_owned_device(sn)
        session_id = prefixed("baselinerecheck")
        session_store.open_meta(session_id=session_id, run_id=session_id, case_id="", app_id=str(doc.get("app_id") or ""))
        shot = await RouterProxy(sn, run_id=session_id).observe_async("screenshot", timeout_sec=8)
        if not callable(getattr(shot, "has_image", None)) or not shot.has_image():
            raise ValueError("baseline_screenshot_unavailable")
        refs = append_batch(session_id, [{"type": "observe/screen", "payload": {
            "thumb": "data:" + (shot.image_mime or "image/png") + ";base64," + shot.image_base64,
            "width": shot.width, "height": shot.height, "source": "baseline_recheck",
            "task_id": task_id,
        }}], {"status": "done", "summary": "隔离解除前的当前画面"})
        shots.append({"sn": sn, "session_id": session_id, **(refs[0] if refs else {})})
    from mino_nexus.casework.isolation_exit import commit_baseline_release
    released = commit_baseline_release(task_id, actor=actor, sns=set(sns), observations=shots)
    if not released.get("committed"):
        raise ValueError(released.get("blocked_reason") or "baseline_not_required")
    return {
        "task_id": task_id,
        "resource_isolated": protected(task_id),
        "business_result_unchanged": True,
        "verification_kind": "baseline_recheck",
        "observations": shots,
        "resources_released": len(released.get("device_resources_released") or []),
        "accounts_released": len(released.get("account_leases_released") or []),
        "account_leases_released": released.get("account_leases_released") or [],
        "isolation_exit": released,
    }


def _require_fresh_baseline(sn: str, package: str) -> dict:
    """Baseline identity only. The session row is updated by the isolation exit.

    Writing ``device_app_sessions`` here cleared ``lease_run_id`` while pool
    account leases stayed held, so the device looked free and the next claim
    returned ``resource_busy``.
    """
    from mino_nexus.casework.isolation_exit import baseline_mark
    return baseline_mark(sn, package)
