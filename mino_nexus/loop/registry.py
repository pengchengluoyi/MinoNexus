"""闭集谓词 / 守卫 / 推进器 / 槽取值注册表。"""
from __future__ import annotations

from typing import Any, Callable, Optional

from mino_nexus.catalog.exec_classes import MUTATE_CAPS
from mino_nexus.loop.step_pointer import (
    format_skip_repeat_tap_message,
    is_guest_entry_step,
    is_observe_only_step,
    repeats_last_tap,
    tap_summary_is_guest_entry,
    tap_summary_is_login_entry,
)

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


RECOVER_PREFIX = "recover_"
ADVISE_RECOVERY_MAX = 2


def _recovery_rule_id(cap_id: str) -> str:
    cid = str(cap_id or "").strip()
    if cid.startswith(RECOVER_PREFIX):
        return cid[len(RECOVER_PREFIX):]
    return ""


def _guard_exec_script_params(ctx: dict[str, Any]) -> Optional[str]:
    cap_id = str(ctx.get("cap_id") or "")
    if cap_id != "exec_script":
        return None
    params = dict(ctx.get("params") or {})
    if str(params.get("script_id") or params.get("script") or "").strip():
        return None
    return (
        "exec_script 缺少 script_id 或 script 参数，已拒绝。"
        "检查类前置若已满足可直接 signal_done；读设备信息用 read_device_data。"
    )


def _guard_limit_advise_recovery(ctx: dict[str, Any]) -> Optional[str]:
    cap_id = str(ctx.get("cap_id") or "")
    rule_id = _recovery_rule_id(cap_id)
    if not rule_id:
        return None
    try:
        from mino_nexus.catalog import registry as catalog

        rule = catalog.get_recovery_rule(rule_id)
    except Exception:
        rule = None
    if rule is None or str(getattr(rule, "mode", "") or "") != "advise":
        return None
    step_cursor = ctx.get("step_cursor")
    used = int(getattr(step_cursor, "advise_recovery_counts", {}).get(rule_id, 0) or 0)
    if used < ADVISE_RECOVERY_MAX:
        return None
    return (
        f"已拒绝重复 recover_{rule_id}：同类 advise 已提示 {used} 次。"
        f"请按 history 改用 hierarchy、input_text、signal_ask_human 或 signal_give_up，勿再调 wake/同 rule。"
    )


def _tap_summary_label(line: str) -> str:
    text = str(line or "")
    if "→" not in text:
        return ""
    tail = text.split("→", 1)[-1]
    if ":" in tail:
        tail = tail.split(":", 1)[-1]
    return tail.strip()[:80]


def _guard_stuck_alternation(ctx: dict[str, Any]) -> Optional[str]:
    if str(ctx.get("phase") or "") != "do":
        return None
    if str(ctx.get("cap_id") or "") != "tap_element":
        return None
    labels = [
        _tap_summary_label(line)
        for line in (ctx.get("history_lines") or [])
        if "tap_element" in str(line) and "→ pass" in str(line)
    ]
    labels = [x for x in labels if x]
    if len(labels) < 4:
        return None
    a, b, c, d = labels[-4], labels[-3], labels[-2], labels[-1]
    if a == c and b == d and a != b:
        step_cursor = ctx.get("step_cursor")
        cur = ctx.get("cursor")
        instr = str(getattr(cur, "instruction", "") or "").strip() if cur else ""
        if (
            step_cursor is not None
            and is_guest_entry_step(instr)
            and bool(getattr(step_cursor, "guest_entry_tapped", False))
        ):
            return (
                f"已拒绝重复切换 {a} ⇄ {b}：访客入口已点过。"
                f"若屏上已是首页内容请 signal_done 结束本步，再执行下一步切换 Tab。"
            )
        return (
            f"已拒绝重复切换 {a} ⇄ {b}：同一对入口已交替点击多轮仍无进展。"
            f"请换策略（找「游客模式」等正确入口、返回、logout、signal_give_up）。"
        )
    return None


def _guard_block_login_after_guest(ctx: dict[str, Any]) -> Optional[str]:
    if str(ctx.get("phase") or "") != "do":
        return None
    if str(ctx.get("cap_id") or "") != "tap_element":
        return None
    cur = ctx.get("cursor")
    step_cursor = ctx.get("step_cursor")
    instr = str(getattr(cur, "instruction", "") or "").strip() if cur else ""
    if not is_guest_entry_step(instr):
        return None
    if not bool(getattr(step_cursor, "guest_entry_tapped", False)):
        return None
    summary = str(ctx.get("tap_summary") or "")
    params = dict(ctx.get("params") or {})
    blob = f"{summary} {params.get('selector_text') or ''}"
    if not tap_summary_is_login_entry(blob):
        return None
    return (
        "已点击访客/游客入口，本步目标应已达成。"
        "请 signal_done 结束本步，勿再点登录方式或「我的」Tab。"
    )


def _guard_require_do_work(ctx: dict[str, Any]) -> Optional[str]:
    """供 agent_loop 在 signal_done 前调用。"""
    if str(ctx.get("phase") or "") != "do":
        return None
    if str(ctx.get("intent") or "") != "signal_done":
        return None
    cur = ctx.get("cursor")
    step_cursor = ctx.get("step_cursor")
    instr = str(getattr(cur, "instruction", "") or "").strip() if cur else ""
    if is_observe_only_step(instr):
        return None
    ops = int(getattr(step_cursor, "step_ops", 0) or 0)
    if ops > 0:
        return None
    return (
        "本步尚未执行任何设备操作（点击/输入/滑动等），不能 signal_done。"
        "请先完成步骤原文要求的具体动作。"
    )


def _guard_skip_repeat_read_device(ctx: dict[str, Any]) -> Optional[str]:
    from mino_nexus.loop.step_pointer import (
        format_skip_repeat_read_device_message,
        last_read_device_summary,
        read_device_prep_satisfied,
    )

    if str(ctx.get("phase") or "") != "prep":
        return None
    if str(ctx.get("cap_id") or "") != "read_device_data":
        return None
    history = list(ctx.get("history_lines") or [])
    prev = last_read_device_summary(history)
    if not prev or not read_device_prep_satisfied(prev):
        return None
    return format_skip_repeat_read_device_message(prev)


def _menu_has_cap(ctx: dict[str, Any], cap_id: str) -> bool:
    menu = ctx.get("menu") or []
    if isinstance(menu, list):
        return any(str((m or {}).get("id") or "") == cap_id for m in menu)
    return False


def _leased_account(ctx: dict[str, Any]) -> bool:
    brief = str(ctx.get("accounts_brief") or "").strip()
    if not brief or brief.startswith("（未租") or "未租" in brief[:12]:
        return False
    return True


def _guard_action_fuse(ctx: dict[str, Any]) -> Optional[str]:
    step_cursor = ctx.get("step_cursor")
    if step_cursor is None:
        return None
    gate = getattr(step_cursor, "progress_gate", None) or getattr(step_cursor, "action_fuse", None)
    if gate is None:
        return None
    cap_id = str(ctx.get("cap_id") or "")
    return gate.check(
        phase=str(ctx.get("phase") or ""),
        cap_id=cap_id,
        params=dict(ctx.get("params") or {}),
        screen_fp=str(ctx.get("screen_fp") or ""),
        has_get_otp=_menu_has_cap(ctx, "get_otp"),
        leased=_leased_account(ctx),
    )


def _guard_skip_repeat_check_run_env(ctx: dict[str, Any]) -> Optional[str]:
    if str(ctx.get("phase") or "") != "prep":
        return None
    if str(ctx.get("cap_id") or "") != "check_run_env":
        return None
    brief = str(ctx.get("run_env_brief") or "").strip()
    if not brief:
        return None
    return (
        f"已拒绝重复 check_run_env（本任务已确认：{brief[:80]}）。"
        f"请直接 signal_done 结束前置；后续用例执行会沿用该环境。"
    )


def _guard_skip_repeat_tap(ctx: dict[str, Any]) -> Optional[str]:
    phase = str(ctx.get("phase") or "")
    cap_id = str(ctx.get("cap_id") or "")
    if phase in ("prep", "check") or cap_id != "tap_element":
        return None
    last = ctx.get("last_tap")
    if not last:
        return None
    current_fp = str(ctx.get("screen_fp") or "").strip()
    last_fp = str((last or {}).get("screen_fp") or "").strip()
    # 页面已切换（含误点进子页后返回）：同坐标可能是不同语义，不拦。
    if last_fp and current_fp and last_fp != current_fp:
        return None
    step_cursor = ctx.get("step_cursor")
    tap_epoch = int(getattr(step_cursor, "tap_epoch", 0) or 0) if step_cursor is not None else 0
    params = ctx.get("params")
    if repeats_last_tap(last, params, tap_epoch=tap_epoch):
        return format_skip_repeat_tap_message(last, params)
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
        instr = str(getattr(cur, "instruction", "") or "").strip() if cur else ""
        from mino_nexus.loop.step_pointer import enrich_assert_expectation

        out["expectation"] = enrich_assert_expectation(instr, case_expected)
    elif not model_expected and case_expected:
        instr = str(getattr(cur, "instruction", "") or "").strip() if cur else ""
        from mino_nexus.loop.step_pointer import enrich_assert_expectation

        out["expectation"] = enrich_assert_expectation(instr, case_expected)
    return out


GUARDS: dict[str, GuardFn] = {
    "deny_mutate": _guard_deny_mutate,
    "skip_repeat_read_device": _guard_skip_repeat_read_device,
    "skip_repeat_check_run_env": _guard_skip_repeat_check_run_env,
    "action_fuse": _guard_action_fuse,
    "skip_repeat_tap": _guard_skip_repeat_tap,
    "limit_advise_recovery": _guard_limit_advise_recovery,
    "stuck_alternation": _guard_stuck_alternation,
    "block_login_after_guest": _guard_block_login_after_guest,
    "exec_script_params": _guard_exec_script_params,
    "require_do_work": _guard_require_do_work,
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
    "knowledge_hint": "loop.inspections.match_step_knowledge",
    "knowledge_body": "loop.inspections.expand_named_knowledge",
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
    "GUARDS",
    "ADVANCERS",
    "PROVIDERS",
    "INSPECTION_ATS",
    "run_guards",
    "apply_force_case_expectation",
]
