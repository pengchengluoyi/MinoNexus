"""Read-only retry diagnostics. Admission remains closed until reset is provable.

Never infer safe replay from a terminal verdict, stopped executor, elapsed lease,
or a publication's declaration that its actions are reversible.
"""
from __future__ import annotations

import math
import time

from mino_nexus.core.execution_user import require_user_id


def assess(doc: dict, *, resources: list[dict], now: float | None = None) -> dict:
    """Pure projection; retain each failed device/unit instead of deduplicating cases."""
    from mino_nexus.services.run_write_safety import unresolved

    now = time.time() if now is None else now
    blockers = []

    def block(code, message):
        blockers.append({"reason_code": code, "message": message})

    if doc.get("status") in {"running", "queued"}:
        block("task_still_running", "原任务尚未结束")
    if unresolved(doc):
        block("write_outcome_unresolved", "存在未结算或隔离的写动作，停止确认不等于资源复位")
    if any(row.get("state") != "released" for row in resources):
        block("resource_not_released", "原任务仍持有或隔离资源，不能重新领取并复跑")
    raw_deadline = (doc.get("execution_policy") or {}).get("batch_deadline_at")
    valid_deadline = type(raw_deadline) in (int, float) and math.isfinite(raw_deadline) and raw_deadline > 0
    remaining = max(0.0, raw_deadline - now) if valid_deadline else None
    if not valid_deadline:
        block("retry_budget_unavailable", "缺少有效的原批次截止时间，不能为复跑重新分配预算")
    elif remaining == 0:
        block("batch_deadline_exceeded", "原批次预算已耗尽")

    units = []
    for case in doc.get("cases") or []:
        if case.get("status") not in {"fail", "blocked", "declined"}:
            continue
        publication = case.get("v5_publication") or {}
        policy = publication.get("retry_policy") or {}
        units.append({
            "execution_unit_id": case.get("execution_unit_id") or "",
            "case_id": case.get("case_id") or "",
            "sn": case.get("sn") or doc.get("sn") or "",
            "status": case.get("status"),
            "reason_code": case.get("reason_code") or "",
            "attempt_count": len(case.get("attempts") or []),
            "published_retry_limit": policy.get("max", 0),
        })
    if not units:
        block("no_failed_execution_units", "没有失败执行单元可复跑")
    elif any(not unit["execution_unit_id"] for unit in units):
        block("execution_unit_identity_missing", "历史记录缺少执行单元身份，不能按用例编号猜测复跑范围")

    # No production reset-proof consumer exists yet. This must remain an explicit
    # product limitation, never an optimistic permission derived from missing data.
    block("retry_admission_not_ready", "安全复跑尚未开放：需完成资源复位证明、发布策略和原预算下的逐执行单元准入")
    return {"task_id": doc.get("run_id") or "", "eligible": False,
            "reason_code": blockers[0]["reason_code"], "blockers": blockers,
            "remaining_budget_seconds": remaining, "execution_units": units,
            "business_result_unchanged": True}


def eligibility(task_id: str) -> dict:
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.execution_resource import ExecutionResource
    from mino_nexus.services import run_store

    actor = require_user_id()
    doc = run_store.get(task_id)
    if not doc or doc.get("owner_user_id") != actor:
        raise KeyError("task_not_found")
    with session_scope() as db:
        resources = [{"state": row.state} for row in db.query(ExecutionResource)
                     .filter_by(run_id=task_id, owner_user_id=actor).all()]
    return assess(doc, resources=resources)
