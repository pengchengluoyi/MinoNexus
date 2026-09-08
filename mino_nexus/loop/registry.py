"""闭集谓词 / 守卫 / 推进器 / 槽取值注册表。"""
from __future__ import annotations

from typing import Any, Callable, Optional

from mino_nexus.catalog.exec_classes import MUTATE_CAPS

PREDICATES: dict[str, Callable[[dict[str, bool]], bool]] = {
    "is_web_channel": lambda flags: bool(flags.get("is_web_channel")),
    "explain_mode": lambda flags: bool(flags.get("explain_mode")),
}

INSPECTION_ATS = frozenset({
    "case_start",
    "case_end",
    "phase_enter:prep",
    "phase_enter:do",
    "phase_enter:check",
    "phase_exit:prep",
    "phase_exit:do",
    "phase_exit:check",
})

GuardFn = Callable[[dict[str, Any]], Optional[str]]


def _guard_deny_mutate(ctx: dict[str, Any]) -> Optional[str]:
    if str(ctx.get("phase") or "") == "check" and str(ctx.get("cap_id") or "") in MUTATE_CAPS:
        return "校验阶段不能改界面，已拒绝这次操作"
    return None


def _guard_skip_repeat_tap(ctx: dict[str, Any]) -> Optional[str]:
    from mino_nexus.loop.step_pointer import repeats_last_tap

    phase = str(ctx.get("phase") or "")
    cap_id = str(ctx.get("cap_id") or "")
    if phase in ("prep", "check") or cap_id != "tap_element":
        return None
    if repeats_last_tap(ctx.get("last_tap"), ctx.get("params")):
        return "入口已点过，跳过这次点击，进入本步校验"
    return None


def apply_force_case_expectation(params: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """校验阶段把用例同号预期塞给 VLM。"""
    out = dict(params or {})
    if "force_case_expectation" not in (ctx.get("guards") or []):
        return out
    if str(ctx.get("cap_id") or "") != "assert_visual":
        return out
    cur = ctx.get("cursor")
    in_check = str(ctx.get("phase") or "") == "check"
    case_expected = str(getattr(cur, "expected", "") or "").strip() if cur else ""
    model_expected = str(out.get("expectation") or out.get("expected") or "").strip()
    if in_check and case_expected:
        out["expectation"] = case_expected
    elif not model_expected and case_expected:
        out["expectation"] = case_expected
    return out


GUARDS: dict[str, GuardFn] = {
    "deny_mutate": _guard_deny_mutate,
    "skip_repeat_tap": _guard_skip_repeat_tap,
    "force_case_expectation": lambda _ctx: None,
}

AdvancerFn = Callable[[dict[str, Any]], bool]


def _advancer_signal_done(_ctx: dict[str, Any]) -> bool:
    return True


def _advancer_assert_pass(ctx: dict[str, Any]) -> bool:
    cursor = ctx.get("cursor")
    return bool(getattr(cursor, "step_checked", False))


ADVANCERS: dict[str, AdvancerFn] = {
    "signal_done": _advancer_signal_done,
    "assert_pass": _advancer_assert_pass,
}

PROVIDERS: dict[str, str] = {
    "goal": "planner.decide_next_action",
    "checkpoints_block": "step_pointer.StepCursor.prompt_block",
    "device_brief_json": "run_context.RunContext.to_prompt_brief",
    "menu_json": "runtime.menu.available_menu_brief",
    "history_block": "agent_loop._history",
    "hierarchy_text": "loop.inspections",
    "session_block": "loop.inspections",
    "knowledge_hint": "loop.inspections",
    "user_payload": "roles_catalog.chat_with_role",
}


def run_guards(names: list[str], ctx: dict[str, Any]) -> Optional[str]:
    for name in names or []:
        fn = GUARDS.get(str(name or "").strip())
        if fn is None:
            continue
        reason = fn(ctx)
        if reason:
            return reason
    return None


__all__ = [
    "PREDICATES",
    "GUARDS",
    "ADVANCERS",
    "PROVIDERS",
    "INSPECTION_ATS",
    "run_guards",
    "apply_force_case_expectation",
]
