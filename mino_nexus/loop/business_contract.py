"""已审核 AppIntel 规则的运行时前置约束；自由文本不进入状态机。"""
from __future__ import annotations

from typing import Any


def required_business_milestones(
    *, app_id: str, project_id: str, case_step: int, capability_id: str,
) -> list[dict[str, Any]]:
    """只取显式绑定的结构化规则；规则不存在时不猜测业务顺序。"""
    if not app_id and not project_id:
        return []
    from mino_nexus.services.knowledge_match import _eligible

    result: list[dict[str, Any]] = []
    for item in _eligible(app_id, project_id):
        rule = item.get("execution_contract")
        if not isinstance(rule, dict):
            continue
        try:
            matching_step = int(rule.get("case_step")) == int(case_step)
        except (TypeError, ValueError):
            continue
        if not matching_step or str(rule.get("capability_id") or "") != capability_id:
            continue
        required = rule.get("requires_milestones")
        if not isinstance(required, list) or not required or any(
            not isinstance(mid, str) or not mid.strip() for mid in required
        ):
            continue
        result.append({"source_id": str(item.get("id") or ""), "required": required})
    return result


def business_contract_block(
    cursor: Any, *, app_id: str, project_id: str, case_step: int, capability_id: str,
) -> dict[str, Any] | None:
    from mino_nexus.loop.milestones import read_state

    rules = required_business_milestones(
        app_id=app_id, project_id=project_id, case_step=case_step, capability_id=capability_id,
    )
    if not rules:
        return None
    state = read_state(cursor)
    statuses = {
        str(row.get("id") or ""): str(row.get("status") or "").lower()
        for row in state.get("milestones") or [] if isinstance(row, dict)
    }
    for rule in rules:
        missing = [mid for mid in rule["required"] if statuses.get(mid) != "pass"]
        if missing:
            return {
                "reason_code": "business_precondition_unsatisfied",
                "source_id": rule["source_id"],
                "missing_milestones": missing,
                "capability_id": capability_id,
                "case_step": case_step,
            }
    return None
