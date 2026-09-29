"""逻辑块 runner v2：按 success_criteria.milestones 推进 hook 步（P3+ / P4 门控）。"""
from __future__ import annotations

from typing import Any, Optional

from mino_nexus.loop.milestones import (
    first_pending_hook_milestone,
    login_block_id_for_ctx,
    note_tool_pass_milestone,
    read_state,
)
from mino_nexus.services.nav_flow_block_catalog import resolve_effective_steps


def skip_login_entry_if_form_visible(cursor: Any, ctx: Any, *, writer: Any = None) -> bool:
    """登录表单已经在屏上时，登录入口这一步记跳过，下一焦点才是聚焦账号。"""
    from mino_nexus.loop.milestone_orchestrator import ensure_single_in_progress, in_progress_milestone
    from mino_nexus.loop.milestones import write_state

    row = in_progress_milestone(read_state(cursor))
    if not row or str(row.get("id") or "") != "login_entry":
        return False
    nodes = [n for n in (getattr(ctx, "nav_hierarchy_nodes", None) or []) if isinstance(n, dict)]
    if not nodes:
        return False
    from mino_nexus.loop.ui_channel import ui_channel_from_ctx, ui_channel_label
    from mino_nexus.loop.ui_sms_request import credential_field_for_login
    from mino_nexus.services.account_credential_text import login_kind_from_ctx

    kind = login_kind_from_ctx(ctx)
    channel = ui_channel_label(ui_channel_from_ctx(ctx))
    hit = credential_field_for_login(nodes, login_kind=kind, channel=channel)
    if hit is None:
        other = "email" if kind != "email" else "phone"
        hit = credential_field_for_login(nodes, login_kind=other, channel=channel)
    if hit is None:
        return False
    state = read_state(cursor)
    for item in state.get("milestones") or []:
        if isinstance(item, dict) and str(item.get("id") or "") == "login_entry":
            item["status"] = "skipped"
            item["skip_reason"] = "login_form_visible"
    write_state(cursor, state)
    ensure_single_in_progress(cursor, writer=writer)
    if writer is not None:
        try:
            writer.append(
                "milestone/focus",
                {"milestone_id": "login_entry", "skip_reason": "login_form_visible"},
            )
        except Exception:  # noqa: BLE001
            pass
    return True


def try_dispatch_block_hook(
    proxy: Any,
    ctx: Any,
    cursor: Any,
    *,
    run_id: str,
    turn_seq: int,
    app_id: str,
    history_lines: list[str] | None = None,
    writer: Any = None,
) -> Optional[dict[str, Any]]:
    """若当前里程碑有待执行 hook，dispatch 对应 cap（与 catalog 步骤对齐）。"""
    state = read_state(cursor)
    pending = first_pending_hook_milestone(state)
    if not pending:
        return None
    block_id = str((state.get("block_ref") or {}).get("block_id") or login_block_id_for_ctx(ctx))
    steps = resolve_effective_steps(app_id=str(app_id or ""), block_id=block_id)
    mid = str(pending.get("id") or "")
    step = next((s for s in steps if str(s.get("id") or "") == mid), None)
    cap = str(pending.get("hook_cap") or "").strip()
    if not cap and step:
        cap = str(step.get("hook_cap") or step.get("cap") or "").strip()
    if not cap:
        return None
    if str(pending.get("kind") or "").strip().lower() != "hook":
        return None

    hist = list(history_lines or [])
    intents = set(getattr(cursor, "step_intents_done", None) or set())

    if mid == "otp_fill" and pending.get("otp_ready"):
        fill = _dispatch_otp_fill(
            proxy,
            ctx,
            cursor,
            turn_seq=turn_seq,
            block_id=block_id,
            mid=mid,
            writer=writer,
        )
        if fill:
            return fill
        from mino_nexus.loop.program_device_fallback import vision_fallback_result

        from mino_nexus.loop.local_executors import _resolve_otp

        _code, _ = _resolve_otp(ctx)
        return vision_fallback_result(
            capability_id="input_text",
            params={"field": "sms_code", "text": _code or ""},
            reason="no_web_coords",
            milestone_id=mid,
            block_id=block_id,
            step_id=mid,
        )

    if cap == "get_otp":
        from mino_nexus.loop.login_submit import otp_fetch_allowed

        if not otp_fetch_allowed(hist, intents, ctx, cursor=cursor):
            return None

    if cap == "get_otp" and mid == "otp_fill":
        from mino_nexus.loop.login_submit import _otp_code_already_entered

        if _otp_code_already_entered(hist, intents):
            return None

    if cap == "confirm_login_state" and mid == "login_state":
        return _dispatch_login_state(
            proxy,
            ctx,
            cursor,
            block_id=block_id,
            step=step,
            writer=writer,
        )

    if cap == "lease_account" and mid == "account_fill":
        from mino_nexus.loop.email_login_auto import _email_input_done
        from mino_nexus.services.account_credential_text import (
            lease_email_address,
            lease_phone_digits,
        )

        if _email_input_done(hist, intents):
            return None
        acc = dict(getattr(ctx, "picked_account", None) or {})
        has_credential = bool(lease_email_address(acc) or lease_phone_digits(acc))
        # 号已经在手上：这一回合只填账号，不再租一次，也不做资产转移。
        if pending.get("lease_ready") or has_credential:
            from mino_nexus.loop.milestones import _mark_hook_ready_flag, write_state

            state = read_state(cursor)
            _mark_hook_ready_flag(state, mid, "lease_ready")
            write_state(cursor, state)
            filled = _dispatch_email_fill(
                proxy,
                ctx,
                cursor,
                turn_seq=turn_seq,
                block_id=block_id,
                mid=mid,
                writer=writer,
            )
            if filled is not None:
                return filled
            return None

    from mino_nexus.loop.router_proxy import is_local_cap

    if not is_local_cap(cap):
        return None
    from mino_nexus.core.schemas import PlanEvent
    from mino_nexus.core.protocol import EventStatus
    from mino_nexus.loop.local_executors import dispatch_local

    event = PlanEvent(
        seq=int(turn_seq),
        capability_id=cap,
        event_kind=cap,
        params=dict(step.get("params") or {}) if step else {},
        ai_reasoning=f"逻辑块 {block_id} · milestone {mid}",
        label=f"块·{mid or cap}",
    )
    res = dispatch_local(
        event,
        ctx=ctx,
        router=proxy,
        target_package=str(getattr(ctx, "target_package", "") or ""),
    )
    st = res.status.value if hasattr(res.status, "value") else str(res.status)
    ok = str(st) in ("pass", EventStatus.PASS.value)
    if cap == "get_otp" and mid == "otp_fill":
        if ok:
            cursor.get_otp_hook_fail_streak = 0
        else:
            cursor.get_otp_hook_fail_streak = (
                int(getattr(cursor, "get_otp_hook_fail_streak", 0) or 0) + 1
            )
    hook_summary = str(res.summary or res.error or cap)
    hook_params = dict(step.get("params") or {}) if step else {}
    from mino_nexus.loop.program_tool_log import log_program_tool

    log_program_tool(
        writer,
        capability_id=cap,
        params=hook_params,
        status=st,
        summary=hook_summary,
        executor_used="internal",
    )
    if ok:
        note_tool_pass_milestone(
            cursor,
            capability_id=cap,
            milestone_id=mid,
            writer=writer,
            tool_summary=hook_summary,
            ctx=ctx,
        )
    elif cap == "accept_legal_consent" and pending.get("optional"):
        state = read_state(cursor)
        for row in state.get("milestones") or []:
            if isinstance(row, dict) and str(row.get("id") or "") == mid:
                row["status"] = "skipped"
                row["skip_reason"] = "no_consent_control"
        from mino_nexus.loop.milestones import write_state

        write_state(cursor, state)
        return {
            "ok": True,
            "status": "skipped",
            "summary": "屏上没有协议控件，跳过同意协议",
            "capability_id": cap,
            "block_id": block_id,
            "step_id": mid,
            "milestone_id": mid,
            "session_logged": "flow_block",
        }
    return {
        "ok": ok,
        "status": st,
        "summary": hook_summary,
        "capability_id": cap,
        "block_id": block_id,
        "step_id": mid,
        "milestone_id": mid,
        "session_logged": "flow_block",
    }


def _dispatch_email_fill(
    proxy: Any,
    ctx: Any,
    cursor: Any,
    *,
    turn_seq: int,
    block_id: str,
    mid: str,
    writer: Any,
) -> Optional[dict[str, Any]]:
    from mino_nexus.core.protocol import EventStatus
    from mino_nexus.loop.device_execute_params import enrich_web_input_text_params
    from mino_nexus.loop.local_executors import _dispatch_device
    from mino_nexus.services.account_credential_text import (
        lease_email_address,
        lease_phone_digits,
        login_kind_from_ctx,
    )

    acc = dict(getattr(ctx, "picked_account", None) or {})
    from mino_nexus.loop.milestones import _mark_hook_ready_flag, read_state, write_state

    kind = login_kind_from_ctx(ctx)
    email = lease_email_address(acc)
    phone = lease_phone_digits(acc)
    if kind == "email" and email:
        field, text = "email", email
    elif phone:
        field, text = "phone", phone
    elif email:
        field, text = "email", email
    else:
        return None
    state = read_state(cursor)
    _mark_hook_ready_flag(state, mid, "lease_ready")
    write_state(cursor, state)
    fill_params: dict[str, Any] = {"field": field, "text": text}
    from mino_nexus.loop.ui_channel import UiChannel, ui_channel_from_ctx

    if ui_channel_from_ctx(ctx) != UiChannel.WEB:
        from mino_nexus.loop.program_device_fallback import vision_fallback_result

        return vision_fallback_result(
            capability_id="input_text",
            params={"field": field, "text": text},
            reason="account_fill",
            milestone_id=mid,
            block_id=block_id,
            step_id=mid,
        )
    cached_email = getattr(ctx, "web_email_tap_milli", None)
    if isinstance(cached_email, (list, tuple)) and len(cached_email) >= 2:
        try:
            fill_params["x"] = int(cached_email[0])
            fill_params["y"] = int(cached_email[1])
            fill_params["email_coords_from_prior_tap_only"] = True
            fill_params["web_coordinate_only"] = True
            fill_params["tap_prefer_coordinates"] = True
        except (TypeError, ValueError):
            pass
    params = enrich_web_input_text_params(fill_params, ctx)
    if params.get("x") is None and params.get("y") is None:
        from mino_nexus.loop.program_device_fallback import vision_fallback_result

        return vision_fallback_result(
            capability_id="input_text",
            params={"field": field, "text": text},
            reason="no_web_coords",
            milestone_id=mid,
            block_id=block_id,
            step_id=mid,
        )
    from mino_nexus.loop.channel_observation import bind_fresh_observation, read_field
    from mino_nexus.loop.flow_block_exit import device_input_text_exit
    from mino_nexus.loop.program_tool_log import log_program_tool
    from mino_nexus.loop.ui_channel import UiChannel, ui_channel_from_ctx

    res = _dispatch_device(
        proxy,
        ctx=ctx,
        seq=turn_seq,
        cap="input_text",
        params=params,
        label="块·填写登录账号",
    )
    st = res.status.value if hasattr(res.status, "value") else str(res.status)
    summ = str(res.summary or res.error or "input_text")
    field_value = None
    if ui_channel_from_ctx(ctx) == UiChannel.WEB:
        bind_fresh_observation(ctx, proxy)
        field_value = read_field(ctx, getattr(ctx, "nav_hierarchy_nodes", None), field)
    exit_st, _ = device_input_text_exit(
        summary=summ,
        field=field,
        field_value=field_value,
        expected=str(params.get("text") or ""),
    )
    ok = str(st) in ("pass", EventStatus.PASS.value) and exit_st == "pass"
    log_program_tool(
        writer,
        capability_id="input_text",
        params=params,
        status="pass" if ok else "fail",
        summary=summ,
        executor_used="internal",
    )
    if ok:
        cursor.step_intents_done.add("login_email")
        note_tool_pass_milestone(
            cursor,
            capability_id="input_text",
            milestone_id=mid,
            writer=writer,
            params=params,
            tool_summary=summ,
            ctx=ctx,
        )
    return {
        "ok": ok,
        "status": "pass" if ok else "fail",
        "summary": str(res.summary or res.error or "input_text"),
        "capability_id": "input_text",
        "block_id": block_id,
        "step_id": mid,
        "milestone_id": mid,
        "session_logged": "flow_block",
    }


def _dispatch_otp_fill(
    proxy: Any,
    ctx: Any,
    cursor: Any,
    *,
    turn_seq: int,
    block_id: str,
    mid: str,
    writer: Any,
) -> Optional[dict[str, Any]]:
    from mino_nexus.core.protocol import EventStatus
    from mino_nexus.loop.device_execute_params import enrich_web_input_text_params
    from mino_nexus.loop.local_executors import _dispatch_device, _resolve_otp

    from mino_nexus.loop.program_tool_log import log_program_tool

    code, _src = _resolve_otp(ctx)
    if not code:
        return None
    from mino_nexus.loop.milestones import milestone_by_id, read_state
    from mino_nexus.loop.ui_channel import UiChannel, ui_channel_from_ctx

    state = read_state(cursor)
    otp_field_row = milestone_by_id(state, "otp_field")
    if otp_field_row is not None and str(otp_field_row.get("status") or "") not in (
        "pass",
        "skipped",
    ):
        if writer is not None:
            writer.append(
                "program/otp_fill_skip",
                {"reason": "otp_field_not_passed", "field": "sms_code"},
            )
        return {
            "ok": False,
            "status": "fail",
            "summary": "验证码已取到；须先点验证码框，再按该次点击输入",
            "capability_id": "input_text",
            "block_id": block_id,
            "step_id": mid,
            "milestone_id": mid,
            "session_logged": "flow_block",
        }
    tap = None
    if ui_channel_from_ctx(ctx) == UiChannel.WEB:
        from mino_nexus.loop.device_execute_params import _cached_sms_milli

        tap = _cached_sms_milli(ctx)
        if tap is None:
            from mino_nexus.loop.milestones import rewind_otp_field_for_refocus

            rewind_otp_field_for_refocus(cursor, writer=writer)
            if writer is not None:
                writer.append(
                    "program/otp_fill_skip",
                    {"reason": "otp_field_tap_missing", "field": "sms_code"},
                )
            return {
                "ok": False,
                "status": "fail",
                "summary": "没有验证码框点击落点：请重新点验证码框，输入只使用该次点击的坐标",
                "capability_id": "input_text",
                "block_id": block_id,
                "step_id": mid,
                "milestone_id": mid,
                "session_logged": "flow_block",
            }
    fill_params: dict[str, Any] = {
        "field": "sms_code",
        "text": code,
        "otp_coords_from_prior_tap_only": tap is not None,
    }
    if tap is not None:
        fill_params["x"] = tap[0]
        fill_params["y"] = tap[1]
        fill_params["web_coordinate_only"] = True
        fill_params["tap_prefer_coordinates"] = True
    params = enrich_web_input_text_params(fill_params, ctx)
    res = _dispatch_device(
        proxy,
        ctx=ctx,
        seq=turn_seq,
        cap="input_text",
        params=params,
        label="块·填写验证码",
    )
    st = res.status.value if hasattr(res.status, "value") else str(res.status)
    summ = str(res.summary or res.error or "input_text")
    from mino_nexus.loop.channel_observation import bind_fresh_observation, read_field
    from mino_nexus.loop.flow_block_exit import device_input_text_exit, log_milestone_exit_eval

    field_value = None
    if ui_channel_from_ctx(ctx) == UiChannel.WEB:
        bind_fresh_observation(ctx, proxy)
        field_value = read_field(ctx, getattr(ctx, "nav_hierarchy_nodes", None), "sms_code")
    sent = getattr(ctx, "last_device_execute_params", None) if ctx is not None else None
    if not isinstance(sent, dict):
        sent = params
    vw, vh = 0, 0
    raw_vp = getattr(ctx, "viewport_wh", None) if ctx is not None else None
    if isinstance(raw_vp, (list, tuple)) and len(raw_vp) >= 2:
        try:
            vw, vh = int(raw_vp[0]), int(raw_vp[1])
        except (TypeError, ValueError):
            vw, vh = 0, 0
    exit_st, exit_reason = device_input_text_exit(
        summary=summ,
        field="sms_code",
        field_value=field_value,
        expected=str(params.get("text") or sent.get("text") or ""),
        sent_x=sent.get("x"),
        sent_y=sent.get("y"),
        viewport_w=vw,
        viewport_h=vh,
    )
    ok = str(st) in ("pass", EventStatus.PASS.value) and exit_st == "pass"
    log_milestone_exit_eval(
        writer,
        milestone_id=mid,
        exit_status=exit_st,
        reason=exit_reason,
        capability_id="input_text",
        would_block=not ok,
    )
    log_program_tool(
        writer,
        capability_id="input_text",
        params=params,
        status="pass" if ok else "fail",
        summary=summ,
        executor_used="internal",
    )
    streak = int(getattr(cursor, "otp_fill_device_fail_streak", 0) or 0)
    if ok:
        cursor.otp_fill_device_fail_streak = 0
        cursor.step_intents_done.add("otp_fill")
        note_tool_pass_milestone(
            cursor,
            capability_id="input_text",
            milestone_id=mid,
            writer=writer,
            params=params,
            tool_summary=summ,
            ctx=ctx,
        )
    else:
        cursor.otp_fill_device_fail_streak = streak + 1
    out_st = "pass" if ok else "fail"
    return {
        "ok": ok,
        "status": out_st,
        "summary": str(res.summary or res.error or "input_text"),
        "capability_id": "input_text",
        "block_id": block_id,
        "step_id": mid,
        "milestone_id": mid,
        "session_logged": "flow_block",
    }


def try_dispatch_otp_fill_after_get_otp(
    proxy: Any,
    ctx: Any,
    cursor: Any,
    *,
    turn_seq: int,
    app_id: str,
    writer: Any = None,
    history_lines: list[str] | None = None,
) -> Optional[dict[str, Any]]:
    from mino_nexus.loop.milestones import otp_fill_device_complete

    hist = list(history_lines or [])
    if otp_fill_device_complete(cursor, hist):
        return None
    state = read_state(cursor)
    row = None
    for m in state.get("milestones") or []:
        if isinstance(m, dict) and str(m.get("id") or "") == "otp_fill":
            row = m
            break
    if not row or str(row.get("status") or "") in ("pass", "skipped", "failed"):
        return None
    if str(row.get("device_cap") or "") != "input_text":
        return None
    field_row = None
    for m in state.get("milestones") or []:
        if isinstance(m, dict) and str(m.get("id") or "") == "otp_field":
            field_row = m
            break
    if field_row is not None and str(field_row.get("status") or "") not in ("pass", "skipped"):
        return None
    block_id = str((state.get("block_ref") or {}).get("block_id") or login_block_id_for_ctx(ctx))
    try:
        proxy.observe("screenshot", force_fresh=True)
    except Exception:
        pass
    return _dispatch_otp_fill(
        proxy,
        ctx,
        cursor,
        turn_seq=turn_seq,
        block_id=block_id,
        mid="otp_fill",
        writer=writer,
    )


def _dispatch_login_state(
    proxy: Any,
    ctx: Any,
    cursor: Any,
    *,
    block_id: str,
    step: dict[str, Any] | None,
    writer: Any,
) -> dict[str, Any]:
    """登录块结尾：先采层级再截屏，用这一对判断是否离开登录表单。"""
    from mino_nexus.loop.channel_observation import (
        capture_decision_frame,
        login_credential_form_open,
    )
    from mino_nexus.loop.milestones import note_tool_pass_milestone

    params = dict(step.get("params") or {}) if step else {}
    try:
        max_turns = max(1, int(params.get("max_turns") or 3))
    except (TypeError, ValueError):
        max_turns = 3
    _shot, frame_thumb = capture_decision_frame(proxy, ctx)
    nodes = getattr(ctx, "nav_hierarchy_nodes", None)
    if login_credential_form_open(nodes if isinstance(nodes, list) else None):
        streak = int(getattr(cursor, "login_state_waits", 0) or 0) + 1
        cursor.login_state_waits = streak
        if streak >= max_turns:
            return {
                "ok": False,
                "status": "fail",
                "case_fail": True,
                "summary": "提交后仍在登录页，登录未成功",
                "capability_id": "confirm_login_state",
                "block_id": block_id,
                "step_id": "login_state",
                "milestone_id": "login_state",
                "session_logged": "flow_block",
                "thumb": frame_thumb,
            }
        return {
            "ok": False,
            "status": "wait",
            "defer": True,
            "summary": "登录页还在，下一回合再看当前屏",
            "capability_id": "confirm_login_state",
            "block_id": block_id,
            "step_id": "login_state",
            "milestone_id": "login_state",
            "session_logged": "flow_block",
            "thumb": frame_thumb,
        }
    cursor.login_state_waits = 0
    note_tool_pass_milestone(
        cursor,
        capability_id="confirm_login_state",
        milestone_id="login_state",
        writer=writer,
        tool_summary="已离开登录表单",
        ctx=ctx,
    )
    return {
        "ok": True,
        "status": "pass",
        "summary": "已离开登录表单",
        "capability_id": "confirm_login_state",
        "block_id": block_id,
        "step_id": "login_state",
        "milestone_id": "login_state",
        "session_logged": "flow_block",
        "thumb": frame_thumb,
    }


def try_dispatch_email_fill_after_lease(
    proxy: Any,
    ctx: Any,
    cursor: Any,
    *,
    turn_seq: int,
    app_id: str,
    writer: Any = None,
) -> Optional[dict[str, Any]]:
    """lease_account 成功后：account_fill 须 input_text，补发填写账号（vision 直调租号时）。"""
    from mino_nexus.loop.email_login_auto import _email_input_done

    intents = set(getattr(cursor, "step_intents_done", None) or set())
    hist: list[str] = []
    if _email_input_done(hist, intents):
        return None
    state = read_state(cursor)
    pending = first_pending_hook_milestone(state)
    if not pending or str(pending.get("id") or "") != "account_fill":
        return None
    if str(pending.get("device_cap") or "") != "input_text":
        return None
    block_id = str((state.get("block_ref") or {}).get("block_id") or login_block_id_for_ctx(ctx))
    acc = dict(getattr(ctx, "picked_account", None) or {})
    if not (acc.get("email") or acc.get("login_email")):
        return None
    try:
        proxy.observe("screenshot", force_fresh=True)
    except Exception:
        pass
    return _dispatch_email_fill(
        proxy,
        ctx,
        cursor,
        turn_seq=turn_seq,
        block_id=block_id,
        mid="account_fill",
        writer=writer,
    )


def run_otp_fill_followup(
    proxy: Any,
    ctx: Any,
    cursor: Any,
    *,
    turn_seq: int,
    app_id: str,
    writer: Any,
    history_lines: list[str],
) -> Optional[dict[str, Any]]:
    """get_otp 后尝试程序填码；未填完时设置 correction_hint。"""
    from mino_nexus.loop.milestones import login_flow_under_milestones, otp_fill_device_complete

    if not login_flow_under_milestones(cursor):
        return None
    if otp_fill_device_complete(cursor, history_lines):
        return None
    fill = try_dispatch_otp_fill_after_get_otp(
        proxy,
        ctx,
        cursor,
        turn_seq=turn_seq,
        app_id=app_id,
        writer=writer,
        history_lines=history_lines,
    )
    if not fill:
        cursor.correction_hint = (
            "验证码已取到。下一动作点验证码输入框；点中后程序按该次点击的坐标输入，勿再 get_otp。"
        )
    elif not fill.get("ok"):
        cursor.correction_hint = str(fill.get("summary") or "")[:280]
    return fill


__all__ = [
    "run_otp_fill_followup",
    "try_dispatch_block_hook",
    "try_dispatch_email_fill_after_lease",
    "try_dispatch_otp_fill_after_get_otp",
]
