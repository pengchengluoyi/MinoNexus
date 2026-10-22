"""守卫否决之后：不派发原动作，但也不等于什么都不做。

这一段是"拒绝"之后的全部善后：记一次 skip、尝试视觉补救、登录链续跑、
导航记账、止损计数、必要时整体停下。8 个出口全部在**任何设备动作之前**。

`guard_vision_remediate` 是本段唯一的输出：如果视觉补救给出了新决策，
主循环会把它当成下一轮的决策继续执行，而不是丢掉这一回合。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from mino_nexus.core.protocol import EventStatus
from mino_nexus.loop.execution.login_branch import _consume_login_chain, _login_chain_guard
from mino_nexus.loop.execution.page_ready import _progress_gate_web_kwargs
from mino_nexus.loop.execution.turn_log import _log_turn_end
from mino_nexus.loop.step_pointer import StepCursor


@dataclass
class SkipFlow:
    cont: bool = False
    leave_case: bool = False
    leave: dict[str, Any] | None = None
    remediate: Any = None


def run_guard_skip(
    *,
    ctx: Any,
    cursor: Any,
    cur: Any,
    case: Any,
    guard_ctx: dict[str, Any],
    guard_verdict: Any,
    phase_tool_kinds: Any,
    history: list[Any],
    inspect_slots: Any,
    login_module_case: Any,
    steps: Any,
    proxy: Any,
    cap_id: str,
    seq: int,
    thought: str,
    thumb: Any,
    screen_fp: Any,
    shot: Any,
    scout_run_id: str,
    provider_id: str,
    writer: Any,
    rec: Callable[..., Any],
    emit: Callable[..., None],
    leave: Callable[..., dict[str, Any]],
) -> SkipFlow:
    from mino_nexus.loop.milestones import login_block_id_for_ctx, read_state
    guard_vision_remediate: Any = None
    skip_cap = guard_verdict.code or cap_id
    if writer:
        from mino_nexus.loop.observe.ops_guard_diag import enrich_guard_block_payload, note_guard_block

        block_payload = enrich_guard_block_payload(
            {
                "capability_id": cap_id,
                "reason": skip_reason,
                "rewritten_cap": skip_cap,
                "dispatch_gate_code": skip_cap,
                "guard_id": guard_verdict.guard_id,
            },
            guard_ctx=guard_ctx,
            cur=cur,
            probe_diag=dict(getattr(ctx, "_last_probe_diag", None) or {}),
        )
        writer.append("guard/block", block_payload)
        if isinstance(cursor, StepCursor):
            note_guard_block(cursor, block_payload)
    if isinstance(cursor, StepCursor):
        from mino_nexus.catalog.guard_recovery_seed import recovery_text

        hint = recovery_text(str(guard_verdict.guard_id or ""), skip_reason)
        if hint:
            cursor.correction_hint = hint
            skip_reason = hint
    rec(seq, capability_id=skip_cap, status="skipped", summary=skip_reason, thought=thought, thumb=thumb)
    emit(
        "result",
        thought=thought,
        step=seq,
        capability_id=skip_cap,
        status="skipped",
        result_status="skipped",
        summary=skip_reason,
        thumb=thumb,
        device_dispatched=False,
        execution_kind="guard_skip",
    )
    stop_msg = None
    if skip_cap == "action_fuse":
        stop_msg = cursor.progress_gate.record_fuse_block(skip_reason)
    elif str(getattr(guard_verdict, "guard_id", "") or "").startswith("skip_repeat_"):
        gid = str(guard_verdict.guard_id or "")
        n_block = int((getattr(cursor, "guard_block_counts", None) or {}).get(gid) or 0)
        if n_block >= 3:
            stop_msg = (
                f"连续 {n_block} 次被 {gid} 拒绝仍重复同一动作，已停止。"
                f" {str(skip_reason or '')[:160]}"
            )
    elif skip_cap in ("limit_recovery_retry", "block_back_without_nav_back_semantics"):
        ctx.recovery_allow_back = True
        if isinstance(cursor, StepCursor):
            cursor.correction_hint = skip_reason
            if skip_cap == "limit_recovery_retry":
                n_block = cursor.bump_recovery_block()
                if n_block >= 3:
                    stop_msg = (
                        f"连续 {n_block} 次同类恢复被拒绝后仍重复尝试，判定陷入死循环。"
                        f"{skip_reason}"
                    )
    elif skip_cap in (
        "block_repeat_get_otp_when_ready",
        "flow_block:otp_ready_need_input",
    ) and cursor.phase in ("prep", "do"):
        cursor.correction_hint = (
            f"{skip_reason} "
            "请 input_text(field=sms_code) 或先 tap「6-Digit Code」；勿再 get_otp。"
        )
        cursor.otp_ready_guard_streak = (
            int(getattr(cursor, "otp_ready_guard_streak", 0) or 0) + 1
        )
        from mino_nexus.loop.milestones import login_block_id_for_ctx, read_state

        _st_fb = read_state(cursor)
        _bid_fb = str((_st_fb.get("block_ref") or {}).get("block_id") or "")
        if not _bid_fb:
            _bid_fb = login_block_id_for_ctx(ctx)
        from mino_nexus.loop.flow_block_executor import _dispatch_otp_fill
        from mino_nexus.contract.execution.ui import UiChannel, ui_channel_from_ctx

        _fill_otp = None
        _remediate_fill = True
        if ui_channel_from_ctx(ctx) == UiChannel.WEB:
            from mino_nexus.loop.web.web_progress import (
                refresh_web_focus,
                web_editable_focus_ready,
            )

            refresh_web_focus(ctx, proxy)
            _remediate_fill = web_editable_focus_ready(ctx)
        if _remediate_fill:
            _fill_otp = _dispatch_otp_fill(
                proxy,
                ctx,
                cursor,
                turn_seq=seq,
                block_id=_bid_fb,
                mid="otp_fill",
                writer=writer,
            )
        else:
            cursor.correction_hint = (
                f"{skip_reason} "
                "焦点不在验证码框：请先 tap「6-Digit Code」再 input_text；勿再 get_otp。"
            )
        from mino_nexus.loop.program_device_fallback import (
            activate_program_vision_fallback,
            is_vision_fallback,
            merge_program_vision_fallback_slots,
        )

        if _fill_otp and is_vision_fallback(_fill_otp):
            activate_program_vision_fallback(cursor, writer, _fill_otp)
            merge_program_vision_fallback_slots(cursor, inspect_slots)
            from mino_nexus.loop.vision_exec import run_vision_exec_turn, execution_phase_active

            if execution_phase_active():
                guard_vision_remediate = run_vision_exec_turn(
                    cursor=cursor,
                    ctx=ctx,
                    writer=writer,
                    shot=shot,
                    steps=steps,
                    inspect_slots=inspect_slots,
                    provider_id=str(provider_id or ""),
                    phase_tool_kinds=phase_tool_kinds or None,
                    send_image=True,
                )
                skip_reason = None
        elif _fill_otp:
            _fcap = str(_fill_otp.get("capability_id") or "input_text")
            _fst = "pass" if _fill_otp.get("ok") else "fail"
            _fsum = str(_fill_otp.get("summary") or "")[:400]
            rec(
                seq,
                capability_id=_fcap,
                status=_fst,
                summary=_fsum,
                thought=_fsum,
                thumb=thumb,
            )
            emit(
                "result",
                thought=_fsum,
                step=seq,
                capability_id=_fcap,
                status=_fst,
                summary=_fsum,
                thumb=thumb,
            )
            _log_turn_end(writer, cap=_fcap, status=_fst)
            return SkipFlow(cont=True)
        if cursor.otp_ready_guard_streak >= 4:
            stop_msg = (
                f"otp_ready 后已连续 {cursor.otp_ready_guard_streak} 次误选 get_otp"
                "且程序填码仍失败；请人工聚焦验证码框或 signal_give_up。"
            )
    elif skip_cap.removeprefix("flow_block:") == "require_sms_send_before_otp":
        from mino_nexus.loop.login_verification import is_web_email_login

        extra = (
            "请先用 tap_element 点击 Send 向邮箱发验证码，确认发码成功后再 get_otp。"
            if is_web_email_login(ctx)
            else "请先用 tap_element 点击 Send/发送验证码，确认发码成功后再 get_otp。"
        )
        cursor.correction_hint = f"{skip_reason} {extra}"
        cursor.sms_otp_guard_streak = (
            int(getattr(cursor, "sms_otp_guard_streak", 0) or 0) + 1
        )
        if cursor.sms_otp_guard_streak >= 5:
            stop_msg = (
                f"连续 {cursor.sms_otp_guard_streak} 次在发码前尝试 get_otp。"
                "请点 Send/发送验证码或 signal_give_up。"
                f" 最近：{skip_reason[:120]}"
            )
        elif cursor.sms_otp_guard_streak >= 3:
            stop_msg = cursor.progress_gate.record_fuse_block(
                f"【熔断·守卫】{skip_reason}"
            )
    elif (
        cursor.phase == "prep"
        and isinstance(cursor, StepCursor)
        and skip_cap in (
            "block_prep_guest_mine_tab",
            "skip_repeat_clear_app_cache",
        )
    ):
        cursor.prep_guard_streak = int(getattr(cursor, "prep_guard_streak", 0) or 0) + 1
        cursor.correction_hint = skip_reason
        if cursor.prep_guard_streak >= 6:
            stop_msg = (
                f"前置空转 {cursor.prep_guard_streak} 轮（仍为 logged_in 或无法 logout）。"
                "请确认应用退出登录路径可用，或将设备登出并重新观察后再跑；清缓存不等于退出登录。"
                f" 最近拦截：{skip_reason[:160]}"
            )
    elif (
        skip_cap in ("block_repeat_email_tab", "flow_block:block_repeat_email_tab")
        and isinstance(cursor, StepCursor)
        and cur
        and cursor.phase in ("prep", "do")
    ):
        cursor.email_tab_guard_streak = (
            int(getattr(cursor, "email_tab_guard_streak", 0) or 0) + 1
        )
        cursor.correction_hint = skip_reason
        from mino_nexus.loop.step_flow_scope import login_flow_allowed

        _lf_em_ok, _ = login_flow_allowed(
            cursor=cursor,
            phase="do",
            instruction=str(cur.instruction or ""),
            expected=str(cur.expected or ""),
        )
        if _lf_em_ok and not getattr(cursor, "login_chain_broken", False):
            from mino_nexus.loop.login_account_lease import try_lease_on_login_surface
            from mino_nexus.loop.milestones import login_flow_under_milestones

            try_lease_on_login_surface(ctx, case)
            hist_em = list(history[-24:])
            chain_em: list[dict[str, Any]] = []
            if login_flow_under_milestones(cursor):
                from mino_nexus.loop.flow_block_executor import try_dispatch_block_hook

                hook = try_dispatch_block_hook(
                    proxy,
                    ctx,
                    cursor,
                    run_id=scout_run_id,
                    turn_seq=seq,
                    app_id=str(getattr(ctx, "app_id", "") or ""),
                    history_lines=hist_em,
                    writer=writer,
                )
                if hook:
                    chain_em = [dict(hook)]
            else:
                from mino_nexus.loop.email_login_auto import run_email_login_ui_chain

                chain_em = run_email_login_ui_chain(
                    proxy,
                    ctx,
                    turn_seq=seq,
                    instruction=str(cur.instruction or ""),
                    intents_done=cursor.step_intents_done,
                    history_lines=hist_em,
                    login_module_case=bool(login_module_case),
                )
            if chain_em:
                fail_msg = _login_chain_guard(chain_em, cursor=cursor)
                if fail_msg:
                    return SkipFlow(leave=leave(status="fail", summary=fail_msg, reason_code="login_program_incomplete"))
                _consume_login_chain(
                    chain_em,
                    seq=seq,
                    cursor=cursor,
                    rec=rec,
                    emit=emit,
                    thumb=thumb,
                    screen_fp=str(screen_fp or ""),
                    login_flow_step=True,
                    fuse_web=_progress_gate_web_kwargs(ctx, proxy),
                    writer=writer,
                )
                cursor.email_tab_guard_streak = 0
                if writer:
                    writer.append(
                        "turn/end",
                        {
                            "decision_cap": "email_login_chain",
                            "decision_status": "pass",
                            "guard_remediate": True,
                        },
                    )
                return SkipFlow(cont=True)
            from mino_nexus.resources.account_credential_text import lease_email_address
            from mino_nexus.loop.device_execute_params import enrich_web_input_text_params
            from mino_nexus.loop.local_executors import _dispatch_device

            try:
                from mino_nexus.navigation.geometry.hierarchy_slots import capture

                _snap_em = capture(proxy, turn_id=int(seq or 0))
                if _snap_em.ok and _snap_em.nodes:
                    ctx.nav_hierarchy_nodes = [n for n in _snap_em.nodes if isinstance(n, dict)]
            except Exception:
                pass
            _acc = dict(getattr(ctx, "picked_account", None) or {})
            _em = lease_email_address(_acc)
            if _em:
                _p = enrich_web_input_text_params(
                    {"field": "email", "text": _em}, ctx
                )
                if _p.get("x") is not None and _p.get("y") is not None:
                    _res = _dispatch_device(
                        proxy,
                        ctx=ctx,
                        seq=seq,
                        cap="input_text",
                        params=_p,
                        label="守卫：Email 已选，程序填租号邮箱",
                    )
                    _st = (
                        _res.status.value
                        if hasattr(_res.status, "value")
                        else str(_res.status)
                    )
                    _ok = str(_st) in ("pass", EventStatus.PASS.value)
                    _item = {
                        "ok": _ok,
                        "status": _st,
                        "summary": str(_res.summary or _res.error or "input_text"),
                        "capability_id": "input_text",
                        "params": _p,
                    }
                    _consume_login_chain(
                        [_item],
                        seq=seq,
                        cursor=cursor,
                        rec=rec,
                        emit=emit,
                        thumb=thumb,
                        screen_fp=str(screen_fp or ""),
                        login_flow_step=True,
                        fuse_web=_progress_gate_web_kwargs(ctx, proxy),
                        writer=writer,
                    )
                    if _ok:
                        cursor.email_tab_guard_streak = 0
                        cursor.step_intents_done.add("login_email")
                        if writer:
                            writer.append(
                                "turn/end",
                                {
                                    "decision_cap": "input_text",
                                    "decision_status": "pass",
                                    "guard_remediate": True,
                                },
                            )
                        return SkipFlow(cont=True)
        if int(getattr(cursor, "email_tab_guard_streak", 0) or 0) >= 2:
            stop_msg = cursor.progress_gate.record_fuse_block(
                "【熔断·守卫】Email 标签已选过仍重复点击；"
                "程序填邮箱失败时请 signal_ask_human。"
            )
    if writer:
        writer.append(
            "turn/end",
            {"decision_cap": skip_cap, "decision_status": "skipped", "guard": True},
        )
    if isinstance(cursor, StepCursor) and guard_vision_remediate is None:
        from mino_nexus.loop.milestones import read_state

        passed = tuple(sorted(
            str(row.get("id") or "")
            for row in (read_state(cursor).get("milestones") or [])
            if isinstance(row, dict) and str(row.get("status") or "") == "pass"
        ))
        guard_stop = cursor.progress_gate.record_guard_rejection(
            case_step=int(getattr(cur, "n", 0) or 0),
            capability_id=cap_id,
            guard_id=str(guard_verdict.guard_id or skip_cap),
            passed_milestones=passed,
            reason=str(guard_verdict.reason or skip_reason),
        )
        if guard_stop:
            if writer:
                writer.append("progress/stop", {
                    "reason": guard_stop,
                    "guard_id": str(guard_verdict.guard_id or skip_cap),
                    "cause": str(guard_verdict.reason or skip_reason)[:300],
                })
            return SkipFlow(leave=leave(status="fail", summary=guard_stop, reason_code="action_guard_exhausted"))
    if writer:
        if stop_msg:
            writer.append(
                "progress/stop",
                {
                    "reason": stop_msg,
                    "fuse_block_streak": cursor.progress_gate.fuse_block_streak,
                },
            )
    if stop_msg:
        from mino_nexus.loop.fuse.interrupt_stack import try_push_on_fuse_block

        if try_push_on_fuse_block(cursor, ctx, writer=writer, message=str(stop_msg)[:240]):
            return SkipFlow(cont=True)
        return SkipFlow(leave=leave(status="fail", summary=stop_msg, reason_code="target_resolution_failed"))
    if guard_vision_remediate is None:
        return SkipFlow(cont=True)
    return SkipFlow(remediate=guard_vision_remediate)
