"""当前逻辑块执行器：按里程碑推进并消费实际结果证据。"""
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
    from mino_nexus.contract.execution.ui import ui_channel_from_ctx, ui_channel_label
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
    from mino_nexus.loop.navigation_policy import dispatch_pending
    navigation = dispatch_pending(proxy, ctx, cursor, turn_seq=turn_seq, writer=writer)
    if navigation is not None:
        return navigation
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
    if cap == "screenshot" and pending.get("key_ref") == "operation.observe":
        import hashlib
        from mino_nexus.loop.milestones import write_state
        observed = proxy.observe("screenshot", force_fresh=True)
        image = str(getattr(observed, "image_base64", "") or "")
        ok = bool(image and getattr(observed, "has_image", lambda: False)())
        if ok:
            pending["status"] = "pass"
            pending["evidence"] = "current_application_observation"
            pending["review_observation_ref"] = hashlib.sha256(image.encode()).hexdigest()
            write_state(cursor, state)
        if writer:
            writer.append("operation/observation", {"milestone_id": mid, "status": "pass" if ok else "unverified",
                "observation_ref": pending.get("review_observation_ref", ""), "channel": "app"})
        return {"ok": ok, "case_fail": not ok, "status": "pass" if ok else "unverified",
                "error_kind": "" if ok else "observation_missing", "capability_id": cap, "milestone_id": mid,
                "summary": "已采集当前应用画面，业务预期由 Check 验证" if ok else "未取得当前画面，观察步骤不能跳过"}
    from mino_nexus.loop.conditional_steps import gate
    conditional_result = gate(pending, state, cursor, ctx, proxy, turn_seq=turn_seq, writer=writer)
    if conditional_result is not None:
        return conditional_result
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
    from mino_nexus.catalog.skill_channel import phase_allows, system_device_dispatch

    phase = str(getattr(cursor, "phase", "") or "")
    if (
        phase in ("prep", "do", "check")
        and not is_program_caller(cap)
        and not system_device_dispatch(cap)
        and not phase_allows(cap, phase)
    ):
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

    if cap == "operation.check_box" and mid == "legal_consent":
        from mino_nexus.action_space.scheme import execution_scheme, structure_target_available, use_visual_fallback
        from mino_nexus.navigation.geometry.hierarchy_slots import node_flag
        from mino_nexus.loop.milestones import write_state
        from mino_nexus.contract.execution.ui import UiChannel, ui_channel_from_ctx
        from mino_nexus.loop.ui_consent import find_consent_control, tap_params_for_control

        nodes = [n for n in (getattr(ctx, "nav_hierarchy_nodes", None) or []) if isinstance(n, dict)]
        ctrl = find_consent_control(nodes)
        if ctrl is not None and not node_flag(ctrl, "checked"):
            # 可见的未勾协议是必需动作；optional 仅用于确证界面没有该控件。
            pending["optional"] = False
            write_state(cursor, state)
            if ui_channel_from_ctx(ctx) == UiChannel.ANDROID:
                from mino_nexus.loop.local_executors import _dispatch_device, _exec_ok
                from mino_nexus.loop.recovery import collect_evidence, invalidate_evidence_cache

                evidence = collect_evidence(ctx, proxy, target_package=str(getattr(ctx, "target_package", "") or ""))
                if evidence.ime_shown == "yes":
                    back = _dispatch_device(proxy, ctx=ctx, seq=turn_seq, cap="press_key",
                                            params={"key": "BACK"}, label="收起键盘后重新观察协议控件")
                    invalidate_evidence_cache(ctx)
                    ok = _exec_ok(back)
                    if writer:
                        writer.append("program/consent_observe", {"milestone_id": mid, "status": "defer" if ok else "blocked",
                                      "reason": "keyboard_dismissed" if ok else "keyboard_dismiss_failed", "fresh_observation_required": True})
                    return {"ok": False, "defer": ok, "case_fail": not ok, "status": "defer" if ok else "blocked",
                            "error_kind": "" if ok else "program_hook_failed", "capability_id": cap,
                            "milestone_id": mid, "step_id": mid, "block_id": block_id,
                            "summary": "键盘已收起；下一回合重新观察并处理协议，当前协议仍未完成。" if ok else str(back.error or back.summary)}
            params = tap_params_for_control(ctrl, nodes)
            if ctrl.get("resource_id"):
                params["target"] = {"resource_id": ctrl["resource_id"]}
            if ctrl.get("node_ref"):
                params["node_ref"] = ctrl["node_ref"]
                params["snapshot_id"] = ctrl.get("snapshot_id", "")
            checked_known = (node_flag(ctrl, "checkable") and ctrl.get("checked") is not None
                             and ("checked_known" not in ctrl or node_flag(ctrl, "checked_known")))
            if pending.get("requirement") != "conditional" and execution_scheme(ctx) == "dom" and (
                not checked_known or not structure_target_available("tap_element", params, ctx)
            ):
                pending["kind"] = "visual_tap"
                pending["device_cap"] = "tap_element"
                pending.pop("hook_cap", None)
                pending["converted_from_hook"] = cap
                pending["title"] = "勾选同意协议（点复选框本身并回看勾选状态，不点协议链接）"
                write_state(cursor, state)
                use_visual_fallback(ctx, reason="consent_target_or_checked_state_unavailable", writer=writer)
                if writer:
                    writer.append("milestone/consent_visual", {"milestone_id": mid, "reason": "no_structure_anchor",
                                  "execution_started": False})
                return None

    hook_params = dict(step.get("params") or {}) if step else {}
    hook_params.update(dict(pending.get("params") or {}))
    if cap == 'operation.check_box' and mid == 'legal_consent':
        if ctrl is None:
            return {'ok': False, 'case_fail': True, 'status': 'blocked', 'error_kind': 'consent_target_unavailable',
                    'capability_id': cap, 'milestone_id': mid, 'summary': '协议控件尚不可定位，不能猜测勾选状态'}
        hook_params.update(tap_params_for_control(ctrl, nodes))
        hook_params['checked'] = True
        if ctrl.get('resource_id'):
            hook_params['target'] = {'resource_id': ctrl['resource_id']}
        if ctrl.get('node_ref'):
            hook_params['node_ref'] = ctrl['node_ref']
            hook_params['snapshot_id'] = ctrl.get('snapshot_id', '')
        if not ctrl.get('resource_id') and not ctrl.get('node_ref'):
            hook_params['target'] = {key: ctrl.get(key) for key in ('bounds', 'class', 'text', 'content_desc')}
            hook_params['snapshot_id'] = str((getattr(ctx, 'observation_contract', None) or {}).get('snapshot_id') or '')
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
    if (
        cap == "confirm_login_state" and phase == "prep" and not ok
        and str(getattr(res, "error", "") or "") == "login_observation_unavailable"
        and str(pending.get("source") or "") == "prep_program"
    ):
        # 首页缺少身份线索不等于登录失败；保留程序核验，先取得可核验的界面。
        from mino_nexus.loop.milestones import write_state
        from mino_nexus.loop.prep_program_plan import schedule_prep_session_navigation, schedule_prep_reauthentication

        if schedule_prep_session_navigation(cursor, ctx, pending, writer=writer):
            return {
                "ok": False, "defer": True, "status": "wait", "capability_id": cap, "milestone_id": mid,
                "summary": "登录态未知，按应用配置只读取证；原前置仍待独立核验",
                "session_logged": "flow_block",
            }
        if schedule_prep_reauthentication(cursor, ctx, pending, writer=writer):
            return {
                "ok": False, "defer": True, "status": "wait", "capability_id": cap, "milestone_id": mid,
                "summary": "身份未知，按应用配置退出后重新登录；前置仍待本次账号核验",
                "session_logged": "flow_block",
            }

        observe_id = "prep_observe_session"
        rows = [dict(row) for row in state.get("milestones") or [] if isinstance(row, dict)]
        if not getattr(cursor, "prep_session_navigation_guard", None) and not any(str(row.get("id") or "") == observe_id for row in rows):
            for row in rows:
                if str(row.get("id") or "") == mid:
                    row["status"] = "pending"
            observe_row = {
                "id": observe_id, "kind": "visual_action", "status": "in_progress",
                "optional": False, "source": "prep_session_observation",
                "title": "导航到能确认当前登录态的被测应用界面；仅观察身份/登录入口，保持登录态",
                "expected_result": "到达能辨认当前登录态的界面，随后由登录态观察工具重新核验；不能猜测已登录",
            }
            index = next((i for i, row in enumerate(rows) if str(row.get("id") or "") == mid), 0)
            rows.insert(index, observe_row)
            state["milestones"] = rows
            state["status"] = "in_progress"
            state["exit_allowed"] = False
            write_state(cursor, state)
            if writer is not None:
                writer.append("prep/session_observe", {
                    "milestone_id": mid, "observation_milestone_id": observe_id,
                    "session": "unknown", "required_session": str(pending.get("required_session") or ""),
                    "status": "pending", "reason": "current_screen_has_no_session_evidence",
                })
            return {
                "ok": False, "defer": True, "status": "wait",
                "capability_id": cap, "milestone_id": mid,
                "summary": "当前屏缺少登录态证据，先导航观察；前置仍未完成，随后重新核验",
                "session_logged": "flow_block",
            }
        # 未配置、作用域不符或步数耗尽时明确阻断，不能把未知身份放行。
    if ok and cap == "confirm_login_state" and phase == "prep" and pending.get("source") == "prep_program":
        from mino_nexus.loop.prep_program_plan import finish_prep_reauthentication

        finish_prep_reauthentication(cursor)
        state = read_state(cursor)
    if ok and pending.get('requirement') == 'conditional':
        from mino_nexus.loop.milestones import write_state
        pending['condition_resolution'] = 'satisfied'
        from mino_nexus.services.step_requirements import checkbox_post_proof
        checked_observation = (res.raw_response if isinstance(res.raw_response, dict) else {}).get('checked_observation')
        proof = checkbox_post_proof(pending.get('condition_proof'), checked_observation)
        if proof is None:
            ok = False
            pending['condition_resolution'] = 'unknown'
            hook_summary = 'checkbox_post_state_proof_unavailable'
        else:
            pending['condition_proof'] = proof
        write_state(cursor, state)
    if ok:
        note_tool_pass_milestone(
            cursor,
            capability_id=cap,
            milestone_id=mid,
            writer=writer,
            tool_summary=hook_summary,
            ctx=ctx,
        )
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
    from mino_nexus.contract.execution.ui import UiChannel, ui_channel_from_ctx

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
    from mino_nexus.contract.execution.ui import UiChannel, ui_channel_from_ctx

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
    from mino_nexus.contract.execution.ui import UiChannel, ui_channel_from_ctx

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
    from mino_nexus.action_space.scheme import execution_scheme as action_scheme
    from mino_nexus.loop.milestone_orchestrator import in_progress_milestone

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


def _schedule_login_evidence_navigation(cursor, *, ctx=None, shot=None, block_id, writer=None,
                                        review_submit=False, inspection=None, restore=False):
    """有界只读导航；导航通过不代表登录、身份或业务通过。"""
    from mino_nexus.loop.milestones import write_state
    from mino_nexus.loop.login_evidence_navigation import NAV_LIMIT, RESTORE_LIMIT, remember_context, navigation_title
    state = read_state(cursor)
    rows = [dict(row) for row in state.get("milestones") or [] if isinstance(row, dict)]
    goal = next((row for row in rows if row.get("id") == "login_state"), None)
    counter = "login_context_restore_count" if restore else "login_evidence_navigation_count"
    count = int(getattr(cursor, counter, 0) or 0)
    limit = RESTORE_LIMIT if restore else NAV_LIMIT
    if goal is None or count >= limit or ctx is None or not remember_context(cursor, ctx, shot):
        return False
    if review_submit:
        submit = next((row for row in rows if row.get("id") == "submit_login"), None)
        if submit is None:
            return False
        submit.update(status="pass", evidence="form_left_login_unverified")
        cursor.visual_prior = None
        cursor.pending_visual_review = None
        cursor.review_wait_budget = None
        cursor.login_review_cache = None
    # 起点要求已经执行过，不能在身份取证时继续要求退出已登录账号。
    cursor.login_session_hint = "【登录取证】表单提交已结束；现在只核验当前登录账号和恢复业务上下文，禁止退出、换号或重填凭据。"
    goal["status"] = "pending"
    count += 1
    nav_id = f"login_context_restore_{count}" if restore else f"login_evidence_nav_{count}"
    from mino_nexus.services.login_evidence_policy import policy_for_context, submitted_account_enabled
    policy = policy_for_context(ctx) if not restore and submitted_account_enabled(ctx) else {}
    navigation_hint = policy.get("authenticated_content_hint") or policy.get("logout_navigation_hint", "")
    rows.insert(rows.index(goal), {"id": nav_id, "kind": "visual_action", "status": "in_progress",
        "optional": False, "source": "login_context_restore" if restore else "login_evidence_observation",
        "source_block": block_id, "exec_caps": ["tap_element", "swipe_direction", "press_key"],
        "title": navigation_title(cursor, inspection or {}, restore=restore),
        "navigation_target": navigation_hint,
        "expected_result": "本次只读导航引起可见页面变化；登录、身份及原上下文由随后程序观察核验"})
    state.update(milestones=rows, status="in_progress", exit_allowed=False)
    write_state(cursor, state)
    setattr(cursor, counter, count)
    if writer:
        writer.append("login/context_restore_navigation" if restore else "login/evidence_navigation",
            {"milestone_id": nav_id, "attempt": count, "limit": limit,
             "login_verified": bool(restore), "business_context": "unverified"})
    return True


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
    visual_fallback: bool = False,
) -> dict[str, Any]:
    """提交后只观察一次页面切换；不补证、不恢复页面、不重试。"""
    from mino_nexus.loop.login_transition import confirm_submission_transition

    result = confirm_submission_transition(proxy, ctx, cursor, shot=shot, writer=writer)
    if not result["ok"]:
        return {"ok": False, "case_fail": True, "status": result["status"],
                "error_kind": result["error_kind"], "summary": result["summary"],
                "capability_id": "confirm_login_state", "milestone_id": "login_state",
                "step_id": "login_state", "block_id": block_id, "session_logged": "flow_block"}
    return _complete_login_state(cursor, ctx, block_id=block_id, writer=writer,
                                 review_submit=review_submit)


def _complete_login_state(cursor, ctx, *, block_id, writer=None, frame_thumb="", review_submit=False):
    # This function completes a submission milestone, not an authenticated session.
    transition = getattr(cursor, 'login_transition_result', None) or {}
    reference = getattr(cursor, 'login_submit_reference', None) or {}
    from mino_nexus.services.login_evidence import scope
    result = transition.get('result') or {}
    if (reference.get('context_id') != id(ctx) or reference.get('scope') != scope(ctx)
            or not reference.get('attempt_ref')
            or transition.get('attempt_ref') != reference.get('attempt_ref')
            or result.get('ok') is not True or result.get('status') != 'completed'):
        return {'ok': False, 'case_fail': True, 'status': 'unverified',
                'error_kind': 'login_transition_observation_error',
                'capability_id': 'confirm_login_state', 'block_id': block_id,
                'summary': '缺少本次提交的页面切换证据，停止确认'}
    cursor.login_state_waits = 0
    from mino_nexus.loop.milestones import note_tool_pass_milestone
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
                "verified_by": "submission_transition",
                "business_context_verified": False,
            })
    note_tool_pass_milestone(
        cursor,
        capability_id="confirm_login_state",
        milestone_id="login_state",
        writer=writer,
        tool_summary="登录提交后已切换页面；后续业务预期由 check 独立核验",
        ctx=ctx,
    )
    cursor.login_navigation_reference = None
    cursor.login_navigation_proof = None
    cursor.login_evidence_navigation_count = 0
    cursor.login_context_restore_count = 0
    return {
        "ok": True,
        "status": "pass",
        "summary": "登录提交后已切换页面；后续业务预期由 check 独立核验",
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
