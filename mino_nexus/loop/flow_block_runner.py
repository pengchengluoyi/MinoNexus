"""通用逻辑块宏：确定性 dispatch 子 cap（登录 / 系统弹窗）。"""
from __future__ import annotations

from typing import Any, Optional

from mino_nexus.catalog.flow_block_seed import LOGIN_BLOCK_ID, SYSTEM_DIALOG_BLOCK_ID
from mino_nexus.core.schemas import PlanEvent
from mino_nexus.services.nav_flow_block_catalog import resolve_effective_steps


def _macro_done(ctx: Any) -> set[str]:
    raw = getattr(ctx, "login_flow_macro_done", None)
    if not isinstance(raw, set):
        raw = set()
        setattr(ctx, "login_flow_macro_done", raw)
    return raw


def _history_passed(history: list[str], cap: str) -> bool:
    needle = f"{cap} → pass"
    return any(needle in line for line in (history or []))


def _step_ready(
    step: dict[str, Any],
    *,
    ctx: Any,
    hierarchy_nodes: list[dict[str, Any]] | None,
    history: list[str],
) -> bool:
    cap = str(step.get("cap") or "")
    sid = str(step.get("id") or cap)
    if sid in _macro_done(ctx) or _history_passed(history, cap):
        return False
    if cap == "request_sms_code":
        from mino_nexus.loop.sms_auto import sms_send_geometry_ready
        from mino_nexus.loop.ui_channel import ui_channel_from_ctx, ui_channel_label

        return sms_send_geometry_ready(
            accounts_brief=str(getattr(ctx, "accounts_brief", "") or ""),
            hierarchy_nodes=hierarchy_nodes,
            has_request_sms_code=True,
            ui_channel=ui_channel_label(ui_channel_from_ctx(ctx)),
        )
    if cap == "accept_legal_consent":
        from mino_nexus.loop.ui_consent import find_consent_control

        return find_consent_control(list(hierarchy_nodes or [])) is not None
    if cap == "get_otp":
        return False
    return True


def try_run_login_flow_macro(
    proxy: Any,
    ctx: Any,
    *,
    run_id: str,
    case_seq: int,
    turn_seq: int,
    app_id: str,
    hierarchy_nodes: list[dict[str, Any]] | None,
    history_lines: list[str],
    interrupt: bool = False,
) -> Optional[dict[str, Any]]:
    """执行登录宏的下一步；无就绪步骤则返回 None。"""
    steps = resolve_effective_steps(app_id=app_id, block_id=LOGIN_BLOCK_ID)
    if not steps:
        return None
    for step in steps:
        if not _step_ready(
            step,
            ctx=ctx,
            hierarchy_nodes=hierarchy_nodes,
            history=history_lines,
        ):
            continue
        cap = str(step.get("cap") or "")
        sid = str(step.get("id") or cap)
        setattr(ctx, "login_flow_macro_active", True)
        event = PlanEvent(
            seq=int(turn_seq),
            capability_id=cap,
            event_kind=cap,
            params=dict(step.get("params") or {}),
            ai_reasoning=f"登录逻辑块 {LOGIN_BLOCK_ID} · {sid}",
            label=f"宏·{sid}",
        )
        from mino_nexus.core.protocol import EventStatus
        from mino_nexus.loop.local_executors import dispatch_local

        res = dispatch_local(
            event,
            ctx=ctx,
            router=proxy,
            target_package=str(getattr(ctx, "target_package", "") or ""),
        )
        st = res.status.value if hasattr(res.status, "value") else str(res.status)
        ok = str(st) in ("pass", EventStatus.PASS.value)
        if ok:
            _macro_done(ctx).add(sid)
            maybe_emit_login_flow_complete(ctx, app_id=app_id, history_lines=history_lines)
        return {
            "ok": ok,
            "status": st,
            "summary": str(res.summary or res.error or cap),
            "capability_id": cap,
            "block_id": LOGIN_BLOCK_ID,
            "step_id": sid,
            "interrupt": bool(interrupt),
        }
    setattr(ctx, "login_flow_macro_active", False)
    maybe_emit_login_flow_complete(ctx, app_id=app_id, history_lines=history_lines)
    return None


def login_block_required_steps_done(
    ctx: Any,
    *,
    app_id: str,
    history_lines: list[str],
) -> bool:
    steps = resolve_effective_steps(app_id=app_id, block_id=LOGIN_BLOCK_ID)
    if not steps:
        return False
    done = _macro_done(ctx)
    for step in steps:
        if step.get("optional"):
            continue
        cap = str(step.get("cap") or "")
        sid = str(step.get("id") or cap)
        if sid in done or _history_passed(history_lines, cap):
            continue
        return False
    return True


def maybe_emit_login_flow_complete(
    ctx: Any,
    *,
    app_id: str,
    history_lines: list[str],
) -> bool:
    """登录流块必做步骤完成且已登录（或 get_otp 已成功）时写 device_app + 号池。"""
    if getattr(ctx, "_login_flow_transition_emitted", False):
        return True
    if not login_block_required_steps_done(ctx, app_id=app_id, history_lines=history_lines):
        return False
    from mino_nexus.services.resource_preflight import effective_device_session

    sn = str(getattr(ctx, "sn", "") or "").strip()
    pkg = str(getattr(ctx, "target_package", "") or "").strip()
    sess = effective_device_session(ctx, sn=sn, package_id=pkg)
    otp_ok = "otp_fill" in _macro_done(ctx) or _history_passed(history_lines, "get_otp")
    if sess != "logged_in" and not otp_ok:
        return False
    from mino_nexus.services.resource_transition_engine import fire_transition

    fire_transition(ctx, "login_flow_complete", source="flow_block")
    setattr(ctx, "_login_flow_transition_emitted", True)
    setattr(ctx, "login_flow_macro_active", False)
    return True


def try_run_system_dialog_macro(
    proxy: Any,
    ctx: Any,
    *,
    turn_seq: int,
    target_package: str = "",
) -> Optional[dict[str, Any]]:
    """系统挡屏时走 fb.global.system_dialog（与 recovery unified 同源）。"""
    if getattr(ctx, "system_dialog_macro_done", False):
        return None
    overlay = str(getattr(ctx, "system_overlay", "") or "").strip().lower()
    fg = str(getattr(ctx, "app_foreground", "") or "").strip().lower()
    overlay_on = overlay in ("yes", "true", "1")
    if not overlay_on and fg != "no":
        return None
    from mino_nexus.loop.system_dialog_recovery import try_proactive_system_permission

    out = try_proactive_system_permission(
        ctx=ctx,
        router=proxy,
        target_package=target_package,
        turn_seq=turn_seq,
    )
    if out and out.get("ok"):
        setattr(ctx, "system_dialog_macro_done", True)
    return out
