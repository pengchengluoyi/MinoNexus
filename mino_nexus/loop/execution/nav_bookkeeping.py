"""FSM 导航记账：衰退计数、到达确认、无底栏页签的延迟标记。

这些判断原来夹在循环里，和"这一轮到底跑了什么"混在一起；
单独成函数后，`fsm_navigate` 的状态只在这里改。
"""
from __future__ import annotations

from typing import Any

from mino_nexus.loop.execution.facts import _effect_nodes
from mino_nexus.loop.step_pointer import StepCursor


def note_fsm_navigation(
    *,
    ctx: Any,
    cursor: Any,
    cur: Any,
    cap_id: str,
    params: Any,
    status_val: str,
    summary: Any,
    screen_fp_turn: Any,
    decision: Any,
    nav: Any,
    thought: str,
    raw_resp: Any,
) -> None:
    if cap_id in ("fsm_navigate", "recover_fsm_navigate"):
        nav_key = str(
            (params or {}).get("to_state")
            or (params or {}).get("to")
            or (params or {}).get("selector_text")
            or ""
        )
        if status_val == "declined":
            prev_key = str(getattr(ctx, "fsm_last_decline_key", "") or "")
            prev_fp = str(getattr(ctx, "fsm_last_decline_fp", "") or "")
            if nav_key and nav_key == prev_key and screen_fp_turn == prev_fp:
                ctx.fsm_decline_repeat_streak = int(getattr(ctx, "fsm_decline_repeat_streak", 0) or 0) + 1
            else:
                ctx.fsm_last_decline_key = nav_key
                ctx.fsm_last_decline_fp = screen_fp_turn
                ctx.fsm_decline_repeat_streak = 0
            if isinstance(cursor, StepCursor) and cur and (
                "tab_not_visible" in str(summary or "")
                or "无底栏" in str(summary or "")
            ):
                from mino_nexus.loop import step_effect as step_effect_mod

                _vlm_decl = (
                    decision.vlm_hierarchy
                    if isinstance(decision.vlm_hierarchy, dict)
                    else None
                )
                step_effect_mod.maybe_mark_deferred_nav_tab(
                    cursor,
                    instruction=str(cur.instruction or ""),
                    expected=str(cur.expected or ""),
                    localized=dict(getattr(ctx, "nav_localized", None) or {}),
                    nodes=_effect_nodes(nav, ctx, extra_vlm=_vlm_decl),
                    thought=thought,
                    coerce_done=False,
                )
        elif status_val == "pass" and isinstance(raw_resp, dict):
            nav_attempt = raw_resp.get("nav_attempt") or {}
            if isinstance(nav_attempt, dict) and nav_attempt.get("arrived_at_target"):
                ctx.fsm_decline_repeat_streak = 0
                ctx.fsm_last_decline_key = ""
            corr = str(raw_resp.get("correction_hint") or "")
            if "已在目标屏" in corr or "signal_done" in corr:
                if isinstance(cursor, StepCursor):
                    cursor.step_intents_done.add("nav_tab")
