"""一次工具调用成功后要做的六件事：延迟视觉通过、记绑定、记里程碑、语义止损、同步前置、阶段转换。

顺序不能变：语义止损必须排在阶段转换之前，否则"该停"的回合会先被推进到下一阶段。
返回 `ToolPassFlow` 把三种结束方式交回主循环决定。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

from mino_nexus.action_space.scheme import execution_scheme as _scheme_now
from mino_nexus.loop.binding_cache import remember_success
from mino_nexus.loop.channel_observation import bind_fresh_observation
from mino_nexus.loop.execution.turn_log import _log_turn_end
from mino_nexus.loop.milestone_orchestrator import in_progress_milestone_id
from mino_nexus.loop.milestones import note_tool_pass_milestone
from mino_nexus.loop.prep_program_plan import sync_prep_internal_milestones
from mino_nexus.loop.step_phase_fsm import try_transition_after_tool_pass
from mino_nexus.loop.step_pointer import StepCursor
from mino_nexus.loop.visual_review import defer_visual_pass, remember_visual_prior


@dataclass
class ToolPassFlow:
    cont: bool = False
    leave: dict[str, Any] | None = None
    leave_case: bool = False


def note_tool_pass(
    *,
    ctx: Any,
    cursor: Any,
    proxy: Any,
    cap_id: str,
    params: Any,
    summary: Any,
    status_val: str,
    writer: Any,
    redo_turn: bool,
    in_prep: bool,
    in_check: bool,
    leave: Callable[..., dict[str, Any]],
) -> ToolPassFlow:
    if status_val == 'pass' and isinstance(cursor, StepCursor):
        from mino_nexus.action_space.scheme import execution_scheme as _scheme_now
        from mino_nexus.loop.channel_observation import bind_fresh_observation
        from mino_nexus.loop.milestone_orchestrator import in_progress_milestone_id
        from mino_nexus.loop.milestones import note_tool_pass_milestone
        from mino_nexus.loop.visual_review import defer_visual_pass, remember_visual_prior

        visual_now = _scheme_now(ctx) == "visual"
        if redo_turn:
            defer_visual_pass(
                cursor,
                capability_id=cap_id,
                params=params if isinstance(params, dict) else None,
                summary=summary,
                writer=writer,
                ctx=ctx,
            )
            setattr(cursor, "visual_turn_pending", False)
        else:
            focus_before = in_progress_milestone_id(cursor)
            deferred = defer_visual_pass(
                cursor,
                capability_id=cap_id,
                params=params if isinstance(params, dict) else None,
                summary=summary,
                writer=writer,
                ctx=ctx,
            )
            if not deferred:
                try:
                    from mino_nexus.loop.binding_cache import remember_success

                    remember_success(
                        cursor,
                        ctx,
                        params=params if isinstance(params, dict) else None,
                        capability_id=cap_id,
                        writer=writer,
                    )
                except Exception:  # noqa: BLE001
                    pass
                if cap_id == "input_text":
                    bind_fresh_observation(ctx, proxy)
                noted = note_tool_pass_milestone(
                    cursor,
                    capability_id=cap_id,
                    writer=writer,
                    params=params if isinstance(params, dict) else None,
                    tool_summary=summary,
                    ctx=ctx,
                )
                if noted and visual_now and cap_id == "tap_element" and focus_before:
                    remember_visual_prior(
                        cursor,
                        milestone_id=focus_before,
                        capability_id=cap_id,
                        params=params if isinstance(params, dict) else None,
                        summary=summary,
                    )
                stop = str(getattr(cursor, "semantic_stop", "") or "").strip()
                if stop:
                    return ToolPassFlow(leave=leave(status="fail", summary=stop[:500], reason_code=str(getattr(cursor, "review_contract_reason", "") or "action_result_unverified")))
                if in_prep:
                    from mino_nexus.loop.prep_program_plan import sync_prep_internal_milestones

                    sync_prep_internal_milestones(cursor, ctx)
                from mino_nexus.loop.step_phase_fsm import try_transition_after_tool_pass

                fsm_tr = try_transition_after_tool_pass(
                    cursor,
                    ctx,
                    writer=writer,
                    in_prep=in_prep,
                    in_check=in_check,
                )
                if fsm_tr == "check_to_case_done":
                    return ToolPassFlow(leave_case=True)
                if fsm_tr:
                    _log_turn_end(writer, cap=cap_id, status=status_val)
                    return ToolPassFlow(cont=True)
    return ToolPassFlow()
