"""执行游标上的纯状态读写：步骤作用域、成功判据、里程碑进度。

只读 `cursor` 的属性，不触网、不读库——`action_space` 归一化方案时需要这些判断，
所以放在 `runtime`（比 `loop` 低），而不是让它反向 import 循环。
"""
from __future__ import annotations

from typing import Any, Optional

SUCCESS_CRITERIA_SCHEMA = "mino.success_criteria.v1"


def step_scope_key(cursor: Any) -> tuple[int, str]:
    phase = str(getattr(cursor, "phase", "") or "do").strip().lower()
    if phase == "prep":
        return 0, "prep"
    node = cursor.current() if cursor is not None else None
    n = int(getattr(node, "n", 0) or 0) if node is not None else 0
    return n, phase


def empty_success_criteria(cursor: Any) -> dict[str, Any]:
    case_step, phase = step_scope_key(cursor)
    return {
        "schema": SUCCESS_CRITERIA_SCHEMA,
        "phase": phase,
        "case_step": case_step,
        "status": "pending",
        "exit_allowed": False,
        "milestones": [],
    }


def read_state(cursor: Any) -> dict[str, Any]:
    raw = getattr(cursor, "success_criteria_state", None)
    if isinstance(raw, dict) and raw.get("schema"):
        return dict(raw)
    return empty_success_criteria(cursor)


def write_state(cursor: Any, state: dict[str, Any]) -> None:
    cursor.success_criteria_state = dict(state)


def _milestones_list(state: dict[str, Any]) -> list[dict[str, Any]]:
    return [row for row in (state.get("milestones") or []) if isinstance(row, dict)]


def in_progress_milestone(state: dict[str, Any]) -> Optional[dict[str, Any]]:
    for row in _milestones_list(state):
        if str(row.get("status") or "").strip().lower() == "in_progress":
            return row
    return None


def active_reauthentication_navigation(cursor: Any) -> bool:
    if cursor is None or step_scope_key(cursor)[1] != "prep":
        return False
    state = read_state(cursor)
    marker = state.get("reauthentication") or {}
    return marker.get("mode") == "logout_then_login" and any(
        isinstance(row, dict) and row.get("status") == "in_progress" and row.get("source") == "prep_program"
        and row.get("prep_flow_id") == "reauthentication" and row.get("id") == marker.get("milestone_id")
        for row in state.get("milestones") or [])


def active_evidence_navigation(cursor: Any) -> bool:
    """Read-only login observation needs the current frame as well as structure."""
    if cursor is None:
        return False
    row = in_progress_milestone(read_state(cursor)) or {}
    return (row.get("source") == "login_evidence_observation"
            and str(row.get("id") or "").startswith("login_evidence_nav_"))
