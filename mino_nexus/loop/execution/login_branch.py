"""登录分支：会话链推进与登录链守卫。"""
from __future__ import annotations

def _consume_login_chain(
    chain: list[dict[str, Any]],
    *,
    seq: int,
    cursor: StepCursor,
    rec,
    emit,
    thumb: str,
    screen_fp: str = "",
    login_flow_step: bool = False,
    fuse_web: dict[str, Any] | None = None,
    writer: Any = None,
) -> tuple[str, str]:
    last_cap, last_st = "", "fail"
    from mino_nexus.loop.fuse.action_fuse import fuseable_cap
    from mino_nexus.loop.observe.program_tool_log import log_program_tool
    from mino_nexus.loop.router_proxy import is_local_cap

    intents = set(getattr(cursor, "step_intents_done", None) or set())
    for item in chain:
        cap = str(item.get("capability_id") or "noop")
        st = str(item.get("status") or "fail")
        summ = str(item.get("summary") or "")
        if writer is not None and item.get("ran_get_otp"):
            log_program_tool(
                writer,
                capability_id="get_otp",
                params={},
                status="pass" if item.get("ok") else "fail",
                summary="程序链内 get_otp",
                executor_used="internal",
                source="login_chain",
            )
        if writer is not None and cap and not is_local_cap(cap):
            log_program_tool(
                writer,
                capability_id=cap,
                params=dict(item.get("params") or {}),
                status=st,
                summary=summ,
                executor_used="device",
                source="login_chain",
            )
        if item.get("ran_get_otp"):
            cursor.record_step_op("get_otp")
        if cap == "input_text" and item.get("ok"):
            fld = str((item.get("params") or {}).get("field") or "sms_code")
            cursor.record_step_op("input_text", params={"field": fld})
            if fld == "email":
                cursor.step_intents_done.add("login_email")
                setattr(cursor, "email_tab_guard_streak", 0)
        if item.get("step_id") == "send_code" and item.get("ok"):
            cursor.step_intents_done.add("sms_send")
        if cap == "tap_element" and item.get("ok"):
            cursor.record_step_op("tap_element", params={"selector_text": "登录"})
        if item.get("ok") and fuseable_cap(cap):
            params: dict[str, Any] = dict(item.get("params") or {})
            if cap == "input_text" and not params.get("field"):
                params = {"field": "sms_code"}
            elif cap == "tap_element":
                params = {"selector_text": "登录"}
            cursor.progress_gate.record_pass(
                cap_id=cap,
                params=params,
                pre_fp=screen_fp,
                post_fp=screen_fp,
                intents_done=set(getattr(cursor, "step_intents_done", None) or intents),
                login_flow_step=login_flow_step,
                **(fuse_web or {"web_channel": False, "web_focus_fp": ""}),
            )
        rec(seq, capability_id=cap, status=st, summary=summ, thought=summ, thumb=thumb)
        emit(
            "result",
            thought=summ,
            step=seq,
            capability_id=cap,
            status=st,
            summary=summ,
            thumb=thumb,
        )
        last_cap, last_st = cap, st
    return last_cap, last_st


def _login_chain_guard(
    chain: list[dict[str, Any]],
    *,
    cursor: StepCursor,
) -> str:
    """程序登录链全失败时累计 streak；误走本地 executor 时停用链。非空则 agent 应 fail 退出。"""
    if not chain:
        return ""
    if any(bool(x.get("ok")) for x in chain):
        cursor.login_chain_fail_streak = 0
        return ""
    streak = int(getattr(cursor, "login_chain_fail_streak", 0) or 0) + 1
    cursor.login_chain_fail_streak = streak
    if any("未知本地能力" in str(x.get("summary") or "") for x in chain):
        setattr(cursor, "login_chain_broken", True)
        cursor.correction_hint = (
            "程序登录链曾误将 input_text/tap 当本地能力执行；已停用程序链，请由模型正常派发设备能力。"
        )
        return ""
    if streak >= 8:
        head = str(chain[0].get("summary") or chain[0].get("capability_id") or "login_chain")
        return f"登录程序链连续失败 {streak} 轮：{head}"[:400]
    return ""
