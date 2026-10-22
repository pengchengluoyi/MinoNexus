"""登录与导航的协同：什么时候该由程序链接管，什么时候该交回模型，什么时候整体停。

这一段的判断对象是"模型这一步想做的事"和"登录流当前处在哪"，两边的状态必须对齐：
链没走完就不能算登录成功，链走完了就不能再让模型重填表单，中断标记出现就要把决策权交回。

17 个出口覆盖三类结局：继续本轮 / 结束用例 / 整体退出。
唯一需要带出块外的状态是 `thumb`：链路可能顺手产生一张新缩略图，后续步骤要用它。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from mino_nexus.loop.execution.login_branch import _consume_login_chain, _login_chain_guard
from mino_nexus.loop.execution.page_ready import _progress_gate_web_kwargs
from mino_nexus.loop.execution.turn_log import _log_turn_end


@dataclass
class LoginFlow:
    cont: bool = False
    leave_case: bool = False
    leave: dict[str, Any] | None = None
    thumb: Any = None


def run_login_nav(
    *,
    ctx: Any,
    cursor: Any,
    cur: Any,
    case: Any,
    history: list[Any],
    proxy: Any,
    seq: int,
    writer: Any,
    shot: Any,
    thumb: Any,
    scout_run_id: str,
    target_pkg: str,
    case_seq: int,
    login_module_case: Any,
    screen_fp_turn: Any,
    allows_login_step: bool,
    cancel_check: Callable[[], bool] | None,
    required_execution_blocker: Callable[[], Any],
    rec: Callable[..., Any],
    emit: Callable[..., None],
    leave_blocked: Callable[..., dict[str, Any]],
    leave: Callable[..., dict[str, Any]],
) -> LoginFlow:
    from mino_nexus.contract.execution.ui import UiChannel, ui_channel_from_ctx
    from mino_nexus.loop.milestones import (
        ensure_milestones_before_decide,
        login_flow_under_milestones,
    )

    ensure_milestones_before_decide(
        cursor,
        ctx,
        app_id=str(getattr(ctx, "app_id", "") or ""),
        writer=writer,
    )
    blocked_step = required_execution_blocker()
    if blocked_step:
        return LoginFlow(leave=leave_blocked(
                        seq, str(blocked_step.get("hook_cap") or blocked_step.get("device_cap") or "program"),
                        f"{blocked_step.get('title') or blocked_step.get('id')}：{blocked_step.get('evidence')}",
                        str(blocked_step.get("error_kind") or "capability_unavailable"),
                    ))
    from mino_nexus.loop.flow_block_runner import (
        try_run_login_flow_macro,
        try_run_system_dialog_macro,
    )
    from mino_nexus.loop.login_flow_interrupt import login_overlay_blocks_flow
    from mino_nexus.loop.step_flow_scope import login_flow_allowed

    hist_lines = list(history[-24:])
    nodes = list(getattr(ctx, "nav_hierarchy_nodes", None) or [])
    from mino_nexus.loop.login_submit import (
        _otp_code_already_entered,
        run_login_otp_submit_chain,
        run_post_sms_login_pipeline,
    )
    from mino_nexus.loop.step_contract import instruction_allows_login_flow
    from mino_nexus.loop.step_flow_scope import login_flow_allowed

    _lf_ok, _lf_msg = login_flow_allowed(
        cursor=cursor,
        phase="do",
        instruction=str(cur.instruction or ""),
        expected=str(cur.expected or ""),
    )
    _under_login_ms = login_flow_under_milestones(cursor)
    from mino_nexus.loop.visual_review import has_visual_prior

    # An observed login form may be the pending action's expected result.
    # Review that action before any legacy login chain can take ownership.
    _login_review_pending = has_visual_prior(cursor)
    if _lf_ok and not _under_login_ms and not _login_review_pending and not getattr(cursor, "login_chain_broken", False):
        from mino_nexus.loop.login_account_lease import (
            login_surface_detected,
            try_lease_on_login_surface,
        )

        if login_surface_detected(ctx, nodes):
            _ok_lease, _lease_err, _lease_brief = try_lease_on_login_surface(
                ctx, case
            )
            if _ok_lease and _lease_brief:
                history.append(
                    f"{seq}. lease_account → pass: {_lease_brief[:160]}"
                )
            elif not _ok_lease and _lease_err:
                cursor.correction_hint = (
                    f"登录界面租号失败：{_lease_err}；"
                    "请检查号池或 signal_ask_human。"
                )
        _pending_sms = bool(getattr(cursor, "login_post_sms_pending", False))
        from mino_nexus.loop.login_submit import (
            _otp_code_already_entered,
            run_login_otp_submit_chain,
            run_post_sms_login_pipeline,
        )

        if _pending_sms:
            from mino_nexus.loop.login_submit import _sms_send_done, otp_fetch_allowed

            if not _sms_send_done(hist_lines, cursor.step_intents_done):
                cursor.login_post_sms_pending = False
                _pending_sms = False
            elif not otp_fetch_allowed(
                hist_lines, cursor.step_intents_done, ctx, cursor=cursor
            ):
                cursor.login_post_sms_pending = False
                _pending_sms = False
        if _pending_sms:
            chain = run_post_sms_login_pipeline(
                proxy,
                ctx,
                turn_seq=seq,
                instruction=str(cur.instruction or ""),
                intents_done=cursor.step_intents_done,
                history_lines=hist_lines,
                hierarchy_nodes=nodes,
                login_module_case=bool(login_module_case),
                retries=2,
            )
        else:
            from mino_nexus.loop.email_login_auto import run_email_login_ui_chain

            chain = run_email_login_ui_chain(
                proxy,
                ctx,
                turn_seq=seq,
                instruction=str(cur.instruction or ""),
                intents_done=cursor.step_intents_done,
                history_lines=hist_lines,
                login_module_case=bool(login_module_case),
            )
            if not chain:
                from mino_nexus.loop.email_login_auto import _login_kind as _email_kind

                if _email_kind(ctx) != "email":
                    chain = run_login_otp_submit_chain(
                        proxy,
                        ctx,
                        turn_seq=seq,
                        instruction=str(cur.instruction or ""),
                        intents_done=cursor.step_intents_done,
                        history_lines=hist_lines,
                        hierarchy_nodes=nodes,
                        login_module_case=bool(login_module_case),
                    )
        if chain:
            if len(chain) == 1 and str(chain[0].get("capability_id") or "") == "input_text":
                if chain[0].get("ok") and _otp_code_already_entered(
                    hist_lines, cursor.step_intents_done
                ):
                    chain = []
            if chain:
                if cancel_check and cancel_check():
                    return LoginFlow(leave=leave(status="cancelled", summary="任务已取消", reason_code="task_cancelled"))
                lc, ls = _consume_login_chain(
                    chain,
                    seq=seq,
                    cursor=cursor,
                    rec=rec,
                    emit=emit,
                    thumb=thumb,
                    screen_fp=screen_fp_turn,
                    login_flow_step=bool(
                        instruction_allows_login_flow(
                            str(cur.instruction or ""),
                            login_module_case=bool(login_module_case),
                        )
                    ),
                    fuse_web=_progress_gate_web_kwargs(ctx, proxy),
                    writer=writer,
                )
                chain_all_fail = not any(bool(x.get("ok")) for x in chain)
                pending_now = bool(
                    _pending_sms or getattr(cursor, "login_post_sms_pending", False)
                )
                if chain_all_fail and not pending_now:
                    cursor.login_chain_fail_streak = 0
                    cursor.correction_hint = (
                        "登录表单未就绪：请先按 instruction 点击右上角「登录」打开弹窗，"
                        "再由程序链填邮箱、发码、取码并点登录提交。"
                    )
                else:
                    fail_msg = _login_chain_guard(chain, cursor=cursor)
                    if fail_msg:
                        return LoginFlow(leave=leave(status="fail", summary=fail_msg, reason_code="login_program_incomplete"))
                if any(
                    str(x.get("step_id") or "") == "send_code"
                    and x.get("ok")
                    for x in chain
                ):
                    cursor.login_post_sms_pending = True
                    cursor.login_post_sms_stall = 0
                    cursor.step_intents_done.add("sms_send")
                if pending_now or getattr(cursor, "login_post_sms_pending", False):
                    if _otp_code_already_entered(
                        list(history[-24:]), cursor.step_intents_done
                    ):
                        cursor.login_post_sms_pending = False
                        cursor.login_post_sms_stall = 0
                    else:
                        cursor.login_post_sms_stall = int(
                            getattr(cursor, "login_post_sms_stall", 0) or 0
                        ) + 1
                        if cursor.login_post_sms_stall >= 5:
                            cursor.login_post_sms_pending = False
                if chain_all_fail and not pending_now:
                    head = str(
                        (chain[0].get("summary") if chain else "")
                        or (chain[0].get("capability_id") if chain else "")
                        or "login_chain"
                    )
                    cursor.correction_hint = head[:280]
                    _log_turn_end(
                        writer,
                        cap=str(chain[0].get("capability_id") or "login_chain"),
                        status="fail",
                    )
                    return LoginFlow(cont=True)
                else:
                    _log_turn_end(writer, cap=lc or "login_chain", status=ls)
                    return LoginFlow(cont=True)
        elif _pending_sms:
            cursor.login_post_sms_stall = int(getattr(cursor, "login_post_sms_stall", 0) or 0) + 1
            if cursor.login_post_sms_stall >= 5:
                cursor.login_post_sms_pending = False
            cursor.correction_hint = (
                "发码后程序链未填上验证码：请稍候或检查测试环境 otp.fixed；"
                "勿重复 get_otp 空转。"
            )
            _log_turn_end(writer, cap="login_chain", status="wait")
            return LoginFlow(cont=True)
    macro = None
    from mino_nexus.loop.recovery_permission import permission_choice_dialog_present

    from mino_nexus.loop.flow_block_executor import try_review_login_submission

    macro = try_review_login_submission(proxy, ctx, cursor, shot=shot, writer=writer)
    if not macro and permission_choice_dialog_present(nodes):
        macro = try_run_system_dialog_macro(
            proxy,
            ctx,
            turn_seq=seq,
            target_package=str(getattr(ctx, "target_package", "") or target_pkg),
        )
    if not macro:
        from mino_nexus.loop.flow_block_executor import try_dispatch_block_hook
        from mino_nexus.loop.milestones import first_pending_hook_milestone, read_state

        if first_pending_hook_milestone(read_state(cursor)):
            hook_macro = try_dispatch_block_hook(
                proxy,
                ctx,
                cursor,
                run_id=scout_run_id,
                turn_seq=seq,
                app_id=str(getattr(ctx, "app_id", "") or ""),
                history_lines=hist_lines,
                writer=writer,
            )
            if hook_macro:
                macro = hook_macro
    if not macro:
        macro = try_run_system_dialog_macro(
            proxy,
            ctx,
            turn_seq=seq,
            target_package=str(getattr(ctx, "target_package", "") or target_pkg),
        )
    if not macro and not has_visual_prior(cursor):
        from mino_nexus.loop.nav_session_fork import (
            login_flow_interrupt_allowed_for_step,
            required_session_from_scene,
        )

        _req_sess_macro = required_session_from_scene(
            dict(getattr(ctx, "case_scene", None) or {})
        )
        overlay_login = login_overlay_blocks_flow(
            hierarchy_nodes=nodes,
            instruction=str(cur.instruction or ""),
            login_module_case=bool(login_module_case),
            cursor=cursor,
            phase="do",
            expected=str(cur.expected or ""),
        )
        if overlay_login and not login_flow_interrupt_allowed_for_step(
            instruction=str(cur.instruction or ""),
            expected=str(cur.expected or ""),
            required_session=_req_sess_macro,
        ):
            overlay_login = False
        from mino_nexus.loop.milestones import login_flow_under_milestones

        _login_owned = login_flow_under_milestones(cursor)
        if overlay_login and not _login_owned:
            ctx.login_flow_interrupt = True
            macro = try_run_login_flow_macro(
                proxy,
                ctx,
                run_id=scout_run_id,
                case_seq=case_seq,
                turn_seq=seq,
                app_id=str(getattr(ctx, "app_id", "") or ""),
                hierarchy_nodes=nodes,
                history_lines=hist_lines,
                interrupt=True,
            )
        else:
            ctx.login_flow_interrupt = False
            if (
                not _login_owned
                and _lf_ok
                and allows_login_step
                and not cursor.sms_auto_attempted
            ):
                macro = try_run_login_flow_macro(
                    proxy,
                    ctx,
                    run_id=scout_run_id,
                    case_seq=case_seq,
                    turn_seq=seq,
                    app_id=str(getattr(ctx, "app_id", "") or ""),
                    hierarchy_nodes=nodes,
                    history_lines=hist_lines,
                    interrupt=False,
                )
    if (
        macro
        and str(macro.get("block_id") or "") == "fb.global.system_dialog"
        and not macro.get("ok")
    ):
        macro = None
    from mino_nexus.loop.program_device_fallback import (
        activate_program_vision_fallback,
        is_vision_fallback,
        note_vision_fallback_streak,
    )

    if macro and is_vision_fallback(macro):
        activate_program_vision_fallback(cursor, writer, macro)
        fb_n = note_vision_fallback_streak(cursor, macro)
        macro = None
        if fb_n >= 3:
            last_visual = getattr(cursor, "vision_fallback_last_execution", None)
            if isinstance(last_visual, dict) and last_visual.get("has_coordinates"):
                fb_sum = (
                    "程序定位连续 3 次未命中；视觉动作已提供坐标，"
                    f"最近设备执行状态 {last_visual.get('status') or '未知'}。"
                )
            else:
                fb_sum = "程序定位连续 3 次未命中；看图兜底未能完成这一步，已停止。"
            rec(
                seq,
                capability_id="input_text",
                status="fail",
                summary=fb_sum,
                thought=fb_sum,
                thumb=thumb,
            )
            emit(
                "result",
                thought=fb_sum,
                step=seq,
                capability_id="input_text",
                status="fail",
                result_status="fail",
                summary=fb_sum,
                thumb=thumb,
            )
            return LoginFlow(leave=leave(status="fail", summary=fb_sum, reason_code="program_vision_fallback_exhausted"))
    if macro and macro.get("thumb"):
        thumb = str(macro.get("thumb") or thumb)
    if macro and macro.get("case_fail"):
        if macro.get("status") in {"product_fail", "unverified", "env_blocked"}:
            reason = str(macro.get("error_kind") or "login_transition_observation_error")
            writer.append("login/completion_blocked", {"reason_code": reason, "status": macro["status"]})
            return LoginFlow(leave=leave(status=macro["status"], summary=str(macro.get("summary") or reason), reason_code=reason))
        fail_summary = str(macro.get("summary") or "登录未成功")
        if macro.get("status") == "blocked":
            return LoginFlow(leave=leave_blocked(
                            seq, str(macro.get("capability_id") or "program"), fail_summary,
                            str(macro.get("error_kind") or "program_hook_failed"),
                        ))
        rec(
            seq,
            capability_id=str(macro.get("capability_id") or "confirm_login_state"),
            status="fail",
            summary=fail_summary[:400],
            thought=fail_summary[:300],
            thumb=thumb,
        )
        emit(
            "result",
            thought=fail_summary[:300],
            step=seq,
            capability_id=str(macro.get("capability_id") or "confirm_login_state"),
            status="fail",
            result_status="fail",
            summary=fail_summary[:400],
            thumb=thumb,
        )
        return LoginFlow(leave=leave(status="fail", summary=fail_summary, reason_code=str(macro.get("error_kind") or "program_hook_failed")))
    if macro and macro.get("defer"):
        wait_summary = str(macro.get("summary") or "下一回合再看")
        rec(
            seq,
            capability_id=str(macro.get("capability_id") or "confirm_login_state"),
            status="wait",
            summary=wait_summary[:400],
            thought=wait_summary[:300],
            thumb=thumb,
        )
        emit(
            "result",
            thought=wait_summary[:300],
            step=seq,
            capability_id=str(macro.get("capability_id") or "confirm_login_state"),
            status="wait",
            result_status="wait",
            summary=wait_summary[:400],
            thumb=thumb,
        )
        _log_turn_end(writer, cap=str(macro.get("capability_id") or "confirm_login_state"), status="wait")
        return LoginFlow(cont=True)
    if macro:
        bid = str(macro.get("block_id") or "")
        login_block = bid == "fb.global.login" or bid.startswith("fb.global.login.")
        if not login_block or macro.get("ok"):
            cursor.sms_auto_attempted = True
        cap_auto = str(macro.get("capability_id") or "login_flow_macro")
        sum_auto = str(macro.get("summary") or "")
        st_auto = "pass" if macro.get("ok") else "fail"
        if macro.get("ok"):
            st_auto = str(macro.get("status") or "pass")
            if st_auto not in ("pass", "done"):
                st_auto = "pass"
        if macro.get("ok"):
            from mino_nexus.loop.milestones import otp_fill_device_complete

            if macro and macro.get("ok"):
                if cap_auto != "input_text" or otp_fill_device_complete(cursor, hist_lines):
                    cursor.record_step_op(cap_auto)
                from mino_nexus.loop.channel_observation import bind_fresh_observation
                from mino_nexus.loop.milestones import note_tool_pass_milestone
                from mino_nexus.contract.execution.ui import UiChannel, ui_channel_from_ctx

                if cap_auto == "input_text" and ui_channel_from_ctx(ctx) == UiChannel.WEB:
                    bind_fresh_observation(ctx, proxy)
                note_tool_pass_milestone(
                    cursor,
                    capability_id=cap_auto,
                    milestone_id=str(
                        macro.get("milestone_id") or macro.get("step_id") or ""
                    ),
                    writer=writer,
                    tool_summary=sum_auto,
                    ctx=ctx,
                )
                if login_block:
                    from mino_nexus.loop.flow_block_runner import maybe_emit_login_flow_complete

                    maybe_emit_login_flow_complete(
                        ctx,
                        app_id=str(getattr(ctx, "app_id", "") or ""),
                        history_lines=hist_lines,
                    )
        if macro:
            from mino_nexus.loop.observe.program_tool_log import log_program_tool

            if str(macro.get("session_logged") or "") != "flow_block":
                log_program_tool(
                    writer,
                    capability_id=cap_auto,
                    params=dict(macro.get("params") or {}),
                    status=st_auto,
                    summary=sum_auto,
                    executor_used="internal",
                    source="flow_block_macro",
                )
            rec(
                seq,
                capability_id=cap_auto,
                status=st_auto,
                summary=sum_auto,
                thought=sum_auto,
                thumb=thumb,
                executor_used="internal",
            )
            emit(
                "result",
                thought=sum_auto,
                step=seq,
                capability_id=cap_auto,
                status=st_auto,
                summary=sum_auto,
                thumb=thumb,
            )
            _log_turn_end(writer, cap=cap_auto, status=st_auto)
            if macro.get("ok"):
                from mino_nexus.loop.step_phase_fsm import try_transition_after_tool_pass

                fsm_tr = try_transition_after_tool_pass(
                    cursor,
                    ctx,
                    writer=writer,
                    in_prep=cursor.phase == "prep",
                    in_check=cursor.phase == "check",
                )
                if fsm_tr == "check_to_case_done":
                    return LoginFlow(leave_case=True)
                return LoginFlow(cont=True)
        _fill_mid = str(macro.get("milestone_id") or macro.get("step_id") or "") if macro else ""
        _otp_hook_fail = bool(macro and login_block and _fill_mid == "otp_fill")
        _email_hook_fail = bool(macro and login_block and _fill_mid == "account_fill" and not macro.get("ok"))
        if macro and (
            _otp_hook_fail
            or _email_hook_fail
            or (
                cap_auto == "input_text"
                and "sms_code" in sum_auto
                and not macro.get("ok")
            )
        ):
            fuse_params = dict(macro.get("params") or {})
            if not fuse_params:
                fuse_params = {
                    "field": "email" if _email_hook_fail else "sms_code",
                }
            gate = getattr(cursor, "progress_gate", None)
            if gate is not None:
                gate.record_fail(cap_id=cap_auto, params=fuse_params)
                from mino_nexus.contract.execution.ui import UiChannel, ui_channel_from_ctx

                fuse_reason = gate.check(
                    phase=str(getattr(cursor, "phase", "") or "do"),
                    cap_id=cap_auto,
                    params=fuse_params,
                    screen_fp="",
                    login_flow_step=True,
                    web_channel=ui_channel_from_ctx(ctx) == UiChannel.WEB,
                    intents_done=set(getattr(cursor, "step_intents_done", None) or set()),
                )
                if fuse_reason:
                    return LoginFlow(leave=leave(status="fail", summary=str(fuse_reason)[:400], reason_code="action_fuse_exhausted"))
            if _email_hook_fail and "焦点未确认" in sum_auto:
                ctx.web_email_tap_milli = None
            if _email_hook_fail:
                cursor.correction_hint = (
                    sum_auto[:280] or "账号须填在上一次点击账号框的落点。"
                )
            else:
                cursor.correction_hint = (
                    sum_auto[:280]
                    or "程序填验证码失败：请先点验证码输入框，再 input_text(field=sms_code)。"
                )
            return LoginFlow(cont=True)
        if macro and not macro.get("ok"):
            # 所有程序宏失败都进入同一进展门禁；不是只有验证码/邮箱输入才计失败。
            # defer/视觉兜底已经在前面分别处理，剩余失败不得每轮无预算地重派。
            gate = getattr(cursor, "progress_gate", None)
            if gate is not None:
                failed_params = dict(macro.get("params") or {})
                gate.record_fail(cap_id=cap_auto, params=failed_params)
                fuse_reason = gate.check(
                    phase=str(getattr(cursor, "phase", "") or "do"),
                    cap_id=cap_auto, params=failed_params, screen_fp="",
                    web_channel=_progress_gate_web_kwargs(ctx, proxy).get("web_channel", False),
                    milestone_id=str(macro.get("milestone_id") or macro.get("step_id") or ""),
                )
                if fuse_reason:
                    return LoginFlow(leave=leave_blocked(
                                    seq, cap_auto, f"程序动作连续失败：{sum_auto}。{fuse_reason}"[:500],
                                    "program_macro_no_progress",
                                ))
            return LoginFlow(cont=True)
    return LoginFlow(thumb=thumb)
