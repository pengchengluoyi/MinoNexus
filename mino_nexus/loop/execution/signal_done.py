"""模型说"我做完了"时的处置：里程碑没齐就挡回去，齐了就进校验。

三种结局必须由调用方决定——所以这里返回 `Signal`，而不是自己 `continue` 或 `return`：
- `NA`：这条不适用（不是 do 阶段的 signal_done）
- `CONTINUE`：本轮到此结束
- `LEAVE_CASE`：整个用例收尾（校验转 case_done 的转换）
"""
from __future__ import annotations

from enum import Enum
from typing import Any

from mino_nexus.loop import step_effect as step_effect_mod
from mino_nexus.loop.execution.facts import _effect_nodes
from mino_nexus.loop.execution.turn_log import _log_turn_end
from mino_nexus.loop.step_phase_fsm import (
    handle_model_signal_done_blocked,
    milestone_phase_active,
)
from mino_nexus.loop.step_pointer import StepCursor


class Signal(Enum):
    NA = "na"
    CONTINUE = "continue"
    LEAVE_CASE = "leave_case"


def handle_signal_done(
    *,
    ctx: Any,
    cursor: Any,
    cur: Any,
    cap_id: str,
    status_val: str,
    decision: Any,
    nav: Any,
    writer: Any,
    thought: str,
) -> Signal:
    if (
        cap_id == "signal_done"
        and status_val == "pass"
        and isinstance(cursor, StepCursor)
        and cursor.phase == "do"
    ):
        from mino_nexus.loop.step_phase_fsm import (
            handle_model_signal_done_blocked,
            milestone_phase_active,
        )

        if milestone_phase_active():
            act, tr = handle_model_signal_done_blocked(
                cursor,
                ctx,
                writer=writer,
                in_prep=False,
                in_check=False,
            )
            if act == "transition" and tr == "check_to_case_done":
                return Signal.LEAVE_CASE
            _log_turn_end(writer, cap="signal_done", status="skipped")
            return Signal.CONTINUE
        if cur:
            from mino_nexus.loop import step_effect as step_effect_mod

            _vlm_sd = (
                decision.vlm_hierarchy if isinstance(decision.vlm_hierarchy, dict) else None
            )
            step_effect_mod.maybe_mark_deferred_nav_tab(
                cursor,
                instruction=str(cur.instruction or ""),
                expected=str(cur.expected or ""),
                localized=dict(getattr(ctx, "nav_localized", None) or {}),
                nodes=_effect_nodes(nav, ctx, extra_vlm=_vlm_sd),
                thought=thought,
                coerce_done=True,
            )
        cursor.enter_check()
        _log_turn_end(writer, cap="signal_done", status="pass")
        return Signal.CONTINUE
    return Signal.NA
