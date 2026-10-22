"""守卫上下文：一次判定能看到的全部事实，集中在一处构造。

为什么单独成函数：这几十个字段就是"守卫到底看见了什么"的完整清单。
散在 4000 行循环里时，排查一个 guard 误判要往上翻 200 行才知道某个字段有没有被塞进去。
"""
from __future__ import annotations

from typing import Any

from mino_nexus.loop.step_pointer import tap_params_guard_summary


def build_guard_ctx(
    *,
    ctx: Any,
    cursor: Any,
    cap_id: str,
    params: Any,
    cur: Any,
    screen_fp: Any,
    phase_cfg: dict[str, Any],
    history: list[str],
    menu: Any,
    task_env_brief: str,
    fg_ctx: dict[str, Any],
    fg_menu: dict[str, Any],
    inspect_slots: dict[str, str],
    case: dict[str, Any],
    thought: str,
    login_module_case: bool,
    target_pkg: str,
    llm_session_block,
) -> dict[str, Any]:
    return {
        "phase": cursor.phase,
        "cap_id": cap_id,
        "params": params,
        "cursor": cur,
        "step_cursor": cursor,
        "last_tap": cursor.last_tap,
        "screen_fp": screen_fp,
        "guards": list(phase_cfg.get("guards") or []),
        "history_lines": list(history[-12:]),
        "tap_summary": tap_params_guard_summary(params),
        "menu": menu,
        "accounts_brief": str(getattr(ctx, "accounts_brief", "") or ""),
        "run_env_brief": task_env_brief,
        "run_ctx": ctx,
        "ctx": ctx,
        "app_foreground": fg_ctx.get("app_foreground") or fg_menu.get("app_foreground") or "",
        "system_overlay": fg_ctx.get("system_overlay") or fg_menu.get("system_overlay") or "",
        "nav_localized": dict(getattr(ctx, "nav_localized", None) or {}),
        "app_version": str(getattr(ctx, "app_version", "") or ""),
        "case_scene": dict(getattr(ctx, "case_scene", None) or {}),
        "session_block": llm_session_block(ctx, str(inspect_slots.get("session_block") or "")),
        "prep_clear_done": bool(getattr(ctx, "prep_clear_done", False)),
        "session_fact_session": str(
            (getattr(ctx, "session_fact", None) or {}).get("session") or ""
        ).strip().lower(),
        "app_launch_confirmed": bool(getattr(ctx, "app_launch_confirmed", False)),
        "precondition": str(getattr(cursor, "precondition", "") or case.get("precondition") or ""),
        "decision_thought": thought,
        "login_module_case": bool(login_module_case),
        "login_flow_interrupt": bool(getattr(ctx, "login_flow_interrupt", False)),
        "login_flow_macro_active": bool(getattr(ctx, "login_flow_macro_active", False)),
        "recovery_allow_back": bool(getattr(ctx, "recovery_allow_back", False)),
        "nav_open_loop_active": bool(getattr(ctx, "nav_open_loop_active", False)),
        "nav_open_loop_blocked_target": str(
            getattr(ctx, "nav_open_loop_blocked_target", "") or ""
        ),
        "fsm_last_decline_key": str(getattr(ctx, "fsm_last_decline_key", "") or ""),
        "fsm_decline_repeat_streak": int(getattr(ctx, "fsm_decline_repeat_streak", 0) or 0),
        "display_guard_active": bool(getattr(ctx, "display_guard_active", False)),
        "sn": str(getattr(ctx, "sn", "") or ""),
        "target_package": str(target_pkg or ""),
        "run_ctx": ctx,
    }
