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
    block_id = str(pending.get("source_block") or (state.get("block_ref") or {}).get("block_id") or "")
    if not block_id and pending.get("source") != "do_program":
        block_id = login_block_id_for_ctx(ctx)
    steps = resolve_effective_steps(app_id=str(app_id or ""), block_id=block_id) if block_id else []
    mid = str(pending.get("id") or "")
    step = next((s for s in steps if str(s.get("id") or "") == mid), None)
    cap = str(pending.get("hook_cap") or "").strip()
    if not cap and step:
        cap = str(step.get("hook_cap") or step.get("cap") or "").strip()
    if not cap:
        return None
    if str(pending.get("kind") or "").strip().lower() != "hook":
        return None
    from mino_nexus.catalog.skill_channel import capability_dispatch_ok, is_program_caller
    from mino_nexus.loop.router_proxy import is_local_cap

    if not is_program_caller(cap) and not is_local_cap(cap):
        return None

    if not capability_dispatch_ok(cap):
        return {
            "ok": False,
            "case_fail": True,
            "status": "blocked",
            "error_kind": "capability_unavailable",
            "summary": f"{cap} 未启用或当前不可覆盖，不能派发",
            "capability_id": cap,
            "milestone_id": mid,
        }
    from mino_nexus.catalog.skill_channel import phase_allows

    phase = str(getattr(cursor, "phase", "") or "")
    if phase in ("prep", "do", "check") and not phase_allows(cap, phase):
        if writer is not None:
            writer.append(
                "guard/block",
                {"capability_id": cap, "reason": "phase", "phase": phase, "milestone_id": mid},
            )
        return {
            "ok": False,
            "case_fail": True,
            "status": "blocked",
            "error_kind": "capability_phase_mismatch",
            "summary": f"{cap} 不能在{phase}阶段派发",
            "capability_id": cap,
            "milestone_id": mid,
        }

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

    from mino_nexus.core.schemas import PlanEvent
    from mino_nexus.core.protocol import EventStatus

    hook_params = dict(step.get("params") or {}) if step else {}
    hook_params.update(dict(pending.get("params") or {}))
    event = PlanEvent(
        seq=int(turn_seq),
        capability_id=cap,
        event_kind=cap,
        params=hook_params,
        ai_reasoning=f"逻辑块 {block_id} · milestone {mid}",
        label=f"块·{mid or cap}",
    )
    res = proxy.dispatch(event, run_id=run_id, step_idx=int(turn_seq), ctx=ctx)
    executor_used = str(getattr(res, "executor_used", "") or "router_proxy")
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
    from mino_nexus.loop.observe.program_tool_log import log_program_tool

    log_program_tool(
        writer,
        capability_id=cap,
        params=hook_params,
        status=st,
        summary=hook_summary,
        executor_used=executor_used,
        result=res,
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
    blocked = not ok and not (cap == "get_otp" and mid == "otp_fill")
    return {
        "ok": ok,
        "case_fail": blocked,
        "status": "blocked" if blocked else st,
        "error_kind": "program_hook_failed" if blocked else "",
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
            params={"field": field, "text": text, "mode": "replace", "focus_ref": "previous_focus_action"},
            reason="account_fill",
            milestone_id=mid,
            block_id=block_id,
            step_id=mid,
        )
    fill_params["mode"] = "replace"
    fill_params["focus_ref"] = "previous_focus_action"
    params = enrich_web_input_text_params(fill_params, ctx)
    from mino_nexus.loop.channel_observation import read_field_after_input
    from mino_nexus.loop.flow_block_exit import device_input_text_exit
    from mino_nexus.loop.observe.program_tool_log import log_program_tool
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
        field_value = read_field_after_input(ctx, proxy, field)
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
        result=res,
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

    from mino_nexus.loop.observe.program_tool_log import log_program_tool

    code, _src = _resolve_otp(ctx)
    if not code:
        return None
    from mino_nexus.loop.milestones import milestone_by_id, read_state
    from mino_nexus.loop.ui_channel import UiChannel, ui_channel_from_ctx

    if ui_channel_from_ctx(ctx) == UiChannel.DESKTOP:
        from mino_nexus.loop.program_device_fallback import vision_fallback_result

        return vision_fallback_result(
            capability_id="input_text",
            params={"field": "sms_code", "text": code, "mode": "replace", "focus_ref": "previous_focus_action"},
            reason="otp_fill",
            milestone_id=mid,
            block_id=block_id,
            step_id=mid,
        )
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
    from mino_nexus.loop.channel_observation import read_field_after_input
    from mino_nexus.loop.flow_block_exit import device_input_text_exit, log_milestone_exit_eval

    field_value = None
    if ui_channel_from_ctx(ctx) == UiChannel.WEB:
        field_value = read_field_after_input(ctx, proxy, "sms_code")
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
        result=res,
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


def try_review_login_submission(
    proxy: Any,
    ctx: Any,
    cursor: Any,
    *,
    shot: Any,
    writer: Any = None,
) -> Optional[dict[str, Any]]:
    from mino_nexus.action_space.scheme import action_scheme
    from mino_nexus.loop.milestone_orchestrator import in_progress_milestone

    if action_scheme(ctx) != "visual":
        return None
    prior = getattr(cursor, "visual_prior", None)
    state = read_state(cursor)
    focus = in_progress_milestone(state) or {}
    if not isinstance(prior, dict) or prior.get("milestone_id") != "submit_login":
        return None
    if focus.get("id") != "submit_login" or prior.get("capability_id") != "tap_element":
        return None
    dispatch_turn = int(prior.get("dispatch_turn") or 0)
    if not prior.get("attempt_ref") or dispatch_turn <= 0 or int(getattr(writer, "turn", 0) or 0) <= dispatch_turn:
        return None
    if not getattr(ctx, "decision_screenshot_hash", ""):
        return None
    rows = [row for row in state.get("milestones") or [] if isinstance(row, dict)]
    pending = [row for row in rows if not row.get("optional") and row.get("status") not in ("pass", "skipped")]
    if [row.get("id") for row in pending] != ["submit_login", "login_state"]:
        return None
    if pending[-1].get("hook_cap") != "confirm_login_state":
        return None
    block_id = str(focus.get("source_block") or (state.get("block_ref") or {}).get("block_id") or "")
    steps = resolve_effective_steps(app_id=str(getattr(ctx, "app_id", "") or ""), block_id=block_id) if block_id else []
    step = next((row for row in steps if row.get("id") == "login_state"), None)
    return _dispatch_login_state(
        proxy, ctx, cursor, block_id=block_id, step=step, writer=writer,
        shot=shot, review_submit=True,
    )


def _visual_login_form_open(shot: Any) -> bool | None:
    """纯视觉：看决策图判断账号框和验证码框是否还同时在。看不了就返回 None，不当成已离开。"""
    if shot is None or not getattr(shot, "has_image", lambda: False)():
        return None
    from mino_nexus.loop.vision_assert import vision_assert_checkpoints

    result = vision_assert_checkpoints(
        expectation="账号输入框和验证码输入框是否同时仍在画面中。两者都在则不通过；已经看不到这组输入框则通过。",
        image_base64=str(getattr(shot, "image_base64", "") or ""),
        image_mime=str(getattr(shot, "image_mime", "") or "image/png"),
    )
    warnings = list(getattr(result, "parse_warnings", None) or [])
    if warnings or getattr(result, "error_kind", ""):
        return None
    return not bool(getattr(result, "passed", False))


def _dispatch_login_state(
    proxy: Any,
    ctx: Any,
    cursor: Any,
    *,
    block_id: str,
    step: dict[str, Any] | None,
    writer: Any,
    shot: Any = None,
    review_submit: bool = False,
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
    from mino_nexus.action_space.scheme import action_scheme

    visual = action_scheme(ctx) == "visual"
    if visual and shot is not None:
        _shot, frame_thumb = shot, ""
    else:
        _shot, frame_thumb = capture_decision_frame(proxy, ctx, hierarchy=not visual)
    if visual:
        import hashlib

        prior = getattr(cursor, "visual_prior", None) or {}
        attempt_ref = str(prior.get("attempt_ref") or "") if review_submit else ""
        image_data = str(getattr(_shot, "image_base64", "") or "")
        image_hash = hashlib.sha256(image_data.encode("ascii", errors="ignore")).hexdigest() if image_data else ""
        cache = getattr(cursor, "login_review_cache", None) or {}
        if attempt_ref and image_hash and cache.get("attempt_ref") == attempt_ref and cache.get("image_hash") == image_hash:
            form_open = cache["form_open"]
            if writer:
                writer.append("login/review_reused", {"attempt_ref": attempt_ref, "image_hash": image_hash[:20]})
        else:
            form_open = _visual_login_form_open(_shot)
            if attempt_ref and image_hash:
                cursor.login_review_cache = {"attempt_ref": attempt_ref, "image_hash": image_hash, "form_open": form_open}
        if form_open is None:
            cursor.login_state_waits = int(getattr(cursor, "login_state_waits", 0) or 0) + 1
            if cursor.login_state_waits >= max_turns:
                return {
                    "ok": False, "case_fail": True, "status": "blocked",
                    "error_kind": "login_observation_unavailable",
                    "summary": "登录结果连续无法验证，已停止，不能判定登录成功或失败",
                    "capability_id": "confirm_login_state", "milestone_id": "login_state",
                }
            return {
                "ok": False,
                "status": "wait",
                "defer": True,
                "summary": "看图未能判断是否还在登录表单，下一回合再看当前屏",
                "capability_id": "confirm_login_state",
                "block_id": block_id,
                "step_id": "login_state",
                "milestone_id": "login_state",
                "session_logged": "flow_block",
                "thumb": frame_thumb,
            }
    else:
        nodes = getattr(ctx, "nav_hierarchy_nodes", None)
        form_open = login_credential_form_open(nodes if isinstance(nodes, list) else None)
    if form_open:
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
    if review_submit:
        from mino_nexus.loop.visual_review import _confirm_visual_prior

        prior = dict(getattr(cursor, "visual_prior", None) or {})
        _confirm_visual_prior(
            cursor, ctx, "submit_login", capability_id="tap_element", writer=writer,
            in_prep=str(getattr(cursor, "phase", "")) == "prep",
        )
        cursor.visual_prior = None
        cursor.pending_visual_review = None
        cursor.review_wait_budget = None
        cursor.login_review_cache = None
        if writer:
            writer.append("review/pass", {
                "milestone_id": "submit_login", "reason": "login_result_shared",
                "attempt_ref": str(prior.get("attempt_ref") or ""),
                "observation_hash": str(getattr(ctx, "decision_screenshot_hash", "") or ""),
                "verified_by": "confirm_login_state",
            })
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
