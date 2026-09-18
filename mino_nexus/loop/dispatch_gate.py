"""派发门闩：guard 链统一求值，返回结构化 code（供 session_log / Studio）。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

GuardFn = Callable[[dict[str, Any]], Optional[str]]

# guard_id → 稳定拒收 code（与 agent_loop 历史 skip_cap 对齐）
GUARD_CODES: dict[str, str] = {
    "deny_mutate": "deny_mutate",
    "skip_repeat_read_device": "skip_repeat_read_device",
    "skip_repeat_check_run_env": "skip_repeat_check_run_env",
    "skip_repeat_get_app_version": "skip_repeat_get_app_version",
    "skip_repeat_launch_app": "skip_repeat_launch_app",
    "skip_repeat_clear_app_cache": "skip_repeat_clear_app_cache",
    "skip_repeat_satisfied_step_action": "skip_repeat_satisfied_step_action",
    "block_idle_wait_in_do": "block_idle_wait_in_do",
    "swipe_direction_vs_instruction": "swipe_direction_vs_instruction",
    "skip_repeat_swipe_stuck": "skip_repeat_swipe_stuck",
    "skip_repeat_structural_cap": "skip_repeat_structural_cap",
    "prep_clear_before_launch": "prep_clear_before_launch",
    "action_fuse": "action_fuse",
    "skip_repeat_tap": "skip_repeat_tap",
    "limit_advise_recovery": "limit_recovery_retry",
    "limit_recovery_retry": "limit_recovery_retry",
    "stuck_alternation": "stuck_alternation",
    "block_login_after_guest": "block_login_after_guest",
    "block_login_flow_unless_step_scope": "block_login_flow_unless_step_scope",
    "block_mutate_when_thought_done": "block_mutate_when_thought_done",
    "block_do_after_step_goal": "block_do_after_step_goal",
    "require_sms_send_before_otp": "require_sms_send_before_otp",
    "exec_script_params": "exec_script_params",
    "require_do_work": "require_do_work",
    "block_assert_in_do": "block_assert_in_do",
    "require_session": "require_session",
    "force_case_expectation": "force_case_expectation",
    "block_back_without_nav_back_semantics": "block_back_without_nav_back_semantics",
}


@dataclass(frozen=True)
class GuardVerdict:
    allowed: bool
    guard_id: str = ""
    code: str = ""
    reason: str = ""


def evaluate_guards(
    names: list[str],
    ctx: dict[str, Any],
    *,
    guards: dict[str, GuardFn],
) -> GuardVerdict:
    """按 SOP 顺序执行 guard，首个拒收即返回。"""
    for name in names or []:
        gid = str(name or "").strip()
        fn = guards.get(gid)
        if fn is None:
            continue
        reason = fn(ctx)
        if reason:
            code = GUARD_CODES.get(gid, gid)
            return GuardVerdict(False, gid, code, str(reason))
    return GuardVerdict(True)


def first_block_reason(
    names: list[str],
    ctx: dict[str, Any],
    *,
    guards: dict[str, GuardFn],
) -> Optional[str]:
    """兼容 registry.run_guards 返回值。"""
    v = evaluate_guards(names, ctx, guards=guards)
    return v.reason if not v.allowed else None
