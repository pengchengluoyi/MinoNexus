"""闭集谓词 / 守卫 / 推进器 / 槽取值注册表。"""
from __future__ import annotations

import re
from typing import Any, Callable, Optional

from mino_nexus.catalog.exec_classes import MUTATE_CAPS
from mino_nexus.loop.step_pointer import (
    format_skip_repeat_tap_message,
    is_guest_entry_step,
    is_observe_only_step,
    repeats_last_tap,
    tap_summary_is_guest_entry,
    tap_summary_is_login_entry,
)

INSPECTION_ATS = frozenset({
    "case_start",
    "case_end",
    "phase_enter:prep",
    "phase_enter:do",
    "phase_enter:check",
    "phase_exit:prep",
    "phase_exit:do",
    "phase_exit:check",
})

GuardFn = Callable[[dict[str, Any]], Optional[str]]


def _guard_deny_mutate(ctx: dict[str, Any]) -> Optional[str]:
    if str(ctx.get("phase") or "") == "check" and str(ctx.get("cap_id") or "") in MUTATE_CAPS:
        return "校验阶段不能改界面，已拒绝这次操作"
    return None


RECOVER_PREFIX = "recover_"
ADVISE_RECOVERY_MAX = 2
RECOVERY_FAIL_MAX = 2


def _recovery_rule_id(cap_id: str) -> str:
    cid = str(cap_id or "").strip()
    if cid.startswith(RECOVER_PREFIX):
        return cid[len(RECOVER_PREFIX):]
    return ""


def _guard_exec_script_params(ctx: dict[str, Any]) -> Optional[str]:
    cap_id = str(ctx.get("cap_id") or "")
    if cap_id != "exec_script":
        return None
    params = dict(ctx.get("params") or {})
    if str(params.get("script_id") or params.get("script") or "").strip():
        return None
    return (
        "exec_script 缺少 script_id 或 script 参数，已拒绝。"
        "检查类前置若已满足可直接 signal_done；读设备信息用 read_device_data。"
    )


def _guard_limit_advise_recovery(ctx: dict[str, Any]) -> Optional[str]:
    return _guard_limit_recovery_retry(ctx)


def _guard_limit_recovery_retry(ctx: dict[str, Any]) -> Optional[str]:
    cap_id = str(ctx.get("cap_id") or "")
    rule_id = _recovery_rule_id(cap_id)
    if not rule_id:
        return None
    try:
        from mino_nexus.catalog import registry as catalog

        rule = catalog.get_recovery_rule(rule_id)
    except Exception:
        rule = None
    mode = str(getattr(rule, "mode", "") or "") if rule is not None else ""
    step_cursor = ctx.get("step_cursor")
    if mode == "advise":
        used = int(getattr(step_cursor, "advise_recovery_counts", {}).get(rule_id, 0) or 0)
        cap = ADVISE_RECOVERY_MAX
    else:
        used = int(getattr(step_cursor, "recovery_fail_counts", {}).get(rule_id, 0) or 0)
        cap = RECOVERY_FAIL_MAX
    if used < cap:
        return None
    snippet = str(getattr(rule, "prompt_snippet", "") or "").strip() if rule is not None else ""
    if snippet:
        snippet = snippet.replace("\n", " ").strip()
        if len(snippet) > 160:
            snippet = snippet[:160].rstrip() + "…"
    if rule_id == "screen_secure_or_no_capture":
        return (
            f"已拒绝重复 recover_{rule_id}：同类恢复已失败/已提示 {used} 次。"
            f"{snippet or '截图全黑且未锁屏时唤醒无效。'}"
            f"请 press_key(BACK) 收起键盘、wait_ms 后再观察，或改用 tap_element / signal_ask_human；"
            f"禁止再调用本恢复。"
        )
    if rule_id in (
        "system_permission_dialog_while_using_deterministic",
        "system_permission_dialog",
        "system_permission_dialog_unified",
        "system_media_picker_dismiss",
    ):
        return (
            f"已拒绝重复 recover_{rule_id}：同类恢复已失败/已提示 {used} 次。"
            f"请改用 press_key(BACK)、tap_element 点「取消/关闭」、signal_ask_human 或 signal_give_up。"
        )
    extra = f"{snippet}" if snippet else "请改用 tap_element、press_key(BACK)、signal_ask_human 或 signal_give_up。"
    return (
        f"已拒绝重复 recover_{rule_id}：同类恢复已失败/已提示 {used} 次。"
        f"{extra}"
    )


def _tap_summary_label(line: str) -> str:
    text = str(line or "")
    if "→" not in text:
        return ""
    tail = text.split("→", 1)[-1]
    if ":" in tail:
        tail = tail.split(":", 1)[-1]
    return tail.strip()[:80]


def _guard_stuck_alternation(ctx: dict[str, Any]) -> Optional[str]:
    if str(ctx.get("phase") or "") != "do":
        return None
    if str(ctx.get("cap_id") or "") != "tap_element":
        return None
    labels = [
        _tap_summary_label(line)
        for line in (ctx.get("history_lines") or [])
        if "tap_element" in str(line) and "→ pass" in str(line)
    ]
    labels = [x for x in labels if x]
    if len(labels) < 4:
        return None
    a, b, c, d = labels[-4], labels[-3], labels[-2], labels[-1]
    if a == c and b == d and a != b:
        step_cursor = ctx.get("step_cursor")
        cur = ctx.get("cursor")
        instr = str(getattr(cur, "instruction", "") or "").strip() if cur else ""
        if (
            step_cursor is not None
            and is_guest_entry_step(instr)
            and bool(getattr(step_cursor, "guest_entry_tapped", False))
        ):
            return (
                f"已拒绝重复切换 {a} ⇄ {b}：访客入口已点过。"
                f"若屏上已是首页内容请 signal_done 结束本步，再执行下一步切换 Tab。"
            )
        return (
            f"已拒绝重复切换 {a} ⇄ {b}：同一对入口已交替点击多轮仍无进展。"
            f"请换策略（找「游客模式」等正确入口、返回、logout、signal_give_up）。"
        )
    return None


def _guard_block_login_after_guest(ctx: dict[str, Any]) -> Optional[str]:
    if str(ctx.get("phase") or "") != "do":
        return None
    if str(ctx.get("cap_id") or "") != "tap_element":
        return None
    cur = ctx.get("cursor")
    step_cursor = ctx.get("step_cursor")
    instr = str(getattr(cur, "instruction", "") or "").strip() if cur else ""
    if not is_guest_entry_step(instr):
        return None
    if not bool(getattr(step_cursor, "guest_entry_tapped", False)):
        return None
    summary = str(ctx.get("tap_summary") or "")
    params = dict(ctx.get("params") or {})
    blob = f"{summary} {params.get('selector_text') or ''}"
    if not tap_summary_is_login_entry(blob):
        return None
    return (
        "已点击访客/游客入口，本步目标应已达成。"
        "请 signal_done 结束本步，勿再点登录方式或「我的」Tab。"
    )


def _guard_require_do_work(ctx: dict[str, Any]) -> Optional[str]:
    """供 agent_loop 在 signal_done 前调用。"""
    if str(ctx.get("phase") or "") != "do":
        return None
    if str(ctx.get("intent") or "") != "signal_done":
        return None
    cur = ctx.get("cursor")
    step_cursor = ctx.get("step_cursor")
    instr = str(getattr(cur, "instruction", "") or "").strip() if cur else ""
    if is_observe_only_step(instr) or bool(getattr(cur, "observe_only", False)):
        return None
    from mino_nexus.loop.step_intent import instruction_required_intents, step_intents_satisfied

    need_int = instruction_required_intents(instr)
    ok_int = True
    if need_int:
        ok_int, int_msg = step_intents_satisfied(
            instruction=instr,
            intents_done=getattr(step_cursor, "step_intents_done", None),
        )
        if not ok_int:
            from mino_nexus.loop.step_intent import format_intent_progress

            prog = format_intent_progress(
                instruction=instr,
                intents_done=getattr(step_cursor, "step_intents_done", None),
            )
            return f"本步意图未达成，不能 signal_done。{prog}"
    from mino_nexus.loop.step_contract import step_actions_satisfied

    ok_fam, fam_msg = step_actions_satisfied(
        instruction=instr,
        families_done=getattr(step_cursor, "step_action_families", None),
        family_counts=getattr(step_cursor, "step_family_counts", None),
    )
    if need_int:
        if not ok_fam and need_int.intersection({"swipe_gesture", "nav_tab"}) and not ok_int:
            return f"本步操作未做完，不能 signal_done。{fam_msg}"
    elif not ok_fam:
        return f"本步操作未做完，不能 signal_done。{fam_msg}"
    ops = int(getattr(step_cursor, "step_ops", 0) or 0)
    exp = str(getattr(cur, "expected", "") or "").strip() if cur else ""
    goal_met = bool(ctx.get("step_goal_met"))
    if ops > 0:
        if need_int:
            ok_int, int_msg = step_intents_satisfied(
                instruction=instr,
                intents_done=getattr(step_cursor, "step_intents_done", None),
            )
            if not ok_int:
                return f"本步意图未达成，不能 signal_done。{int_msg}"
        if exp and not goal_met:
            from mino_nexus.loop.step_effect import expected_absence_terms
            from mino_nexus.loop.step_pointer import _expected_defers_to_check

            defer_chk = _expected_defers_to_check(exp)
            if expected_absence_terms(exp) and need_int:
                ok_int, _ = step_intents_satisfied(
                    instruction=instr,
                    intents_done=getattr(step_cursor, "step_intents_done", None),
                )
                if ok_int:
                    return None
            if defer_chk:
                if need_int:
                    return None
            else:
                if need_int:
                    ok_int_done, _ = step_intents_satisfied(
                        instruction=instr,
                        intents_done=getattr(step_cursor, "step_intents_done", None),
                    )
                    if ok_int_done:
                        return None
                ok_fam_done, _ = step_actions_satisfied(
                    instruction=instr,
                    families_done=getattr(step_cursor, "step_action_families", None),
                    family_counts=getattr(step_cursor, "step_family_counts", None),
                )
                if (ok_fam_done or ops > 0) and not need_int:
                    return (
                        "本步 expected 尚未在屏上确认（如半屏登录、进详情），"
                        "不能 signal_done。请完成业务目标，勿在 tap 次数达标后直接收工。"
                    )
        return None
    if bool(ctx.get("step_goal_met")):
        hint = str(getattr(step_cursor, "step_effect_hint", "") or "")
        if hint.startswith("【达成提示·弱】"):
            return (
                "屏上可能仅出现底栏 Tab 等弱信号，不等于本步操作已完成。"
                "请继续 fsm_navigate 或 tap_element 进入目标页后再 signal_done。"
            )
        return None
    if str(getattr(step_cursor, "step_effect_hint", "") or "").startswith("【达成提示】"):
        return None
    if need_int and ok_int:
        from mino_nexus.loop.step_pointer import _expected_defers_to_check

        exp_defer = str(getattr(cur, "expected", "") or "").strip() if cur else ""
        if _expected_defers_to_check(exp_defer) or bool(ctx.get("step_goal_met")):
            return None
    return (
        "本步尚未执行任何设备操作（点击/输入/滑动/等待等），不能 signal_done。"
        "请先完成步骤原文要求的具体动作。"
    )


def _guard_block_assert_in_do(ctx: dict[str, Any]) -> Optional[str]:
    """突变步不得用 assert_visual 跳过 do；观察步允许（随后仍进 check）。"""
    if str(ctx.get("phase") or "") != "do":
        return None
    if str(ctx.get("cap_id") or "") != "assert_visual":
        return None
    cur = ctx.get("cursor")
    instr = str(getattr(cur, "instruction", "") or "").strip() if cur else ""
    if is_observe_only_step(instr) or bool(getattr(cur, "observe_only", False)):
        return None
    return (
        "操作阶段不能用 assert_visual 跳过动作。"
        "请先完成点击/输入/等待，再 signal_done 进入校验。"
    )


def _guard_block_mutate_when_thought_done(ctx: dict[str, Any]) -> Optional[str]:
    phase = str(ctx.get("phase") or "")
    if phase not in ("do", "prep"):
        return None
    from mino_nexus.loop.thought_done import thought_implies_signal_done

    thought = str(ctx.get("decision_thought") or "")
    if not thought_implies_signal_done(thought):
        return None
    cap = str(ctx.get("cap_id") or "")
    if not cap or cap.startswith("signal_") or cap in ("wait_ms",):
        return None
    if cap.startswith("recover_"):
        return None
    return (
        "模型思考已结论本步/前置应收工（signal_done），与实际 capability 不一致。"
        f"请 decision.status=done，勿再 {cap}。"
    )


def _guard_block_prep_login_when_logged_in_required(ctx: dict[str, Any]) -> Optional[str]:
    """已登录前置（非 relogin）：禁止模型走登录链填表，须先 confirm session。"""
    if str(ctx.get("phase") or "") != "prep":
        return None
    scene = ctx.get("case_scene") if isinstance(ctx.get("case_scene"), dict) else {}
    from mino_nexus.runtime.session_gate import required_session, session_prep_intent

    if required_session(scene=scene) != "logged_in":
        return None
    if session_prep_intent(scene) == "relogin":
        return None
    cap = str(ctx.get("cap_id") or "")
    if not cap or cap.startswith("recover_") or cap.startswith("signal_"):
        return None
    if cap in ("accept_legal_consent", "wait_ms", "wait_screen_ready", "get_foreground_app"):
        return None
    msg = (
        "前置要求已登录：须先观察主界面或程序进「我的」确认 session，"
        "禁止 get_otp/发码/填手机号或点登录入口。"
        "确认 session=logged_in 后再 signal_done。"
    )
    if cap in ("get_otp", "request_sms_code", "lease_account"):
        return msg
    if cap == "input_text":
        field = str((ctx.get("params") or {}).get("field") or "").lower()
        if field in ("phone", "sms_code", "password", "验证码", "sms", "otp"):
            return msg
        return (
            "前置要求已登录：禁止 input_text 填表。"
            "请 wait_screen_ready / tap 底栏「我的」或由程序确认登录态，勿模拟登录。"
        )
    if cap == "tap_element":
        blob = str(ctx.get("tap_summary") or "")
        if re.search(r"发送验证码|获取验证码|去登录|立即登录", blob):
            return msg
        from mino_nexus.loop.step_pointer import tap_summary_is_login_entry

        if tap_summary_is_login_entry(blob):
            return msg
    return None


def _guard_block_prep_guest_mine_tab(ctx: dict[str, Any]) -> Optional[str]:
    """guest 前置仍 logged_in 时，禁止点「我的」冒充未登录登录页。"""
    if str(ctx.get("phase") or "") != "prep":
        return None
    if str(ctx.get("cap_id") or "") != "tap_element":
        return None
    scene = ctx.get("case_scene") if isinstance(ctx.get("case_scene"), dict) else {}
    from mino_nexus.runtime.session_gate import required_session

    if required_session(scene=scene) != "guest":
        return None
    from mino_nexus.loop.session_ensure import parse_session_value

    session = parse_session_value(str(ctx.get("session_block") or ""))
    if session != "logged_in":
        return None
    blob = str(ctx.get("tap_summary") or "")
    if "我的" not in blob and "My" not in blob:
        return None
    return (
        "session_block 仍为 logged_in，本用例要求 guest。"
        "勿点「我的」进登录页收工；请走路线图 logout 边或 recover_restart_target_app 后再 signal_done。"
    )


def _guard_block_login_flow_unless_step_scope(ctx: dict[str, Any]) -> Optional[str]:
    phase = str(ctx.get("phase") or "")
    if phase not in ("do", "prep"):
        return None
    if ctx.get("login_flow_interrupt"):
        return None
    cap = str(ctx.get("cap_id") or "")
    if not cap or cap.startswith("recover_") or cap.startswith("signal_"):
        return None
    # 应用内隐私/协议弹窗不是「延后登录弹窗」；步骤 1 进详情前常必须先同意。
    if cap == "accept_legal_consent":
        return None
    cur = ctx.get("cursor")
    step_cursor = ctx.get("step_cursor")
    instr = str(getattr(cur, "instruction", "") or "").strip() if cur else ""
    expected = str(getattr(cur, "expected", "") or "").strip() if cur else ""
    from mino_nexus.loop.step_contract import instruction_allows_login_flow
    from mino_nexus.loop.step_flow_scope import login_flow_allowed

    if step_cursor is not None:
        allowed, defer_msg = login_flow_allowed(
            cursor=step_cursor,
            phase=phase,
            instruction=instr,
            expected=expected,
        )
        if not allowed and defer_msg:
            login_caps = {
                "get_otp",
                "request_sms_code",
                "lease_account",
            }
            if cap in login_caps:
                return defer_msg
            if cap == "input_text":
                field = str((ctx.get("params") or {}).get("field") or "").lower()
                if field in ("phone", "sms_code", "password", "验证码"):
                    return defer_msg
            if cap == "tap_element":
                from mino_nexus.loop.step_pointer import tap_summary_is_login_entry

                blob = str(ctx.get("tap_summary") or "")
                if tap_summary_is_login_entry(blob):
                    return defer_msg

    if instruction_allows_login_flow(instr, login_module_case=False):
        return None
    login_caps = {
        "get_otp",
        "request_sms_code",
        "lease_account",
    }
    if cap in login_caps:
        return (
            "本步未要求登录/验证码流程，禁止调用 "
            f"{cap}。若屏上 expected 已满足请 signal_done，勿展开登录链。"
        )
    if cap == "input_text":
        field = str((ctx.get("params") or {}).get("field") or "").lower()
        if field in ("phone", "sms_code", "password", "验证码"):
            return (
                "本步未要求登录/验证码，禁止 input_text 填 "
                f"{field}。请 signal_done 或做本步 instruction 内的操作。"
            )
    if cap == "tap_element":
        from mino_nexus.loop.step_pointer import tap_summary_is_login_entry

        blob = str(ctx.get("tap_summary") or "")
        if tap_summary_is_login_entry(blob):
            return (
                "本步未要求登录，禁止点击登录入口。"
                "若本步目标页已出现请 signal_done。"
            )
    return None


def _guard_block_back_without_nav_back_semantics(ctx: dict[str, Any]) -> Optional[str]:
    """D5-A：recovery 建议 BACK 时放行；否则无 plan allow_back 时拒 LLM 盲 BACK。"""
    if ctx.get("recovery_allow_back"):
        return None
    if str(ctx.get("phase") or "") != "do":
        return None
    cap = str(ctx.get("cap_id") or "")
    if cap != "press_key":
        return None
    key = str((ctx.get("params") or {}).get("key") or "").strip().upper()
    if key not in ("BACK", "BACK_KEY"):
        return None
    step_cursor = ctx.get("step_cursor")
    hint = str(getattr(step_cursor, "step_nav_plan_hint", "") or "")
    if "【本步导航】" in hint and "降级" not in hint:
        return (
            "本步已有 Nav 路线图，禁止用系统 BACK 代替图上 Tab/入口边。"
            "请 fsm_navigate 或 tap_element；若 recovery 已提示 BACK 则按提示执行。"
        )
    return None


def _guard_block_do_after_step_goal(ctx: dict[str, Any]) -> Optional[str]:
    if str(ctx.get("phase") or "") != "do":
        return None
    step_cursor = ctx.get("step_cursor")
    if not bool(getattr(step_cursor, "step_goal_met", False)):
        return None
    cap = str(ctx.get("cap_id") or "")
    if cap in ("", "signal_done", "signal_give_up", "wait_ms"):
        return None
    if cap.startswith("recover_") or cap in ("dismiss_ime", "accept_legal_consent"):
        return None
    return (
        "本步目标已在屏上达成，请立刻 signal_done 进入校验，"
        f"勿再执行 {cap}。"
    )


def _guard_require_sms_send_before_otp(ctx: dict[str, Any]) -> Optional[str]:
    cap = str(ctx.get("cap_id") or "")
    if cap not in ("get_otp", "input_text"):
        return None
    if str(ctx.get("phase") or "") != "do":
        return None
    cur = ctx.get("cursor")
    instr = str(getattr(cur, "instruction", "") or "").strip() if cur else ""
    from mino_nexus.loop.step_contract import instruction_allows_login_flow

    if not instruction_allows_login_flow(
        instr,
        login_module_case=bool(ctx.get("login_module_case")),
    ):
        return None
    if cap == "input_text":
        field = str((ctx.get("params") or {}).get("field") or "").lower()
        if field not in ("sms_code", "验证码"):
            return None
    hist = list(ctx.get("history_lines") or [])
    step_cursor = ctx.get("step_cursor")
    done = set(getattr(step_cursor, "step_intents_done", None) or set()) if step_cursor else set()
    from mino_nexus.loop.login_submit import _email_login_kind, _email_ready_for_otp, _sms_send_done

    if cap == "input_text" and _email_login_kind(ctx.get("run_ctx")):
        field = str((ctx.get("params") or {}).get("field") or "").lower()
        if field in ("phone", "username", "login_phone"):
            return (
                "当前为邮箱登录（login.kind=email）：须 input_text(field=email) 填租号邮箱，"
                "禁止 field=phone（勿填 display_name/用户名）。"
            )
        text = str((ctx.get("params") or {}).get("text") or "").strip()
        if field in ("email", "login_email") and text and "@" not in text:
            return (
                "邮箱登录须填入含 @ 的租号邮箱，勿填 display_name/用户名；"
                "请 field=email 并由系统从号池写入。"
            )
    run_ctx = ctx.get("run_ctx") or ctx.get("ctx")
    if cap == "get_otp" and _email_login_kind(run_ctx):
        if not _email_ready_for_otp(hist, done):
            from mino_nexus.loop.login_verification import verification_send_required_message

            return verification_send_required_message(run_ctx)
    if _sms_send_done(hist, done):
        from mino_nexus.loop.login_verification import record_verification_send

        if float(getattr(run_ctx, "otp_sent_at", 0) or 0) <= 0:
            record_verification_send(run_ctx)
        return None
    blob = f"{ctx.get('session_block') or ''}\n{instr}"
    if not re.search(r"验证码|短信|sms|otp|Send|邮箱", blob, re.I):
        return None
    from mino_nexus.loop.login_verification import verification_send_required_message

    return verification_send_required_message(run_ctx)


def _guard_block_repeat_email_tab(ctx: dict[str, Any]) -> Optional[str]:
    """Web/邮箱登录：Email 标签已选过则禁止再 tap，逼模型 input_text(field=email)。"""
    if str(ctx.get("cap_id") or "") != "tap_element":
        return None
    if str(ctx.get("phase") or "") != "do":
        return None
    from mino_nexus.loop.ui_channel import ui_channel_from_ctx, UiChannel

    run_ctx = ctx.get("run_ctx") or ctx.get("ctx")
    if ui_channel_from_ctx(run_ctx) != UiChannel.WEB:
        return None
    params = dict(ctx.get("params") or {})
    sel = str(
        params.get("selector_text")
        or params.get("text")
        or params.get("content_desc")
        or ""
    )
    # 仅放行小写 field 提示（input 框）；勿用 re.I，否则会误放行「Email」标签。
    if sel.strip() == "email":
        return None
    if not re.search(r"\bEmail\b|邮箱", sel, re.I):
        return None
    if str(params.get("field") or params.get("login_field") or "").lower() in (
        "email",
        "login_email",
    ):
        return None
    target = params.get("target")
    if isinstance(target, dict):
        role = str(target.get("role") or target.get("tag") or "").lower()
        if role in ("textbox", "input", "combobox", "searchbox"):
            return None
        blob = " ".join(
            str(target.get(k) or "") for k in ("text", "content_desc", "aria_label", "name")
        )
        if "@" in blob or re.search(r"gmail|mail\.|邮箱", blob, re.I):
            return None
    cur = ctx.get("cursor")
    instr = str(getattr(cur, "instruction", "") or "").strip() if cur else ""
    from mino_nexus.loop.step_contract import instruction_allows_login_flow

    if not instruction_allows_login_flow(
        instr,
        login_module_case=bool(ctx.get("login_module_case")),
    ):
        return None
    from mino_nexus.loop.email_login_auto import _email_input_done, _email_tab_selected

    hist = list(ctx.get("history_lines") or [])
    step_cursor = ctx.get("step_cursor")
    done = (
        set(getattr(step_cursor, "step_intents_done", None) or set())
        if step_cursor
        else set()
    )
    if _email_input_done(hist, done):
        return None
    if not _email_tab_selected(hist):
        return None
    return (
        "Email 标签已选过；请 input_text(field=email) 填租号邮箱（含 @），"
        "或点 Send / request_sms_code 发邮箱验证码。禁止重复点 Email 标签。"
    )


def _guard_block_repeat_continue_submit(ctx: dict[str, Any]) -> Optional[str]:
    """Web 邮箱登录：Continue 已连点仍非 logged_in 时禁止再 tap，避免空转烧步。"""
    if str(ctx.get("cap_id") or "") != "tap_element":
        return None
    if str(ctx.get("phase") or "") != "do":
        return None
    from mino_nexus.loop.login_submit import (
        _otp_code_already_entered,
        _sms_send_done,
        account_session_logged_in,
        login_submit_tap_matches,
    )
    from mino_nexus.loop.login_verification import is_web_email_login

    run_ctx = ctx.get("run_ctx") or ctx.get("ctx")
    if not is_web_email_login(run_ctx):
        return None
    params = dict(ctx.get("params") or {})
    sel = str(
        params.get("selector_text")
        or params.get("text")
        or params.get("content_desc")
        or ""
    )
    if not login_submit_tap_matches(sel):
        return None
    step_cursor = ctx.get("step_cursor") or ctx.get("cursor")
    done = (
        set(getattr(step_cursor, "step_intents_done", None) or set())
        if step_cursor
        else set()
    )
    hist = list(ctx.get("history_lines") or [])
    if account_session_logged_in(run_ctx):
        return (
            "账号已登录（session=logged_in）；本步应 signal_done，"
            "禁止再点 Continue/登录/关弹窗/头像。"
        )
    if _sms_send_done(hist, done) and not _otp_code_already_entered(hist, done):
        return (
            "已发邮箱验证码：须先 get_otp + input_text(field=sms_code) 填入验证码，"
            "再点 Continue。禁止在验证码未填时点提交。"
        )
    from mino_nexus.loop.login_submit import _final_login_submit_done

    if _final_login_submit_done(hist, ctx=run_ctx):
        return (
            "Continue/登录提交已执行过；勿重复点绿色 Continue。"
            "若屏上已登录请 signal_done，否则核对验证码后 signal_ask_human。"
        )
    if not _otp_code_already_entered(hist, done):
        return None
    streak = int(getattr(step_cursor, "login_continue_stall", 0) or 0)
    if streak < 2:
        return None
    return (
        "Continue/提交已连点但账号仍未登录（session≠logged_in）；"
        "请 signal_ask_human 或核对验证码/邮箱，勿再重复点 Continue。"
    )


def _guard_require_otp_before_login_tap(ctx: dict[str, Any]) -> Optional[str]:
    """已发码但未填验证码时禁止点「登录」，避免空转（须先 get_otp + input_text）。"""
    if str(ctx.get("cap_id") or "") != "tap_element":
        return None
    if str(ctx.get("phase") or "") != "do":
        return None
    cur = ctx.get("cursor")
    instr = str(getattr(cur, "instruction", "") or "").strip() if cur else ""
    from mino_nexus.loop.step_contract import instruction_allows_login_flow

    if not instruction_allows_login_flow(
        instr,
        login_module_case=bool(ctx.get("login_module_case")),
    ):
        return None
    params = dict(ctx.get("params") or {})
    sel = str(params.get("selector_text") or params.get("text") or params.get("content_desc") or "")
    if not re.search(r"登录|立即登录", sel):
        return None
    step_cursor = ctx.get("step_cursor")
    done = set(getattr(step_cursor, "step_intents_done", None) or set()) if step_cursor else set()
    hist = list(ctx.get("history_lines") or [])
    if "sms_send" not in done and not _history_cap_passed(hist, "request_sms_code"):
        from mino_nexus.loop.login_submit import _sms_send_done

        if not _sms_send_done(hist, done):
            return None
    if "otp_fill" in done:
        return None
    from mino_nexus.loop.login_submit import _otp_code_already_entered

    if _otp_code_already_entered(hist, done):
        return None
    return (
        "已发送验证码但尚未填入验证码框。"
        "须先 get_otp（或程序链自动填码）再点登录；禁止空点登录按钮。"
    )


def _guard_require_session(ctx: dict[str, Any]) -> Optional[str]:
    """仅 prep 结束收工时核对 session；do 阶段勿拦 signal_done（否则 guest 用例在步骤 1 会 signal_done↔require_session 死循环）。"""
    if str(ctx.get("phase") or "") != "prep":
        return None
    if str(ctx.get("intent") or "") != "signal_done":
        return None
    scene = ctx.get("case_scene") if isinstance(ctx.get("case_scene"), dict) else {}
    if bool(ctx.get("prep_clear_done")):
        fact_sess = str(ctx.get("session_fact_session") or "").lower()
        if fact_sess in ("logged_out", "guest"):
            return None
    from mino_nexus.loop.session_prep_trust import prep_session_signal_done_block_reason

    reason = prep_session_signal_done_block_reason(
        scene=scene,
        session_block=str(ctx.get("session_block") or ""),
        ctx=ctx.get("run_ctx"),
    )
    if not reason:
        return None
    thought = str(ctx.get("decision_thought") or "")
    block = str(ctx.get("session_block") or "")
    if re.search(r"未登录|登录页|guest|游客|前置.{0,6}满足", thought, re.I) and "session=logged_in" in block:
        return (
            f"{reason} "
            "思考声称未登录/前置已满足，但 session_block 仍为 logged_in；"
            "请先 logout/recover 再 signal_done，勿与 require_session 结论矛盾。"
        )
    return reason


def _guard_skip_repeat_read_device(ctx: dict[str, Any]) -> Optional[str]:
    from mino_nexus.loop.step_pointer import (
        format_skip_repeat_read_device_message,
        last_read_device_summary,
        read_device_prep_satisfied,
    )

    if str(ctx.get("phase") or "") != "prep":
        return None
    if str(ctx.get("cap_id") or "") != "read_device_data":
        return None
    history = list(ctx.get("history_lines") or [])
    prev = last_read_device_summary(history)
    if not prev or not read_device_prep_satisfied(prev):
        return None
    return format_skip_repeat_read_device_message(prev)


def _menu_has_cap(ctx: dict[str, Any], cap_id: str) -> bool:
    menu = ctx.get("menu") or []
    if isinstance(menu, list):
        return any(str((m or {}).get("id") or "") == cap_id for m in menu)
    return False


def _menu_has_hitl(ctx: dict[str, Any]) -> bool:
    menu = ctx.get("menu") or []
    if not isinstance(menu, list):
        return False
    return any(str((m or {}).get("id") or "").startswith("human_") for m in menu)


def _leased_account(ctx: dict[str, Any]) -> bool:
    brief = str(ctx.get("accounts_brief") or "").strip()
    if not brief or brief.startswith("（未租") or "未租" in brief[:12]:
        return False
    return True


def _system_ui_dismiss_phase(ctx: dict[str, Any]) -> bool:
    """权限/相册等系统挡屏：允许继续点关/返回，不因进展熔断拦死。"""
    if str(ctx.get("system_overlay") or "") == "yes":
        return True
    if str(ctx.get("app_foreground") or "") == "no":
        return True
    return False


def _guard_action_fuse(ctx: dict[str, Any]) -> Optional[str]:
    if _system_ui_dismiss_phase(ctx):
        return None
    step_cursor = ctx.get("step_cursor")
    if step_cursor is None:
        return None
    gate = getattr(step_cursor, "progress_gate", None) or getattr(step_cursor, "action_fuse", None)
    if gate is None:
        return None
    cap_id = str(ctx.get("cap_id") or "")
    cur = ctx.get("cursor")
    instr = str(getattr(cur, "instruction", "") or "") if cur else ""
    exp = str(getattr(cur, "expected", "") or "") if cur else ""
    from mino_nexus.loop.step_intent import is_profile_shape_completion_step
    from mino_nexus.loop.step_contract import instruction_allows_login_flow

    login_flow_step = instruction_allows_login_flow(
        instr,
        login_module_case=bool(ctx.get("login_module_case")),
    )
    from mino_nexus.loop.ui_channel import UiChannel, ui_channel_from_ctx

    run_ctx = ctx.get("run_ctx")
    web_channel = ui_channel_from_ctx(run_ctx) == UiChannel.WEB

    return gate.check(
        phase=str(ctx.get("phase") or ""),
        cap_id=cap_id,
        params=dict(ctx.get("params") or {}),
        screen_fp=str(ctx.get("screen_fp") or ""),
        has_get_otp=_menu_has_cap(ctx, "get_otp"),
        leased=_leased_account(ctx),
        has_hitl=_menu_has_hitl(ctx),
        profile_shape_completion=is_profile_shape_completion_step(instr, exp),
        intents_done=set(getattr(step_cursor, "step_intents_done", None) or set()),
        login_flow_step=login_flow_step,
        web_channel=web_channel,
    )


def _guard_skip_repeat_check_run_env(ctx: dict[str, Any]) -> Optional[str]:
    if str(ctx.get("phase") or "") != "prep":
        return None
    if str(ctx.get("cap_id") or "") != "check_run_env":
        return None
    return (
        "前置不再切换测试环境；环境由批次 env_profile 确定。"
        "请只做筛选账号、筛选设备、环境清理，或直接 signal_done。"
    )


def _guard_skip_repeat_get_app_version(ctx: dict[str, Any]) -> Optional[str]:
    if str(ctx.get("cap_id") or "") != "get_app_version":
        return None
    ver = str(ctx.get("app_version") or "").strip()
    if not ver:
        return None
    return (
        f"已拒绝重复 get_app_version（本任务已确认版本 {ver}）。"
        f"版本由 Scout 直接回传，无需看图；请继续其它前置或 signal_done。"
    )


def _history_cap_passed(history: list[str], cap_id: str) -> bool:
    needle = str(cap_id or "").strip()
    if not needle:
        return False
    pat = re.compile(rf"^\d+\.\s*{re.escape(needle)}\s*→\s*pass\b")
    for line in reversed(history or []):
        if pat.search(str(line or "").strip()):
            return True
    return False


def _guard_block_fsm_off_step_target(ctx: dict[str, Any]) -> Optional[str]:
    cap = str(ctx.get("cap_id") or "")
    if cap not in ("fsm_navigate", "recover_fsm_navigate"):
        return None
    cur = ctx.get("cursor")
    instr = str(getattr(cur, "instruction", "") or "").strip() if cur else ""
    exp = str(getattr(cur, "expected", "") or "").strip() if cur else ""
    from mino_nexus.loop.step_nav_plan import instruction_nav_target
    from mino_nexus.loop.step_pointer import _expected_defers_to_check
    from mino_nexus.services.nav_route import (
        click_label_from_nav_ref,
        load_fsm_doc,
        tab_root_label_for_state,
    )
    from mino_nexus.services.nav_state_resolve import plan_route_resolved

    oral = instruction_nav_target(instr) or click_label_from_nav_ref(instr)
    if not oral:
        return None
    params = ctx.get("params") if isinstance(ctx.get("params"), dict) else {}
    to_raw = str(params.get("to_state") or params.get("to") or "").strip()
    loc = ctx.get("nav_localized") if isinstance(ctx.get("nav_localized"), dict) else {}
    from_ref = str(loc.get("chosen") or "").strip()
    if _expected_defers_to_check(exp):
        from mino_nexus.loop.step_effect import localized_matches_step

        if localized_matches_step(loc, instruction=instr, expected=exp):
            return (
                "已在目标页且本步 expected 仅在 check 校验；请 signal_done，勿再 fsm_navigate。"
            )
    scene = ctx.get("case_scene") if isinstance(ctx.get("case_scene"), dict) else {}
    app_id = str(scene.get("app_id") or "").strip()
    if not app_id:
        return None
    fsm_doc, _ = load_fsm_doc(app_id, project_id=str(scene.get("project_id") or ""))
    fsm = fsm_doc or {}
    if not fsm:
        return None
    plan_oral = plan_route_resolved(fsm, from_ref=from_ref, to_ref=oral, localized=loc)
    if plan_oral.get("ok") and int(plan_oral.get("hop_count") or 0) <= 0:
        return (
            f"路线图判定已在「{oral}」相关目标屏；请 signal_done，"
            f"勿 fsm 到其它节点（{to_raw or '未填 to_state'}）。"
        )
    if not to_raw:
        return None
    plan_to = plan_route_resolved(fsm, from_ref=from_ref, to_ref=to_raw, localized=loc)
    dest_tab = tab_root_label_for_state(
        fsm, str(plan_to.get("to_state") or to_raw)
    )
    if not dest_tab:
        return None
    o = re.sub(r"[\s_·\-]+", "", oral.strip().lower())
    d = re.sub(r"[\s_·\-]+", "", dest_tab.strip().lower())
    t = re.sub(r"[\s_·\-]+", "", click_label_from_nav_ref(to_raw).strip().lower())
    if o and d and o != d and o not in t and t != o:
        return (
            f"本步 instruction 目标是「{oral}」，与 fsm 目标 Tab「{dest_tab}」不一致；"
            f"请改 to_state/口语目标或 signal_done。"
        )
    return None


def _guard_block_fsm_logged_in_session_drift(ctx: dict[str, Any]) -> Optional[str]:
    cap = str(ctx.get("cap_id") or "")
    if cap not in ("fsm_navigate", "recover_fsm_navigate"):
        return None
    scene = ctx.get("case_scene") if isinstance(ctx.get("case_scene"), dict) else {}
    cur = ctx.get("cursor")
    instr = str(getattr(cur, "instruction", "") or "").strip() if cur else ""
    exp = str(getattr(cur, "expected", "") or "").strip() if cur else ""
    params = ctx.get("params") if isinstance(ctx.get("params"), dict) else {}
    to_raw = str(params.get("to_state") or params.get("to") or "").strip()
    loc = ctx.get("nav_localized") if isinstance(ctx.get("nav_localized"), dict) else {}
    nodes = list(ctx.get("nav_hierarchy_nodes") or [])
    from mino_nexus.loop.nav_session_fork import (
        fsm_blocked_logged_in_session_drift,
        required_session_from_scene,
    )

    block, msg = fsm_blocked_logged_in_session_drift(
        required_session=required_session_from_scene(scene),
        instruction=instr,
        expected=exp,
        to_raw=to_raw,
        hierarchy_nodes=nodes,
        localized=loc,
    )
    return msg if block else None


def _guard_skip_repeat_fsm_declined(ctx: dict[str, Any]) -> Optional[str]:
    cap = str(ctx.get("cap_id") or "")
    if cap not in ("fsm_navigate", "recover_fsm_navigate"):
        return None
    params = ctx.get("params") if isinstance(ctx.get("params"), dict) else {}
    key = str(params.get("to_state") or params.get("to") or params.get("selector_text") or "")
    last = str(ctx.get("fsm_last_decline_key") or "")
    streak = int(ctx.get("fsm_decline_repeat_streak") or 0)
    if not last or key != last:
        return None
    if streak < 1:
        return None
    return (
        "已拒绝重复 fsm_navigate：上一轮同目标导航已 declined 且屏态未变。"
        "若已在目标页请 signal_done；否则 tap_element 直点 Tab/入口，勿再 fsm。"
    )


def _guard_skip_repeat_fsm_open_loop(ctx: dict[str, Any]) -> Optional[str]:
    from mino_nexus.loop.nav_onboarding_open_loop import fsm_params_blocked_in_open_loop

    return fsm_params_blocked_in_open_loop(ctx)


def _guard_skip_recover_screen_when_display_guard(ctx: dict[str, Any]) -> Optional[str]:
    cap = str(ctx.get("cap_id") or "")
    if cap not in ("recover_screen_asleep_or_locked",):
        return None
    if not bool(ctx.get("display_guard_active")):
        return None
    return (
        "亮屏/解锁由任务子线程自动处理，勿再 recover_screen_asleep_or_locked；"
        "请 wait_ms 后重试本步或 signal_done。"
    )


def _guard_skip_recover_bring_when_foreground(ctx: dict[str, Any]) -> Optional[str]:
    cap = str(ctx.get("cap_id") or "")
    if cap not in ("recover_bring_target_app_foreground",):
        return None
    if str(ctx.get("app_foreground") or "") != "yes":
        return None
    return (
        "probe/hierarchy 显示被测 App 已在前台，勿再 recover_bring_target_app_foreground；"
        "请继续本步 tap/fsm 或 signal_done。"
    )


def _guard_skip_repeat_launch_app(ctx: dict[str, Any]) -> Optional[str]:
    cap = str(ctx.get("cap_id") or "")
    if cap not in ("launch_app", "open_app", "open_url"):
        return None
    from mino_nexus.runtime.run_context import is_web_slot

    sn = str(ctx.get("sn") or "")
    run_ctx = ctx.get("run_ctx")
    plat = str(getattr(run_ctx, "platform", "") or "") if run_ctx is not None else ""
    web = is_web_slot(sn, plat)
    if str(ctx.get("app_foreground") or "") == "yes":
        if web:
            return (
                "已拒绝重复 launch_app：目标网址已在当前浏览器任务中打开。"
                "请 tap_element / input_text 继续，或前置满足后 signal_done。"
            )
        return (
            "已拒绝重复 launch_app：probe 显示被测 App 已在前台。"
            "请继续本步后续操作（如点 Tab），勿再次打开应用。"
        )
    if bool(ctx.get("app_launch_confirmed")):
        if web:
            return (
                "已拒绝重复 launch_app：本任务已成功打开目标网址。"
                "请根据截图继续前置（登录态/权限），勿重复 launch；"
                "若标签页被切走可再 launch_app(url) 或关闭后重开。"
            )
        return (
            "已拒绝重复 launch_app：本任务已成功启动过被测 App。"
            "若仍不在前台请 recover_bring_target_app_foreground，不要重复 launch_app。"
        )
    return None


def _guard_skip_repeat_clear_app_cache(ctx: dict[str, Any]) -> Optional[str]:
    if str(ctx.get("phase") or "") != "prep":
        return None
    if str(ctx.get("cap_id") or "") not in ("clear_app_cache", "system_pkg_clear"):
        return None
    if bool(ctx.get("prep_clear_done")):
        return (
            "已拒绝重复 clear_app_cache：本任务前置已成功清缓存。"
            "请继续 launch_app 或其它前置，或 signal_done 结束前置。"
        )
    if _history_cap_passed(list(ctx.get("history_lines") or []), "clear_app_cache"):
        return (
            "已拒绝重复 clear_app_cache：history 已有成功清缓存记录。"
            "请继续后续前置或 signal_done。"
        )
    return None


def _guard_skip_repeat_structural_cap(ctx: dict[str, Any]) -> Optional[str]:
    """结构型 cap（同意框/发码等）本步已成功一次则勿重复。"""
    if str(ctx.get("phase") or "") != "do":
        return None
    cap = str(ctx.get("cap_id") or "")
    from mino_nexus.loop.step_intent import is_structural_cap

    if not is_structural_cap(cap):
        return None
    step_cursor = ctx.get("step_cursor")
    done = set(getattr(step_cursor, "step_structural_caps_done", None) or set())
    if cap not in done:
        return None
    intents = set(getattr(step_cursor, "step_intents_done", None) or set()) if step_cursor else set()
    if cap == "get_otp" and "otp_fill" in intents:
        from mino_nexus.loop.step_intent import step_intents_satisfied

        cur = ctx.get("cursor")
        instr = str(getattr(cur, "instruction", "") or "").strip() if cur else ""
        ok, _ = step_intents_satisfied(instruction=instr, intents_done=intents)
        if not ok:
            return (
                "验证码已取且已填入，禁止重复 get_otp。"
                "请 tap_element 点击「登录」完成登录；若仍失败请检查验证码框是否已填齐。"
            )
    return (
        f"本步已成功执行过 {cap}，请勿重复。"
        "若屏上已达成本步意图请 signal_done；若未达成请 tap_element 或检查 hierarchy。"
    )


def _guard_block_idle_wait_in_do(ctx: dict[str, Any]) -> Optional[str]:
    """instruction 未要求等待时，意图已齐仍 wait_ms 空等 expected 结果。"""
    if str(ctx.get("phase") or "") != "do":
        return None
    if str(ctx.get("cap_id") or "") != "wait_ms":
        return None
    cur = ctx.get("cursor")
    instr = str(getattr(cur, "instruction", "") or "").strip() if cur else ""
    if re.search(r"等待|加载|稍候", instr, re.I):
        return None
    from mino_nexus.loop.step_intent import instruction_required_intents, step_intents_satisfied
    from mino_nexus.loop.step_pointer import _expected_defers_to_check

    exp = str(getattr(cur, "expected", "") or "").strip() if cur else ""
    if not _expected_defers_to_check(exp):
        return None
    need = instruction_required_intents(instr)
    if not need:
        return None
    step_cursor = ctx.get("step_cursor")
    ok_int, _ = step_intents_satisfied(
        instruction=instr,
        intents_done=getattr(step_cursor, "step_intents_done", None),
    )
    if not ok_int:
        return None
    return (
        "本步操作意图已齐；expected 中加载/下一页结果在 check 阶段验证。"
        "请 signal_done，勿再 wait_ms 空等。"
    )


def _guard_swipe_direction_vs_instruction(ctx: dict[str, Any]) -> Optional[str]:
    if str(ctx.get("phase") or "") != "do":
        return None
    if str(ctx.get("cap_id") or "") != "swipe_direction":
        return None
    cur = ctx.get("cursor")
    instr = str(getattr(cur, "instruction", "") or "").strip() if cur else ""
    from mino_nexus.loop.swipe_hint import instruction_swipe_direction, swipe_direction_label

    want = instruction_swipe_direction(instr)
    if not want:
        return None
    got = str((ctx.get("params") or {}).get("direction") or "").strip().lower()
    if not got or got == want:
        return None
    return (
        f"用例要求{swipe_direction_label(want)}，当前 direction={got} 与步骤原文相反。"
        f"请改用 swipe_direction direction={want}，或先确认截图再滑动。"
    )


def _guard_skip_repeat_swipe_stuck(ctx: dict[str, Any]) -> Optional[str]:
    """同向滑动多次且屏指纹不变 → 与 9/17 重复事件文档同类问题。"""
    if str(ctx.get("phase") or "") != "do":
        return None
    if str(ctx.get("cap_id") or "") != "swipe_direction":
        return None
    params = dict(ctx.get("params") or {})
    direction = str(params.get("direction") or "").strip().lower()
    if not direction:
        return None
    step_cursor = ctx.get("step_cursor")
    streak_fp = str(getattr(step_cursor, "swipe_stuck_fp", "") or "")
    streak_dir = str(getattr(step_cursor, "swipe_stuck_dir", "") or "")
    streak_n = int(getattr(step_cursor, "swipe_stuck_count", 0) or 0)
    fp = str(ctx.get("screen_fp") or "").strip()
    if streak_dir == direction and streak_n >= 3:
        from mino_nexus.loop.swipe_hint import instruction_swipe_direction, swipe_direction_label

        cur = ctx.get("cursor")
        instr = str(getattr(cur, "instruction", "") or "").strip() if cur else ""
        want = instruction_swipe_direction(instr)
        hint = swipe_direction_label(want) if want else "换滑动方向"
        return (
            f"已连续 {streak_n} 次同向 direction={direction} 滑动。"
            f"请 {hint}、换区域滑动，或 signal_done / signal_give_up；勿再重复 swipe。"
        )
    return None


def _guard_skip_repeat_satisfied_step_action(ctx: dict[str, Any]) -> Optional[str]:
    """动作族次数上限（legacy）。do 阶段已从 skill_defs 移除，避免与 LLM 空转对打。"""
    if str(ctx.get("phase") or "") != "do":
        return None
    cap = str(ctx.get("cap_id") or "")
    if not cap or cap.startswith("signal_") or cap.startswith("recover_"):
        return None
    from mino_nexus.loop.step_intent import instruction_required_intents, is_structural_cap

    cur = ctx.get("cursor")
    instr = str(getattr(cur, "instruction", "") or "").strip() if cur else ""
    if instruction_required_intents(instr) or is_structural_cap(cap):
        return None
    step_cursor = ctx.get("step_cursor")
    from mino_nexus.loop.step_contract import cap_action_family, instruction_action_counts

    fam = cap_action_family(cap)
    if not fam or fam == "key":
        return None
    need_n = int(instruction_action_counts(instr).get(fam) or 0)
    if need_n <= 0:
        return None
    got = int((getattr(step_cursor, "step_family_counts", None) or {}).get(fam) or 0)
    if got < need_n:
        return None
    if bool(getattr(step_cursor, "step_goal_met", False)):
        return f"本步动作次数已达标且目标已达成，请 signal_done，勿再 {cap}。"
    from mino_nexus.loop.step_intent import format_intent_progress

    prog = format_intent_progress(
        instruction=instr,
        intents_done=getattr(step_cursor, "step_intents_done", None),
    )
    extra = f" {prog}" if prog else ""
    return (
        f"本步 {fam} 次数已达标（{got}/{need_n}），但达成信号未确认，勿重复 {cap}。"
        f"请换目标元素/导航，或核对是否进错页后再 signal_done。{extra}"
    )


def _guard_prep_clear_before_launch(ctx: dict[str, Any]) -> Optional[str]:
    if str(ctx.get("phase") or "") != "prep":
        return None
    if str(ctx.get("cap_id") or "") not in ("launch_app", "open_app", "open_url"):
        return None
    from mino_nexus.loop.step_contract import precondition_requires_clear_cache

    prec = str(ctx.get("precondition") or "")
    if not precondition_requires_clear_cache(prec):
        return None
    if bool(ctx.get("prep_clear_done")):
        return None
    if _history_cap_passed(list(ctx.get("history_lines") or []), "clear_app_cache"):
        return None
    return (
        "前置要求先清缓存再打开应用：请先 clear_app_cache 成功，再 launch_app。"
        "顺序反了会落在系统设置/桌面，后续点击会失效。"
    )


def history_cap_passed(history: list[str], cap_id: str) -> bool:
    return _history_cap_passed(history, cap_id)


def _guard_skip_repeat_tap(ctx: dict[str, Any]) -> Optional[str]:
    if _system_ui_dismiss_phase(ctx):
        return None
    phase = str(ctx.get("phase") or "")
    cap_id = str(ctx.get("cap_id") or "")
    if phase in ("prep", "check") or cap_id != "tap_element":
        return None
    last = ctx.get("last_tap")
    if not last:
        return None
    current_fp = str(ctx.get("screen_fp") or "").strip()
    last_fp = str((last or {}).get("screen_fp") or "").strip()
    # 页面已切换（含误点进子页后返回）：同坐标可能是不同语义，不拦。
    if last_fp and current_fp and last_fp != current_fp:
        return None
    step_cursor = ctx.get("step_cursor")
    tap_epoch = int(getattr(step_cursor, "tap_epoch", 0) or 0) if step_cursor is not None else 0
    params = ctx.get("params")
    if repeats_last_tap(last, params, tap_epoch=tap_epoch):
        return format_skip_repeat_tap_message(last, params)
    return None


def apply_force_case_expectation(params: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """校验阶段把用例同号预期塞给 VLM。"""
    out = dict(params or {})
    if "force_case_expectation" not in (ctx.get("guards") or []):
        return out
    if str(ctx.get("cap_id") or "") != "assert_visual":
        return out
    cur = ctx.get("cursor")
    in_check = str(ctx.get("phase") or "") == "check"
    case_expected = str(getattr(cur, "expected", "") or "").strip() if cur else ""
    model_expected = str(out.get("expectation") or out.get("expected") or "").strip()
    if in_check and case_expected:
        instr = str(getattr(cur, "instruction", "") or "").strip() if cur else ""
        from mino_nexus.loop.step_pointer import enrich_assert_expectation

        out["expectation"] = enrich_assert_expectation(instr, case_expected)
    elif not model_expected and case_expected:
        instr = str(getattr(cur, "instruction", "") or "").strip() if cur else ""
        from mino_nexus.loop.step_pointer import enrich_assert_expectation

        out["expectation"] = enrich_assert_expectation(instr, case_expected)
    return out


GUARDS: dict[str, GuardFn] = {
    "deny_mutate": _guard_deny_mutate,
    "skip_repeat_read_device": _guard_skip_repeat_read_device,
    "skip_repeat_check_run_env": _guard_skip_repeat_check_run_env,
    "skip_repeat_get_app_version": _guard_skip_repeat_get_app_version,
    "skip_repeat_launch_app": _guard_skip_repeat_launch_app,
    "skip_recover_bring_when_foreground": _guard_skip_recover_bring_when_foreground,
    "skip_recover_screen_when_display_guard": _guard_skip_recover_screen_when_display_guard,
    "skip_repeat_fsm_open_loop": _guard_skip_repeat_fsm_open_loop,
    "skip_repeat_fsm_declined": _guard_skip_repeat_fsm_declined,
    "block_fsm_off_step_target": _guard_block_fsm_off_step_target,
    "block_fsm_logged_in_session_drift": _guard_block_fsm_logged_in_session_drift,
    "skip_repeat_clear_app_cache": _guard_skip_repeat_clear_app_cache,
    "skip_repeat_satisfied_step_action": _guard_skip_repeat_satisfied_step_action,
    "block_idle_wait_in_do": _guard_block_idle_wait_in_do,
    "swipe_direction_vs_instruction": _guard_swipe_direction_vs_instruction,
    "skip_repeat_swipe_stuck": _guard_skip_repeat_swipe_stuck,
    "skip_repeat_structural_cap": _guard_skip_repeat_structural_cap,
    "prep_clear_before_launch": _guard_prep_clear_before_launch,
    "action_fuse": _guard_action_fuse,
    "skip_repeat_tap": _guard_skip_repeat_tap,
    "limit_advise_recovery": _guard_limit_advise_recovery,
    "limit_recovery_retry": _guard_limit_recovery_retry,
    "stuck_alternation": _guard_stuck_alternation,
    "block_login_after_guest": _guard_block_login_after_guest,
    "block_login_flow_unless_step_scope": _guard_block_login_flow_unless_step_scope,
    "block_mutate_when_thought_done": _guard_block_mutate_when_thought_done,
    "block_prep_guest_mine_tab": _guard_block_prep_guest_mine_tab,
    "block_prep_login_when_logged_in_required": _guard_block_prep_login_when_logged_in_required,
    "block_back_without_nav_back_semantics": _guard_block_back_without_nav_back_semantics,
    "block_do_after_step_goal": _guard_block_do_after_step_goal,
    "require_sms_send_before_otp": _guard_require_sms_send_before_otp,
    "block_repeat_email_tab": _guard_block_repeat_email_tab,
    "block_repeat_continue_submit": _guard_block_repeat_continue_submit,
    "require_otp_before_login_tap": _guard_require_otp_before_login_tap,
    "exec_script_params": _guard_exec_script_params,
    "require_do_work": _guard_require_do_work,
    "block_assert_in_do": _guard_block_assert_in_do,
    "require_session": _guard_require_session,
    "force_case_expectation": lambda _ctx: None,
}

AdvancerFn = Callable[[dict[str, Any]], bool]


def _advancer_signal_done(_ctx: dict[str, Any]) -> bool:
    return True


def _advancer_assert_pass(ctx: dict[str, Any]) -> bool:
    cursor = ctx.get("cursor")
    return bool(getattr(cursor, "step_checked", False))


ADVANCERS: dict[str, AdvancerFn] = {
    "signal_done": _advancer_signal_done,
    "assert_pass": _advancer_assert_pass,
}

PROVIDERS: dict[str, str] = {
    "goal": "planner.decide_next_action",
    "checkpoints_block": "step_pointer.StepCursor.prompt_block",
    "device_brief_json": "run_context.RunContext.to_prompt_brief",
    "menu_json": "runtime.menu.available_menu_brief",
    "history_block": "agent_loop._history",
    "hierarchy_text": "loop.inspections",
    "session_block": "loop.inspections",
    "knowledge_hint": "loop.inspections.match_step_knowledge",
    "knowledge_body": "loop.inspections.expand_named_knowledge",
    "user_payload": "roles_catalog.chat_with_role",
}


def run_guards(names: list[str], ctx: dict[str, Any]) -> Optional[str]:
    from mino_nexus.loop.dispatch_gate import first_block_reason

    return first_block_reason(names, ctx, guards=GUARDS)


def run_guards_verdict(names: list[str], ctx: dict[str, Any]):
    from mino_nexus.loop.dispatch_gate import evaluate_guards

    return evaluate_guards(names, ctx, guards=GUARDS)


__all__ = [
    "GUARDS",
    "ADVANCERS",
    "PROVIDERS",
    "INSPECTION_ATS",
    "run_guards",
    "apply_force_case_expectation",
]
