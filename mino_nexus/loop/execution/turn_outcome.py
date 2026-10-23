"""把一次派发结果归一成"这一轮发生了什么"：状态、摘要、耗时、错误、执行器。

`recovered` 分支和普通事件分支的输出字段是同一组，所以必须一起收敛，
否则回收路径会漏掉 `executor_used` 之类的字段，日志里就看不出是谁做的。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mino_nexus.loop.step_pointer import StepCursor


@dataclass
class TurnOutcome:
    status_val: str = ""
    summary: Any = ""
    elapsed_ms: Any = 0
    error: str = ""
    executor_used: str = ""


def resolve_turn_outcome(
    *,
    result: Any,
    cap_id: str,
    params: Any,
    cursor: Any,
    recovery_extra: dict[str, Any],
) -> TurnOutcome:
    if hasattr(result, "recovered"):
        mode = str(getattr(result, "mode", "") or "")
        rid = str(getattr(result, "rule_id", "") or "").strip()
        advice = str(getattr(result, "advice", "") or "").strip()
        if mode == "advise":
            status_val = "declined" if result.error else "pass"
            summary = advice or (result.summary() if callable(getattr(result, "summary", None)) else str(getattr(result, "error", "") or ""))
            elapsed_ms = 0
            error = result.error or ""
            executor_used = "recovery"
            if summary and isinstance(cursor, StepCursor):
                cursor.correction_hint = summary[:500]
            if rid:
                cursor.bump_advise_recovery(rid)
        else:
            status_val = "pass" if result.recovered else ("fail" if result.applied else "skipped")
            summary = result.summary() if callable(getattr(result, "summary", None)) else str(getattr(result, "error", "") or "")
            elapsed_ms = 0
            error = result.error or ""
            executor_used = "recovery"
            if rid:
                if result.recovered:
                    pass
                else:
                    cursor.bump_recovery_fail(rid)
        if result.stop_run:
            status_val = "blocked"
        actions = getattr(result, "actions", None)
        if isinstance(actions, list) and actions:
            recovery_extra["recovery_actions"] = actions
        ev_brief = str(getattr(result, "evidence", "") or "").strip()
        if ev_brief and not result.recovered:
            error = f"{error}; {ev_brief}".strip("; ")
    else:
        status_val = result.status.value if hasattr(result.status, "value") else str(result.status)
        summary = result.summary or ""
        elapsed_ms = result.elapsed_ms or 0
        error = result.error or ""
        executor_used = result.executor_used or ""
        if cap_id == "input_text":
            from mino_nexus.loop.flow_block_exit import coalesce_input_text_device_status

            coerced = coalesce_input_text_device_status(
                status_val,
                summary=str(summary or error or ""),
                field=str((params or {}).get("field") or ""),
                params=params if isinstance(params, dict) else None,
            )
            if coerced != status_val:
                status_val = coerced
                if coerced == "fail" and not error:
                    error = summary or "input_text_exit_not_pass"

    return TurnOutcome(
        status_val=str(status_val),
        summary=summary,
        elapsed_ms=elapsed_ms,
        error=str(error or ""),
        executor_used=str(executor_used or ""),
    )
