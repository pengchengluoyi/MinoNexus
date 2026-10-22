"""推进型能力成功之后：记一步、绑校验上下文、必要时自动收尾进入校验。

返回 True 表示"这一轮到此结束"——调用方据此 `continue`。
循环控制在原处是 `continue`，这里换成布尔返回，是为了不让这个函数自己决定主循环怎么走。
"""
from __future__ import annotations

from typing import Any, Callable

from mino_nexus.catalog.exec_classes import PROGRESS_CAPS
from mino_nexus.loop.execution.actions import RECOVER_PREFIX
from mino_nexus.loop.execution.turn_log import _log_turn_end
from mino_nexus.loop.step_pointer import StepCursor


def note_progress_and_auto_finish(
    *,
    ctx: Any,
    cursor: Any,
    cap_id: str,
    params: Any,
    status_val: str,
    summary: Any,
    result: Any,
    recovery_action: Any,
    history: list[str],
    seq: int,
    writer: Any,
    thumb: Any,
    emit: Callable[..., None],
    rec: Callable[..., Any],
) -> bool:
    if status_val == "pass" and cap_id in PROGRESS_CAPS and not recovery_action and not str(cap_id).startswith(RECOVER_PREFIX):
        count_nav = True
        if cap_id in ("fsm_navigate", "recover_fsm_navigate"):
            raw_nav = getattr(result, "raw_response", None) or {}
            if isinstance(raw_nav, dict):
                nav_attempt = raw_nav.get("nav_attempt") or {}
                count_nav = bool(nav_attempt.get("arrived_at_target"))
        cursor.record_step_op(
            cap_id,
            params=params if isinstance(params, dict) else None,
            count_nav_intent=count_nav,
        )
        if (
            isinstance(cursor, StepCursor)
            and "logout" in set(getattr(cursor, "step_intents_done", ()) or ())
            and not getattr(cursor, "ui_logout_committed", False)
        ):
            from mino_nexus.casework.resource_transition import emit_device_logout

            emit_device_logout(ctx, source="ui_logout")
            cursor.ui_logout_committed = True
        if isinstance(cursor, StepCursor) and cursor.phase == "do":
            _finish_hist = list(history[-48:])
            if status_val == "pass" and cap_id:
                _finish_hist.append(
                    f"{seq}. {cap_id} → pass: {str(summary or '')[:160]}"
                )
            cursor.bind_finish_context(ctx, _finish_hist)
        if isinstance(cursor, StepCursor) and cursor.phase == "do":
            from mino_nexus.loop.vision_plan import hold_visual_do_exit_until_effect

            hold_visual_do_exit_until_effect(cursor, ctx)
        if (
            isinstance(cursor, StepCursor)
            and cursor.try_auto_finish_do_when_milestones_met(ctx, writer=writer)
        ):
            rec(
                seq,
                capability_id="signal_done",
                status="skipped",
                summary="子里程碑已齐，自动进入校验",
                thought="",
                thumb=thumb,
            )
            emit(
                "result",
                thought="",
                step=seq,
                capability_id="signal_done",
                status="skipped",
                summary="子里程碑已齐，自动进入校验",
                thumb=thumb,
            )
            _log_turn_end(writer, cap="signal_done", status="skipped")
            return True
    return False
