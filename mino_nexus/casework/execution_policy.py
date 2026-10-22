"""Persisted absolute limits. Model retries and process restarts cannot extend them."""
import time


from mino_nexus.resources.run_budget import deadline, remaining  # noqa: F401


def start_attempt(run_id, case):
    from mino_nexus.casework import run_store
    from mino_nexus.contract.step_pointer import compute_case_wall_budget_sec
    with run_store._LOCK:
        doc = run_store.get(run_id)
        if not doc or not run_store.task_is_live(doc):
            raise ValueError("task_not_running")
        current = next((row for row in doc.get("cases") or [] if row.get("execution_unit_id") == case.get("execution_unit_id")), None)
        if current is None:
            raise ValueError("execution_unit_missing")
        end = current.get("attempt_deadline_at") or time.time() + compute_case_wall_budget_sec(current)
        batch = deadline(doc)
        end = min(end, batch) if batch else end
        current["attempt_deadline_at"] = end
        current["status"] = "running"
        run_store.put(doc)
        case["attempt_deadline_at"] = end
        return end


def expire_runs():
    from mino_nexus.platform.identity import execution_user
    from mino_nexus.casework import run_store
    from mino_nexus.records.write_state import unresolved
    expired = []
    offset = 0
    while True:
        rows = run_store._fetch_payloads(limit=80, offset=offset, status="running")
        if not rows:
            break
        for snapshot in rows:
            if remaining(snapshot) != 0:
                continue
            with execution_user(snapshot["owner_user_id"]):
                with run_store._LOCK:
                    doc = run_store.get(snapshot["run_id"])
                    if not doc or not run_store.task_is_live(doc):
                        continue
                    for guard in unresolved(doc):
                        guard.update(state="isolated", isolation_reason="batch_deadline_exceeded")
                    for case in doc.get("cases") or []:
                        if case.get("status") in {"pending", "running"}:
                            case.update(status="blocked", reason_code="batch_deadline_exceeded",
                                execution_started="no" if case.get("status") == "pending" else "unknown",
                                summary="批次预算已耗尽；未确认资源继续隔离")
                    run_store.put(doc)
                    run_store.finish(doc["run_id"], status="failed", error="batch_deadline_exceeded")
                    expired.append(doc["run_id"])
        # Finished rows leave the live set. Count only surviving rows for next page.
        offset += sum(row["run_id"] not in expired for row in rows)
    return expired
