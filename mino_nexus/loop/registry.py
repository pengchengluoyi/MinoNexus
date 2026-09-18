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
        if not ok_fam and need_int.intersection({"swipe_gesture", "nav_tab"}):
            return f"本步操作未做完，不能 signal_done。{fam_msg}"
    elif not ok_fam:
        return f"本步操作未做完，不能 signal_done。{fam_msg}"
    ops = int(getattr(step_cursor, "step_ops", 0) or 0)
    if ops > 0:
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
    if str(ctx.get("phase") or "") != "do":
        return None
    thought = str(ctx.get("decision_thought") or "")
    if not re.search(r"signal_done|本步.{0,8}完成|操作.{0,6}完成|应收工", thought, re.I):
        return None
    cap = str(ctx.get("cap_id") or "")
    if not cap or cap.startswith("signal_") or cap in ("wait_ms",):
        return None
    if cap.startswith("recover_"):
        return None
    return (
        "模型思考已结论本步应收工（signal_done），与实际 capability 不一致。"
        f"请 decision.status=done，勿再 {cap}。"
    )


def _guard_block_login_flow_unless_step_scope(ctx: dict[str, Any]) -> Optional[str]:
    if str(ctx.get("phase") or "") != "do":
        return None
    cap = str(ctx.get("cap_id") or "")
    if not cap or cap.startswith("recover_") or cap.startswith("signal_"):
        return None
    cur = ctx.get("cursor")
    instr = str(getattr(cur, "instruction", "") or "").strip() if cur else ""
    from mino_nexus.loop.step_contract import instruction_allows_login_flow

    if instruction_allows_login_flow(
        instr,
        login_module_case=bool(ctx.get("login_module_case")),
    ):
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
    if _history_cap_passed(list(ctx.get("history_lines") or []), "request_sms_code"):
        return None
    blob = f"{ctx.get('session_block') or ''}\n{instr}"
    if not re.search(r"验证码|短信|sms|otp", blob, re.I):
        return None
    return (
        "须先 request_sms_code 成功发送验证码，再 get_otp / 填验证码。"
        "禁止跳过发送步骤。"
    )


def _guard_require_session(ctx: dict[str, Any]) -> Optional[str]:
    if str(ctx.get("phase") or "") != "prep":
        return None
    if str(ctx.get("intent") or "") != "signal_done":
        return None
    from mino_nexus.loop.session_ensure import session_mismatch_reason

    scene = ctx.get("case_scene") if isinstance(ctx.get("case_scene"), dict) else {}
    return session_mismatch_reason(
        scene=scene,
        session_block=str(ctx.get("session_block") or ""),
    )


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
    return gate.check(
        phase=str(ctx.get("phase") or ""),
        cap_id=cap_id,
        params=dict(ctx.get("params") or {}),
        screen_fp=str(ctx.get("screen_fp") or ""),
        has_get_otp=_menu_has_cap(ctx, "get_otp"),
        leased=_leased_account(ctx),
        has_hitl=_menu_has_hitl(ctx),
    )


def _guard_skip_repeat_check_run_env(ctx: dict[str, Any]) -> Optional[str]:
    if str(ctx.get("phase") or "") != "prep":
        return None
    if str(ctx.get("cap_id") or "") != "check_run_env":
        return None
    brief = str(ctx.get("run_env_brief") or "").strip()
    if not brief:
        return None
    return (
        f"已拒绝重复 check_run_env（本任务已确认：{brief[:80]}）。"
        f"请直接 signal_done 结束前置；后续用例执行会沿用该环境。"
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
    for line in reversed(history or []):
        text = str(line or "")
        if needle not in text:
            continue
        if "→ pass" in text or "→ skipped:" in text:
            return True
    return False


def _guard_skip_repeat_launch_app(ctx: dict[str, Any]) -> Optional[str]:
    cap = str(ctx.get("cap_id") or "")
    if cap not in ("launch_app", "open_app", "open_url"):
        return None
    if str(ctx.get("app_foreground") or "") == "yes":
        return (
            "已拒绝重复 launch_app：probe 显示被测 App 已在前台。"
            "请继续本步后续操作（如点 Tab），勿再次打开应用。"
        )
    if bool(ctx.get("app_launch_confirmed")):
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
    return (
        f"本步已成功执行过 {cap}，请勿重复。"
        "若屏上已达成本步意图请 signal_done；若未达成请 tap_element 或检查 hierarchy。"
    )


def _guard_skip_repeat_satisfied_step_action(ctx: dict[str, Any]) -> Optional[str]:
    """仅当无明确业务意图时，用动作族次数限制超额 swipe/tap（兼容旧步骤）。"""
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
    return f"本步 {fam} 次数已达标（{got}/{need_n}），请 signal_done，勿重复 {cap}。"


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
    "skip_repeat_clear_app_cache": _guard_skip_repeat_clear_app_cache,
    "skip_repeat_satisfied_step_action": _guard_skip_repeat_satisfied_step_action,
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
    "block_do_after_step_goal": _guard_block_do_after_step_goal,
    "require_sms_send_before_otp": _guard_require_sms_send_before_otp,
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
    for name in names or []:
        fn = GUARDS.get(str(name or "").strip())
        if fn is None:
            continue
        reason = fn(ctx)
        if reason:
            return reason
    return None


__all__ = [
    "GUARDS",
    "ADVANCERS",
    "PROVIDERS",
    "INSPECTION_ATS",
    "run_guards",
    "apply_force_case_expectation",
]
