"""点完登录/发码/邮箱相关控件之后，登录程序链的处置。

三条分支（发码、登录提交、邮箱登录）都在改 cursor 的登录状态，
任何一条走完程序链都要结束本轮——所以返回 `TapFlow`：
`leave` 非空 → 调用方 `return`；`cont` 为真 → 调用方 `continue`。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

from mino_nexus.action_space.scheme import execution_scheme as action_scheme
from mino_nexus.navigation.login.transition import update_auth_state
from mino_nexus.loop.email_login_auto import run_email_login_ui_chain
from mino_nexus.loop.execution.login_branch import _consume_login_chain, _login_chain_guard
from mino_nexus.loop.execution.page_ready import _progress_gate_web_kwargs
from mino_nexus.loop.execution.turn_log import _log_turn_end
from mino_nexus.loop.login_account_lease import try_lease_on_login_surface
from mino_nexus.loop.login_submit import (
    account_session_logged_in,
    login_submit_tap_matches,
    sms_send_tap_matches,
)
from mino_nexus.loop.login_verification import is_web_email_login, record_verification_send
from mino_nexus.loop.step_contract import instruction_allows_login_flow
from mino_nexus.loop.step_flow_scope import login_flow_allowed
from mino_nexus.loop.step_pointer import StepCursor


@dataclass
class TapFlow:
    cont: bool = False
    leave: dict[str, Any] | None = None


def handle_login_tap(
    *,
    ctx: Any,
    proxy: Any,
    cursor: Any,
    cur: Any,
    case: dict[str, Any],
    cap_id: str,
    params: Any,
    status_val: str,
    summary: Any,
    post_fp: Any,
    screen_fp: Any,
    history: list[str],
    seq: int,
    writer: Any,
    thumb: Any,
    login_module_case: bool,
    rec: Callable[..., Any],
    emit: Callable[..., None],
    leave: Callable[..., dict[str, Any]],
) -> TapFlow:
    if (
        status_val == "pass"
        and cap_id == "tap_element"
        and isinstance(cursor, StepCursor)
        and cursor.phase == "do"
        and cur
    ):
        _tap_sel = str(
            (params or {}).get("selector_text")
            or (params or {}).get("text")
            or (params or {}).get("content_desc")
            or summary
            or ""
        )
        from mino_nexus.loop.login_submit import (
            account_session_logged_in,
            login_submit_tap_matches,
            sms_send_tap_matches,
        )

        send_just = bool(getattr(cursor, "send_code_tap_passed", False))
        setattr(cursor, "send_code_tap_passed", False)

        if sms_send_tap_matches(_tap_sel):
            from mino_nexus.loop.login_verification import (
                is_web_email_login,
                record_verification_send,
            )

            from mino_nexus.action_space.scheme import execution_scheme as action_scheme

            pending_send_review = action_scheme(ctx) == "visual"
            if pending_send_review:
                cursor.correction_hint = "已点击发送；须先回看确认本次发码被接受，才能取码。"
            else:
                record_verification_send(ctx)
                cursor.step_intents_done.add("sms_send")
                cursor.login_post_sms_pending = True
                cursor.login_post_sms_stall = 0
                update_auth_state(ctx, login_auto_submit_attempts=0)
            if not pending_send_review and is_web_email_login(ctx):
                cursor.correction_hint = (
                    "已向邮箱发码（Send）。下一轮程序先取验证码；取到后点验证码输入框，"
                    "程序按该次点击的坐标输入。勿重复点 Send，勿在点框前输入。"
                )
            elif not pending_send_review:
                cursor.correction_hint = (
                    "已点发送验证码；下一轮程序链将 get_otp 并填码，再点登录。"
                    "本 turn 勿重复发码或提前 get_otp。"
                )
        elif login_submit_tap_matches(_tap_sel) and not send_just:
            cursor.step_intents_done.add("login_submit")
            if account_session_logged_in(ctx):
                cursor.login_continue_stall = 0
                cursor.step_goal_met = True
                cursor.correction_hint = (
                    "账号已登录（session=logged_in）；本步操作已达成，请立即 signal_done，"
                    "勿再点 Continue/登录/头像。"
                )
                cursor.bind_finish_context(ctx, list(history[-24:]))
                cursor.try_auto_finish_do_when_milestones_met(ctx, writer=writer)
            else:
                fp_now = str(post_fp or screen_fp or "")
                last = str(getattr(cursor, "_last_continue_screen_fp", "") or "")
                if fp_now and last and fp_now == last:
                    cursor.login_continue_stall = int(
                        getattr(cursor, "login_continue_stall", 0) or 0
                    ) + 1
                else:
                    cursor.login_continue_stall = 0
                if fp_now:
                    setattr(cursor, "_last_continue_screen_fp", fp_now)
                if int(getattr(cursor, "login_continue_stall", 0) or 0) >= 2:
                    cursor.correction_hint = (
                        "Continue 已连点但账号仍未登录；勿再点 Continue，"
                        "请 signal_ask_human 或核对验证码/邮箱。"
                    )
        elif (
            re.search(r"\bEmail\b|邮箱", _tap_sel, re.I)
            or (
                re.search(r"log\s*in|sign\s*in|登录", _tap_sel, re.I)
                and not re.search(r"email|邮箱", _tap_sel, re.I)
            )
        ):
            from mino_nexus.loop.email_login_auto import run_email_login_ui_chain
            from mino_nexus.loop.step_contract import instruction_allows_login_flow
            from mino_nexus.loop.step_flow_scope import login_flow_allowed

            _lf_em, _ = login_flow_allowed(
                cursor=cursor,
                phase="do",
                instruction=str(cur.instruction or ""),
                expected=str(cur.expected or ""),
            )
            if _lf_em and not getattr(cursor, "login_chain_broken", False):
                from mino_nexus.loop.login_account_lease import try_lease_on_login_surface

                _ok_lease, _lease_err, _lease_brief = try_lease_on_login_surface(
                    ctx, case
                )
                if not _ok_lease and _lease_err:
                    cursor.correction_hint = (
                        f"登录界面租号失败：{_lease_err}；"
                        "请检查号池或 signal_ask_human。"
                    )
                hist_em = list(history[-24:])
                if status_val == "pass":
                    hist_em.append(
                        f"{seq}. tap_element → pass: {_tap_sel[:120]}"
                    )
                if _ok_lease and _lease_brief:
                    hist_em.append(
                        f"{seq}. lease_account → pass: {_lease_brief[:160]}"
                    )
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
                        return TapFlow(leave=leave(status="fail", summary=fail_msg, reason_code="login_program_incomplete"))
                    lc, ls = _consume_login_chain(
                        chain_em,
                        seq=seq,
                        cursor=cursor,
                        rec=rec,
                        emit=emit,
                        thumb=thumb,
                        screen_fp=str(post_fp or screen_fp or ""),
                        login_flow_step=instruction_allows_login_flow(
                            str(cur.instruction or ""),
                            login_module_case=bool(login_module_case),
                        ),
                        fuse_web=_progress_gate_web_kwargs(ctx, proxy),
                        writer=writer,
                    )
                    for item in chain_em:
                        if str(item.get("capability_id") or "") == "input_text" and item.get("ok"):
                            cursor.record_step_op(
                                "input_text",
                                params={"field": "email"},
                            )
                            cursor.step_intents_done.add("login_email")
                        if str(item.get("step_id") or "") == "send_code" and item.get("ok"):
                            cursor.step_intents_done.add("sms_send")
                            cursor.login_post_sms_pending = True
                            cursor.login_post_sms_stall = 0
                    _log_turn_end(writer, cap=lc or "email_login", status=ls)
                    return TapFlow(cont=True)
    return TapFlow()
