"""逻辑块内 guards / fuse（P4 §2.6），在 milestones 登录流下替代 registry 登录 guard。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

LOGIN_REGISTRY_GUARD_IDS = frozenset(
    {
        "require_sms_send_before_otp",
        "block_repeat_email_tab",
        "require_otp_before_login_tap",
    }
)


@dataclass(frozen=True)
class FlowBlockGuardVerdict:
    allowed: bool
    guard_id: str = ""
    code: str = ""
    reason: str = ""
    dispatch_gate_code: str = ""
    remediate: str = ""


def filter_registry_guards_for_milestones(
    guard_names: list[str],
    cursor: Any,
) -> list[str]:
    from mino_nexus.loop.milestones import login_flow_under_milestones, milestone_v1_enabled

    if not milestone_v1_enabled() or not login_flow_under_milestones(cursor):
        return list(guard_names or [])
    return [g for g in (guard_names or []) if str(g) not in LOGIN_REGISTRY_GUARD_IDS]


def evaluate_flow_block_guards(ctx: dict[str, Any], cursor: Any) -> Optional[FlowBlockGuardVerdict]:
    """登录块 milestones 激活时，按 steps_json guards/fuse 求值。"""
    from mino_nexus.loop.milestones import login_flow_under_milestones, read_state

    if not login_flow_under_milestones(cursor):
        return None

    cap = str(ctx.get("cap_id") or "")
    phase = str(ctx.get("phase") or "")
    if phase != "do" or cap != "tap_element":
        if cap in ("get_otp", "input_text"):
            v = _guard_otp_ready_no_repeat_get_otp(ctx, cursor)
            if v:
                return v
            v = _guard_require_sms_before_otp(ctx, cursor)
            if v:
                return v
        return None

    state = read_state(cursor)
    block_id = str((state.get("block_ref") or {}).get("block_id") or "")
    run_ctx = ctx.get("run_ctx") or ctx.get("ctx")
    app_id = str(getattr(run_ctx, "app_id", "") or ctx.get("app_id") or "")
    from mino_nexus.services.nav_flow_block_catalog import resolve_effective_steps

    steps = resolve_effective_steps(app_id=app_id, block_id=block_id) if block_id else []

    v = _guard_block_repeat_email(ctx, cursor, steps=steps)
    if v:
        return v
    v = _guard_require_otp_before_login_tap(ctx, cursor)
    if v:
        return v
    v = _evaluate_fuse_repeat_tap(ctx, cursor, steps=steps)
    if v:
        return v
    return None


def _guard_block_repeat_email(
    ctx: dict[str, Any],
    cursor: Any,
    *,
    steps: list[dict[str, Any]],
) -> Optional[FlowBlockGuardVerdict]:
    has_guard = any(
        isinstance(s, dict)
        and any(
            str(g.get("type") or "") == "block_repeat" and str(g.get("key") or "") == "email_tab"
            for g in (s.get("guards") or [])
            if isinstance(g, dict)
        )
        for s in steps
    )
    if not has_guard:
        return None

    from mino_nexus.loop.ui_channel import UiChannel, ui_channel_from_ctx

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
    if (
        focus_mid == "otp_field"
        and str(ctx.get("cap_id") or "") == "tap_element"
        and bool(getattr(cursor, "otp_field_refocus_after_rewind", False))
    ):
        return None
    store = getattr(cursor, "flow_block_tap_streak", None)
    if not isinstance(store, dict):
        store = {}
    key = sel.lower()
    store[key] = int(store.get(key) or 0) + 1
    setattr(cursor, "flow_block_tap_streak", store)
    limit = 2
    for step in fuse_steps:
        fuse = step.get("fuse") if isinstance(step.get("fuse"), dict) else {}
        if int(fuse.get("same_target_repeat") or 0) > 0:
            limit = min(limit, int(fuse["same_target_repeat"]))
    if store[key] < limit:
        return None
    reason = f"逻辑块 fuse：同一控件「{sel[:24]}」重复点击 {store[key]} 次，请换步或 signal_ask_human。"
    return FlowBlockGuardVerdict(
        allowed=False,
        guard_id="flow_block:fuse_repeat_tap",
        code="action_fuse",
        reason=reason,
        dispatch_gate_code="flow_block:fuse_repeat_tap",
    )


def registry_login_guards_suppressed(ctx: dict[str, Any]) -> bool:
    step_cursor = ctx.get("step_cursor") or ctx.get("cursor")
    from mino_nexus.loop.milestones import login_flow_under_milestones, milestone_v1_enabled

    return bool(
        step_cursor is not None
        and milestone_v1_enabled()
        and login_flow_under_milestones(step_cursor)
    )


__all__ = [
    "FlowBlockGuardVerdict",
    "LOGIN_REGISTRY_GUARD_IDS",
    "evaluate_flow_block_guards",
    "filter_registry_guards_for_milestones",
    "registry_login_guards_suppressed",
]
