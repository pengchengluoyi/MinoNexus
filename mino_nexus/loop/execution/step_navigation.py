"""进入新一步时重置上一轮留下的导航/登录宏状态，并算这一屏的导航计划提示。

不重置会出现"上一屏的登录宏还开着，这一屏直接被当成登录流"——
状态是跨步骤的，所以必须在换步那一刻统一清掉。
"""
from __future__ import annotations

from typing import Any

from mino_nexus.loop.nav_session_fork import (
    detect_guest_tab_login_fork,
    format_guest_tab_fork_hint,
    required_session_from_scene,
)
from mino_nexus.loop.step_nav_plan import build_step_nav_plan_hint, step_needs_nav_plan
from mino_nexus.loop.step_pointer import StepCursor
from mino_nexus.navigation.login.transition import update_auth_state


def refresh_step_navigation(
    *,
    is_explore: bool,
    cursor: Any,
    cur: Any,
    ctx: Any,
    case: dict[str, Any],
) -> None:
    if (
        not is_explore
        and isinstance(cursor, StepCursor)
        and cursor.phase == "do"
        and cur
    ):
        from mino_nexus.loop.step_nav_plan import build_step_nav_plan_hint, step_needs_nav_plan

        if int(cur.n) != int(cursor.step_nav_plan_step_n or 0):
            cursor.step_nav_plan_step_n = int(cur.n)
            update_auth_state(ctx, login_flow_macro_done=set())
            ctx.login_macro_yielded = None
            ctx.login_flow_interrupt = False
            update_auth_state(ctx, login_flow_macro_active=False)
            ctx.system_dialog_macro_done = False
            ctx.recovery_allow_back = False
            instr_nav = str(cur.instruction or "")
            from mino_nexus.loop.nav_session_fork import (
                detect_guest_tab_login_fork,
                format_guest_tab_fork_hint,
                required_session_from_scene,
            )

            req_sess = required_session_from_scene(
                dict(getattr(ctx, "case_scene", None) or {})
            )
            exp_nav = str(cur.expected or "") if cur else ""
            fork = detect_guest_tab_login_fork(
                instruction=instr_nav,
                expected=exp_nav,
                required_session=req_sess,
            )
            ctx.nav_guest_tab_fork = fork
            if step_needs_nav_plan(instr_nav):
                cursor.step_nav_plan_hint = build_step_nav_plan_hint(
                    instruction=instr_nav,
                    app_id=str(getattr(ctx, "app_id", "") or ""),
                    project_id=str(getattr(ctx, "nav_project_id", "") or ""),
                    localized=dict(getattr(ctx, "nav_localized", None) or {}),
                    app_version=str(getattr(ctx, "app_version", "") or ""),
                    expected=exp_nav,
                    required_session=req_sess,
                    precondition=str(
                        getattr(cursor, "precondition", "") or case.get("precondition") or ""
                    ),
                )
            else:
                cursor.step_nav_plan_hint = (
                    format_guest_tab_fork_hint(fork) if fork else ""
                )
