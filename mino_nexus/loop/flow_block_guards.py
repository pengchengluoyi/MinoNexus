"""逻辑块内 guards / fuse（P4 §2.6），在 milestones 登录流下按意图边求值。

守卫声明只在 ``knowledge.intent_graph`` 的意图边上；这里只负责把声明解析成
函数并顺序执行。新增/删除登录守卫不需要再改本模块的分支。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Optional

GuardFn = Callable[[dict[str, Any], Any], Optional["FlowBlockGuardVerdict"]]


@dataclass(frozen=True)
class FlowBlockGuardVerdict:
    allowed: bool
    guard_id: str = ""
    code: str = ""
    reason: str = ""
    dispatch_gate_code: str = ""
    remediate: str = ""


def evaluate_flow_block_guards(ctx: dict[str, Any], cursor: Any) -> Optional[FlowBlockGuardVerdict]:
    """登录块（含非里程碑登录用例）按意图边 guards + 块 fuse 求值。"""
    from mino_nexus.loop.milestones import (
        login_block_id_for_ctx,
        login_flow_under_milestones,
        read_state,
    )

    from mino_nexus.loop.login_evidence_navigation import rejected_navigation_action
    if rejected_navigation_action(cursor, ctx.get("run_ctx") or ctx.get("ctx"),
                                  str(ctx.get("cap_id") or ""), ctx.get("params")):
        return FlowBlockGuardVerdict(allowed=False, guard_id="navigation:no_change_repeat",
            code="navigation_no_change_repeat", dispatch_gate_code="navigation_no_change_repeat",
            reason="上一只读导航已独立核验为无可见变化，禁止重复；选择其他可见入口或相反滚动方向，不能重复无效返回。")

    milestone_login = login_flow_under_milestones(cursor)
    login_context = milestone_login or bool(ctx.get("login_module_case"))
    if not login_context:
        current = cursor.current() if hasattr(cursor, "current") else None
        instruction = str(getattr(current, "instruction", "") or "")
        from mino_nexus.loop.step_contract import instruction_allows_login_flow

        login_context = instruction_allows_login_flow(instruction, login_module_case=False)
    if not login_context:
        return None

    state = read_state(cursor)
    block_id = str((state.get("block_ref") or {}).get("block_id") or "")
    if not block_id:
        # 非里程碑登录用例没有 block_ref，但 guard_ctx 已明确标记 login_module_case。
        block_id = login_block_id_for_ctx(ctx.get("run_ctx") or ctx.get("ctx"))
    run_ctx = ctx.get("run_ctx") or ctx.get("ctx")
    app_id = str(getattr(run_ctx, "app_id", "") or ctx.get("app_id") or "")

    from mino_nexus.knowledge.intent_graph import guards_for_block

    for name in guards_for_block(block_id, app_id=app_id):
        impl = _LOGIN_GUARD_IMPL.get(name)
        if impl is None:
            continue
        verdict = impl(ctx, cursor)
        if verdict is not None and not verdict.allowed:
            return verdict

    if str(ctx.get("cap_id") or "") != "tap_element":
        return None
    from mino_nexus.navigation.atlas.nav_flow_block_catalog import resolve_effective_steps

    steps = resolve_effective_steps(app_id=app_id, block_id=block_id) if block_id else []
    return _evaluate_fuse_repeat_tap(ctx, cursor, steps=steps)


def _guard_block_repeat_email(
    ctx: dict[str, Any],
    cursor: Any,
) -> Optional[FlowBlockGuardVerdict]:
    """Web 邮箱登录：Email 标签已选过就不再点，由意图边声明触发。"""
    if str(ctx.get("cap_id") or "") != "tap_element":
        return None
    from mino_nexus.contract.execution.ui import UiChannel, ui_channel_from_ctx

    run_ctx = ctx.get("run_ctx") or ctx.get("ctx")
    if ui_channel_from_ctx(run_ctx) != UiChannel.WEB:
        return None

    params = dict(ctx.get("params") or {})
    sel = str(
        params.get("selector_text") or params.get("text") or params.get("content_desc") or ""
    )
    if sel.strip() == "email" or not re.search(r"\bEmail\b|邮箱", sel, re.I):
        return None

    from mino_nexus.loop.email_login_auto import _email_input_done, _email_tab_selected

    hist = list(ctx.get("history_lines") or [])
    done = set(getattr(cursor, "step_intents_done", None) or set())
    if _email_input_done(hist, done) or not _email_tab_selected(hist):
        return None

    reason = (
        "Email 标签已选过；请由程序链或 input_text(field=email) 填租号邮箱，"
        "再发码。禁止重复点 Email 标签。"
    )
    return FlowBlockGuardVerdict(
        allowed=False,
        guard_id="flow_block:block_repeat_email_tab",
        code="block_repeat_email_tab",
        reason=reason,
        dispatch_gate_code="flow_block:block_repeat_email_tab",
        remediate="hook_chain",
    )


def _guard_require_otp_before_login_tap(ctx: dict[str, Any], cursor: Any) -> Optional[FlowBlockGuardVerdict]:
    if str(ctx.get("cap_id") or "") != "tap_element":
        return None
    params = dict(ctx.get("params") or {})
    sel = str(params.get("selector_text") or params.get("text") or params.get("content_desc") or "")
    if not re.search(r"登录|立即登录|Log\s*in|Continue", sel, re.I):
        return None
    from mino_nexus.loop.milestones import read_state

    state = read_state(cursor)
    for row in state.get("milestones") or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("id") or "") != "otp_fill":
            continue
        st = str(row.get("status") or "pending").lower()
        if st in ("pass", "skipped"):
            return None
    from mino_nexus.loop.login_submit import _otp_code_already_entered, _sms_send_done

    hist = list(ctx.get("history_lines") or [])
    done = set(getattr(cursor, "step_intents_done", None) or set())
    if not _sms_send_done(hist, done):
        return None
    if _otp_code_already_entered(hist, done):
        return None
    reason = "已发码但验证码未填入；须先完成 otp_fill（get_otp + 填码）再点登录。"
    return FlowBlockGuardVerdict(
        allowed=False,
        guard_id="flow_block:require_otp_before_login_tap",
        code="require_otp_before_login_tap",
        reason=reason,
        dispatch_gate_code="flow_block:require_otp_before_login_tap",
    )


def _guard_otp_ready_no_repeat_get_otp(
    ctx: dict[str, Any], cursor: Any
) -> Optional[FlowBlockGuardVerdict]:
    if str(ctx.get("cap_id") or "") != "get_otp":
        return None
    from mino_nexus.loop.milestones import milestone_by_id, read_state

    row = milestone_by_id(read_state(cursor), "otp_fill")
    if not row or not row.get("otp_ready"):
        return None
    if str(row.get("status") or "") in ("pass", "skipped"):
        return None
    return FlowBlockGuardVerdict(
        allowed=False,
        guard_id="flow_block:otp_ready_need_input",
        code="otp_ready_need_input",
        reason="验证码已获取；请 input_text(field=sms_code) 填入验证码框，勿重复 get_otp。",
        dispatch_gate_code="flow_block:otp_ready_need_input",
    )


def _guard_require_sms_before_otp(ctx: dict[str, Any], cursor: Any) -> Optional[FlowBlockGuardVerdict]:
    cap = str(ctx.get("cap_id") or "")
    if cap not in ("get_otp", "input_text"):
        return None
    if cap == "input_text":
        field = str((ctx.get("params") or {}).get("field") or "").lower()
        if field not in ("sms_code", "验证码"):
            return None
    from mino_nexus.loop.login_submit import otp_fetch_allowed

    hist = list(ctx.get("history_lines") or [])
    done = set(getattr(cursor, "step_intents_done", None) or set())
    run_ctx = ctx.get("run_ctx") or ctx.get("ctx")
    if otp_fetch_allowed(hist, done, run_ctx, cursor=cursor):
        return None
    from mino_nexus.loop.login_verification import verification_send_required_message

    reason = verification_send_required_message(run_ctx)
    return FlowBlockGuardVerdict(
        allowed=False,
        guard_id="flow_block:require_sms_send_before_otp",
        code="require_sms_send_before_otp",
        reason=reason,
        dispatch_gate_code="flow_block:require_sms_send_before_otp",
    )


def _evaluate_fuse_repeat_tap(
    ctx: dict[str, Any],
    cursor: Any,
    *,
    steps: list[dict[str, Any]],
) -> Optional[FlowBlockGuardVerdict]:
    fuse_steps = [
        s for s in steps if isinstance(s, dict) and isinstance(s.get("fuse"), dict)
    ]
    if not fuse_steps:
        return None
    params = dict(ctx.get("params") or {})
    sel = str(params.get("selector_text") or params.get("text") or "")[:80]
    if not sel:
        return None
    from mino_nexus.loop.milestone_orchestrator import in_progress_milestone_id

    focus_mid = in_progress_milestone_id(cursor)
    from mino_nexus.loop.milestones import read_state, write_state
    from mino_nexus.loop.visual_review import input_focus_menu_ids

    run_ctx = ctx.get("run_ctx") or ctx.get("ctx")
    if str(ctx.get("cap_id") or "") == "tap_element" and input_focus_menu_ids(cursor, run_ctx) == {
        "tap_element", "wait_ms", "wait_screen_ready"
    }:
        state = read_state(cursor)
        for row in state.get("milestones") or []:
            if (
                isinstance(row, dict)
                and row.get("id") == focus_mid
                and row.get("input_refocus_required")
                and row.get("input_recovery_attempts") == 1
                and not row.get("input_recovery_tap_permitted")
            ):
                row["input_recovery_tap_permitted"] = True
                write_state(cursor, state)
                return None
    if (
        focus_mid == "otp_field"
        and str(ctx.get("cap_id") or "") == "tap_element"
        and bool(getattr(cursor, "otp_field_refocus_after_rewind", False))
    ):
        return None
    if (
        str(ctx.get("cap_id") or "") == "tap_element"
        and focus_mid in ("account_field", "otp_field", "prep_tap_input_box")
        and not str(getattr(run_ctx, "focus_action_ref", "") or "")
    ):
        # 输入框聚焦里程碑还没有本会话的聚焦点击引用：这次点击是补聚焦，不是重复点击。
        return None
    store = getattr(cursor, "flow_block_tap_streak", None)
    if not isinstance(store, dict):
        store = {}
    key = sel.lower()
    if focus_mid.startswith(("login_evidence_nav_", "login_context_restore_", "prep_reauth_nav_")):
        from mino_nexus.loop.llm_step_context import step_scope_key

        case_step, phase = step_scope_key(cursor)
        key = f"navigation:{case_step}:{phase}:{focus_mid}:{key}"
    # 只保留「界面指纹没变」的重复点击熔断：合法连点（multi_tap、连续步进）在指纹
    # 变化时清零；拿不到指纹就不拦（规划 §6）。
    fp = str(ctx.get("screen_fp") or "").strip()
    fp_store = getattr(cursor, "flow_block_tap_fp", None)
    if not isinstance(fp_store, dict):
        fp_store = {}
    if fp and fp_store.get(key) and fp_store[key] != fp:
        store[key] = 0
    fp_store[key] = fp
    setattr(cursor, "flow_block_tap_fp", fp_store)
    if not fp:
        return None
    store[key] = int(store.get(key) or 0) + 1
    setattr(cursor, "flow_block_tap_streak", store)
    limit = 2
    for step in fuse_steps:
        fuse = step.get("fuse") if isinstance(step.get("fuse"), dict) else {}
        if int(fuse.get("same_target_repeat") or 0) > 0:
            limit = min(limit, int(fuse["same_target_repeat"]))
    if store[key] < limit:
        return None
    reason = f"逻辑块 fuse：同一控件「{sel[:24]}」重复点击 {store[key]} 次，请检查当前页面与目标，停止重复点击。"
    return FlowBlockGuardVerdict(
        allowed=False,
        guard_id="flow_block:fuse_repeat_tap",
        code="action_fuse",
        reason=reason,
        dispatch_gate_code="flow_block:fuse_repeat_tap",
    )


_LOGIN_GUARD_IMPL: dict[str, GuardFn] = {
    "block_repeat_get_otp_when_ready": _guard_otp_ready_no_repeat_get_otp,
    "require_sms_send_before_otp": _guard_require_sms_before_otp,
    "block_repeat_email_tab": _guard_block_repeat_email,
    "require_otp_before_login_tap": _guard_require_otp_before_login_tap,
}


__all__ = [
    "FlowBlockGuardVerdict",
    "evaluate_flow_block_guards",
]
