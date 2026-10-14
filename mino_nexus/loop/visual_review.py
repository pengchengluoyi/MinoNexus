"""看图的点击当回合记 pass，输入等下一张决策图确认。

下一回合的同一次看图执行同时判断上一步有没有完成，并返回坐标。
上一步没完成时不另开规划：把那条 pass 改回 in_progress，用这次返回的坐标直接做。
DOM 仍用节点证据回看。
"""
from __future__ import annotations

from typing import Any

_REVIEW_CAPS = frozenset({
    "tap_element",
    "tap_xy",
    "input_text",
    "press_key",
    "swipe_direction",
    "reload_page",
})
_INTERNAL_CAPS = frozenset({
    "lease_account",
    "get_otp",
    "get_phone",
    "clear_app_cache",
    "wait_ms",
    "wait_screen_ready",
    "confirm_login_state",
    "release_account",
    "relogin",
})
_FOCUS_INPUT_PAIRS = {
    "account_field": "account_fill",
    "otp_field": "otp_fill",
}


def _visible_expected(
    capability_id: str, milestone_id: str, title: str, step_expected: str = "",
) -> str:
    """检查单写画面上能看见的结果，不写「已完成」。

    step_expected 是用例本步声明的达成信号；通用 hook 里程碑（无专属文案）必须以它为准，
    否则回看只会按「标题的结果已出现」判，把「点赞→拉起登录页」这类预期跳转误判为走偏。
    """
    mid = str(milestone_id or "")
    name = str(title or "")
    if mid == "interrupt_accept_consent":
        return "应用协议弹窗已关闭，被遮挡页面已经可见"
    if mid == "login_entry" or "登录入口" in name:
        return "登录页已经打开"
    if mid in ("account_field", "otp_field", "prep_tap_input_box") or "聚焦" in name:
        return "输入框已聚焦，可以填写"
    if mid == "send_code":
        return "本次发码已被接受：出现倒计时、发送成功提示，或发送按钮变为已发送。验证码框本来就能填，不能算发码成功。"
    if mid == "legal_consent":
        return "协议复选框已处于勾选状态（打钩/高亮），登录按钮变为可点"
    if mid == "submit_login":
        return "登录提交后的画面已经出现"
    if capability_id == "input_text":
        return "框内看得到本次填写的内容"
    if capability_id == "swipe_direction":
        return "画面相对点击前发生了位移"
    if capability_id == "reload_page":
        return "页面已换成刷新后的画面"
    if step_expected and capability_id == "tap_element":
        return f"{step_expected}（以本步声明的结果为准，画面已跳转到该结果即视为通过）"
    if name:
        return f"{name}的结果已经出现在画面上"
    return "该位置附近出现了这一步要的变化"


def _check_sheet(
    capability_id: str,
    params: dict[str, Any],
    summary: str,
    *,
    milestone_title: str = "",
    milestone_id: str = "",
    step_expected: str = "",
) -> dict[str, str]:
    target = params.get("target") if isinstance(params.get("target"), dict) else {}
    field = str(params.get("field") or "").strip()
    role = str(target.get("role") or "").strip()
    if not role:
        if (
            field in ("email", "login_email", "sms_code", "验证码", "otp", "phone", "password")
            or milestone_id in ("account_field", "otp_field", "prep_tap_input_box")
            or "聚焦" in str(milestone_title or "")
        ):
            role = "input"
        elif capability_id == "tap_element":
            role = "button"
    title = str(milestone_title or "").strip()
    expected = str(params.get("text") or "").strip()
    if capability_id != "input_text" or not expected:
        if not expected or capability_id == "tap_element":
            expected = _visible_expected(capability_id, milestone_id, title, step_expected)
        elif not expected:
            expected = title or str(summary or "")[:80]
    return {
        "x": str(params.get("x") if params.get("x") is not None else ""),
        "y": str(params.get("y") if params.get("y") is not None else ""),
        "role": role,
        "field": field,
        "expected": expected[:200],
    }


def _row_caps(row: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for key in ("device_cap", "hook_cap", "cap"):
        val = str(row.get(key) or "").strip()
        if val:
            out.add(val)
    return out


def _input_focus_pair(cursor: Any) -> tuple[dict[str, Any], dict[str, Any]] | None:
    from mino_nexus.loop.milestone_orchestrator import in_progress_milestone
    from mino_nexus.loop.milestones import read_state

    state = read_state(cursor)
    active = in_progress_milestone(state)
    if not active:
        return None
    active_id = str(active.get("id") or "")
    rows = [row for row in state.get("milestones") or [] if isinstance(row, dict)]
    for index, focus in enumerate(rows[:-1]):
        focus_id = str(focus.get("id") or "")
        input_id = _FOCUS_INPUT_PAIRS.get(focus_id)
        following = rows[index + 1]
        if not input_id or str(following.get("id") or "") != input_id:
            continue
        if "input_text" not in _row_caps(following):
            continue
        if str(following.get("status") or "") in ("pass", "skipped", "failed"):
            continue
        if active_id == focus_id or (
            active_id == input_id
            and str(focus.get("status") or "") in ("pending", "pass", "skipped")
        ):
            return focus, following
    return None


def input_focus_milestone_id(cursor: Any) -> str:
    pair = _input_focus_pair(cursor)
    return str(pair[0].get("id") or "") if pair else ""


def input_focus_menu_ids(cursor: Any, ctx: Any) -> set[str] | None:
    from mino_nexus.action_space.scheme import action_scheme

    if action_scheme(ctx) != "visual":
        return None
    pair = _input_focus_pair(cursor)
    if pair is None:
        return None
    focus, following = pair
    waits = {"wait_screen_ready", "wait_ms"}
    from mino_nexus.action_space.visual import bind_focus_ref

    # 聚焦里程碑正在做、还没有本会话的聚焦点击：只能点，不许直接写入（否则会撞 _focus_unresolved 硬阻塞）。
    if focus.get("status") == "in_progress" and following.get("status") == "pending":
        unresolved = bind_focus_ref(
            {"target_mode": "current_focus", "field": (following.get("params") or {}).get("field", "")}, ctx,
        ).get("_focus_unresolved")
        if unresolved and not has_visual_prior(cursor):
            return {"tap_element"} | waits
    if following.get("status") != "in_progress":
        return None
    hook = str(following.get("hook_cap") or "")
    if hook and hook != "input_text" and not (following.get("lease_ready") or following.get("otp_ready")):
        return None

    params = following.get("params") if isinstance(following.get("params"), dict) else {}
    focus_input = bind_focus_ref({"target_mode": "current_focus", "field": params.get("field", "")}, ctx)
    if focus_input.get("_focus_unresolved") and not has_visual_prior(cursor):
        return {"tap_element"} | waits
    if has_visual_prior(cursor):
        return {"input_text", "tap_element"} | waits
    if following.get("input_refocus_required"):
        return {"tap_element"} | waits
    return {"input_text"} | waits


def guard_input_focus_action(cursor: Any, ctx: Any, capability_id: str, *, writer: Any = None) -> str:
    allowed = input_focus_menu_ids(cursor, ctx)
    if allowed is None:
        return "none"
    pair = _input_focus_pair(cursor)
    if pair is None:
        return "none"
    _, following = pair
    if capability_id in allowed:
        if following.pop("input_action_reasks", None):
            setattr(cursor, "correction_hint", "")
        return "none"
    attempts = int(following.get("input_action_reasks") or 0) + 1
    following["input_action_reasks"] = attempts
    hint = f"{following.get('id')} 当前仅允许 {', '.join(sorted(allowed))}；本次动作未派发。"
    setattr(cursor, "correction_hint", hint)
    if writer:
        writer.append(
            "contract/blocked" if attempts > 1 else "contract/reask",
            {
                "reason_code": "input_action_not_allowed",
                "capability_id": capability_id,
                "allowed_capabilities": sorted(allowed),
                "execution_started": False,
                "attempt": attempts,
            },
        )
    if attempts > 1:
        setattr(cursor, "semantic_stop", hint)
        return "stop"
    return "reask"


def recover_input_focus(
    cursor: Any,
    ctx: Any,
    *,
    reason: str,
    execution_started: bool = False,
    writer: Any = None,
) -> str:
    """每个填写里程碑最多恢复一次；重新点击后仍须输入结果回看。"""
    from mino_nexus.action_space.scheme import action_scheme
    from mino_nexus.loop.milestones import read_state, write_state

    if action_scheme(ctx) != "visual":
        return "none"
    pair = _input_focus_pair(cursor)
    if pair is None:
        return "none"
    focus, following = pair
    if following.get("status") != "in_progress" and not (
        following.get("status") == "pending" and focus.get("status") == "in_progress"
    ):
        return "none"
    attempts = int(following.get("input_recovery_attempts") or 0)
    # A write that may have started cannot be automatically replayed.
    exhausted = attempts >= 1 or execution_started
    if writer:
        writer.append(
            "input/recovery_exhausted" if exhausted else "input/recovery",
            {
                "milestone_id": str(following.get("id") or ""),
                "focus_milestone_id": str(focus.get("id") or ""),
                "reason": reason[:300],
                "retryable": not exhausted,
                "execution_started": execution_started,
                "attempt": attempts if exhausted else attempts + 1,
                "limit": 1,
            },
        )
    setattr(ctx, "focus_action_ref", "")
    setattr(ctx, "focus_action_field", "")
    setattr(cursor, "visual_prior", None)
    setattr(cursor, "pending_visual_review", None)
    setattr(cursor, "visual_turn_pending", False)
    if exhausted:
        setattr(cursor, "semantic_stop", f"{following.get('id')} 输入恢复一次后仍未完成，已停止：{reason[:150]}")
        return "stop"
    following["input_recovery_attempts"] = attempts + 1
    following["input_refocus_required"] = True
    following.pop("input_action_reasks", None)
    write_state(cursor, read_state(cursor))
    setattr(cursor, "correction_hint", f"{following.get('id')} 输入未完成，需重新定位输入框后再填写：{reason[:150]}")
    return "retry"


def defer_input_focus(
    cursor: Any,
    ctx: Any,
    *,
    capability_id: str,
    writer: Any = None,
) -> bool:
    """聚焦点击只允许尝试输入，聚焦与填写在输入结果确认后一起完成。"""
    from mino_nexus.action_space.scheme import action_scheme
    from mino_nexus.loop.milestones import read_state, write_state

    if capability_id != "tap_element" or action_scheme(ctx) != "visual":
        return False
    pair = _input_focus_pair(cursor)
    if pair is None:
        return False
    focus, following = pair
    focus_id = str(focus.get("id") or "")
    input_id = str(following.get("id") or "")
    state = read_state(cursor)
    focus["status"] = "pending"
    focus["verify_with_milestone"] = input_id
    following["status"] = "in_progress"
    if following.pop("input_refocus_required", False):
        setattr(cursor, "correction_hint", "")
    write_state(cursor, state)
    setattr(cursor, "visual_prior", None)
    setattr(cursor, "pending_visual_review", None)
    if writer:
        writer.append(
            "review/deferred",
            {
                "milestone_id": focus_id,
                "capability_id": capability_id,
                "verify_with_milestone": input_id,
                "verification_status": "pending",
                "reason": "verify_input_result",
            },
        )
    return True


def defer_visual_pass(
    cursor: Any,
    *,
    capability_id: str,
    params: dict[str, Any] | None,
    summary: str,
    writer: Any = None,
    ctx: Any = None,
) -> bool:
    """看图的点击、输入、滑动、刷新都等下一回合看图裁定。DOM 仍当回合收口。"""
    cap = str(capability_id or "").strip()
    if cap in _INTERNAL_CAPS or cap not in _REVIEW_CAPS:
        return False
    from mino_nexus.action_space.scheme import action_scheme

    if action_scheme(ctx) != "visual":
        return False
    if defer_input_focus(cursor, ctx, capability_id=cap, writer=writer):
        return True
    from mino_nexus.loop.milestone_orchestrator import in_progress_milestone
    from mino_nexus.loop.milestones import read_state

    row = in_progress_milestone(read_state(cursor))
    if not isinstance(row, dict):
        return False
    from mino_nexus.loop.milestone_orchestrator import milestone_exec_menu_ids

    if cap not in milestone_exec_menu_ids(cursor, row, ctx):
        return False
    mid = str(row.get("id") or "")
    kept = dict(params or {})
    pending = {
        "milestone_id": mid,
        "attempt_ref": f"{getattr(writer, 'session_id', '')}:{getattr(writer, 'turn', 0)}" if writer else "",
        "dispatch_turn": int(getattr(writer, "turn", 0) or 0),
        "capability_id": cap,
        "params": kept,
        "summary": str(summary or "")[:400],
        "landed_labels": _landed_at_dispatch(ctx, cap, kept, str(summary or "")),
    }
    setattr(cursor, "pending_visual_review", pending)
    remember_visual_prior(
        cursor,
        milestone_id=mid,
        capability_id=cap,
        params=kept,
        summary=pending["summary"],
    )
    prior = getattr(cursor, "visual_prior", None)
    if isinstance(prior, dict):
        prior["attempt_ref"] = pending["attempt_ref"]
        prior["dispatch_turn"] = pending["dispatch_turn"]
    if writer:
        sheet = getattr(cursor, "visual_prior", None)
        sheet = sheet if isinstance(sheet, dict) else {}
        writer.append(
            "review/dispatched",
            {
                "milestone_id": mid,
                "attempt_ref": pending["attempt_ref"],
                "capability_id": cap,
                "summary": pending["summary"],
                "x": str(sheet.get("x") or ""),
                "y": str(sheet.get("y") or ""),
                "role": str(sheet.get("role") or ""),
                "field": str(sheet.get("field") or ""),
                "expected": "[已脱敏输入值]" if cap == "input_text" else str(sheet.get("expected") or "")[:200],
            },
        )
    return True


def _milestone_title(cursor: Any, milestone_id: str) -> str:
    from mino_nexus.loop.milestones import read_state

    row = _row_by_id(read_state(cursor), milestone_id) or {}
    return str(row.get("title") or "").strip()


def remember_visual_prior(
    cursor: Any,
    *,
    milestone_id: str,
    capability_id: str,
    params: dict[str, Any] | None,
    summary: str,
) -> None:
    """下一张决策图要回看的上一步。带落点、元素类型、字段和预期。"""
    kept = params if isinstance(params, dict) else {}
    title = _milestone_title(cursor, str(milestone_id or ""))
    step_expected = ""
    if getattr(cursor, "phase", "") == "do":
        node = cursor.current() if callable(getattr(cursor, "current", None)) else None
        step_expected = str(getattr(node, "expected", "") or "").strip()[:120]
    sheet = _check_sheet(
        str(capability_id or ""),
        kept,
        str(summary or ""),
        milestone_title=title,
        milestone_id=str(milestone_id or ""),
        step_expected=step_expected,
    )
    setattr(
        cursor,
        "visual_prior",
        {
            "milestone_id": str(milestone_id or ""),
            "capability_id": str(capability_id or ""),
            "params": kept,
            "summary": str(summary or "")[:400],
            **sheet,
        },
    )


def has_visual_prior(cursor: Any) -> bool:
    prior = getattr(cursor, "visual_prior", None)
    return isinstance(prior, dict) and bool(str(prior.get("milestone_id") or "").strip())


def same_turn_review_targets(cursor: Any) -> dict[str, Any] | None:
    from mino_nexus.loop.milestones import read_state
    from mino_nexus.loop.milestone_orchestrator import _required_failed_before, milestone_exec_menu_ids
    from mino_nexus.catalog.skill_channel import is_program_caller

    prior = getattr(cursor, "visual_prior", None)
    if not isinstance(prior, dict):
        return None
    mid = str(prior.get("milestone_id") or "")
    state = read_state(cursor)
    if not mid or not _prior_still_current(state, mid):
        return None
    rows = [row for row in state.get("milestones") or [] if isinstance(row, dict)]
    current = next((row for row in rows if row.get("id") == mid), {})
    ctx = getattr(cursor, "run_context", None)
    following = next((row for row in rows
                      if row.get("id") != mid
                      and row.get("status", "pending") in ("pending", "in_progress")
                      and not row.get("verify_with_milestone")
                      and not _required_failed_before(rows, row)), None)

    def target(row):
        if not row:
            return None
        caps = milestone_exec_menu_ids(cursor, row, ctx)
        return {"milestone_id": str(row.get("id") or ""),
                "title": str(row.get("title") or ""), "capabilities": sorted(caps)}

    failed = target(current)
    if prior.get("capability_id") == "input_text":
        pair = _input_focus_pair(cursor)
        if pair:
            failed = target(pair[0])
            failed["capabilities"] = ["tap_element"]
    passed = target(following)
    if passed and passed["capabilities"] and all(is_program_caller(cap) for cap in passed["capabilities"]):
        passed["program_owned"] = True
        passed["program_capabilities"] = list(passed["capabilities"])
        passed["capabilities"] = []
    return {"review_milestone_id": mid, "if_pass": passed, "if_fail": failed}


def prior_action_payload(cursor: Any) -> dict[str, str] | None:
    prior = getattr(cursor, "visual_prior", None)
    if not isinstance(prior, dict):
        return None
    mid = str(prior.get("milestone_id") or "").strip()
    if not mid:
        return None
    return {
        "milestone_id": mid,
        "attempt_ref": str(prior.get("attempt_ref") or ""),
        "capability_id": str(prior.get("capability_id") or ""),
        "x": str(prior.get("x") or ""),
        "y": str(prior.get("y") or ""),
        "role": str(prior.get("role") or ""),
        "field": str(prior.get("field") or ""),
        "expected": str(prior.get("expected") or "")[:200],
        "summary": str(prior.get("summary") or "")[:400],
    }


def apply_same_turn_prior(
    cursor: Any,
    ctx: Any,
    decision: Any,
    *,
    writer: Any = None,
    in_prep: bool = False,
    in_check: bool = False,
) -> str:
    """同一张决策图上的回看，区分补答、输出契约阻塞和实际画面失败。

    redo：上一步没完成，里程碑改回 in_progress，本回合用返回的坐标继续做。
    stop：连续回看未通过。
    contract_stop：连续没有合法回看输出，尚不能判定页面成败。
    settled：上一步已完成，这次返回的还是同一步，不再下发。
    confirmed：上一步已完成，这次返回的是下一步。
    """
    from mino_nexus.action_space.scheme import action_scheme
    from mino_nexus.catalog.tool_schema import CONFIRM_ACTION_RESULT

    action = getattr(decision, "action", None)
    action_cap = str(getattr(action, "capability_id", "") or "")
    confirmation = action_cap == CONFIRM_ACTION_RESULT
    invalid_confirmation = confirmation and (
        str(getattr(decision, "prior_status", "") or "") != "pass"
        or not str(getattr(decision, "thought", "") or "").strip()
    )

    if action_scheme(ctx) != "visual":
        return "reask" if confirmation else "none"
    prior = getattr(cursor, "visual_prior", None)
    if not isinstance(prior, dict) or not str(prior.get("milestone_id") or "").strip():
        answered = str(getattr(decision, "prior_status", "") or "").strip()
        if answered:
            if writer:
                writer.append("review/rejected", {"reason": "no_dispatched_action", "prior_status": answered})
            setattr(cursor, "correction_hint", "没有待回看的已派发动作，不能报告 prior_status。")
            return "reask"
        if not isinstance(prior, dict) or not str(prior.get("milestone_id") or "").strip():
            return "none"
    from mino_nexus.loop.milestones import read_state

    mid = str(prior.get("milestone_id") or "")
    if not _prior_still_current(read_state(cursor), mid):
        setattr(cursor, "visual_prior", None)
        setattr(cursor, "pending_visual_review", None)
        if confirmation:
            setattr(cursor, "correction_hint", "待回看的动作已经过期；请按当前里程碑重新决策，不得确认旧动作。")
        return "reask" if confirmation else "none"
    dispatched = int(prior.get("dispatch_turn") or 0)
    observed = int(getattr(writer, "turn", 0) or 0)
    if (dispatched and observed <= dispatched) or not str(getattr(ctx, "decision_screenshot_hash", "") or ""):
        attempts = int(getattr(cursor, "review_observation_missing", 0) or 0) + 1
        setattr(cursor, "review_observation_missing", attempts)
        if writer:
            writer.append("review/hold", {
                "milestone_id": mid, "attempt_ref": str(prior.get("attempt_ref") or ""),
                "reason": "fresh_observation_missing", "count": attempts,
            })
        if attempts >= 2:
            setattr(cursor, "semantic_stop", "动作后未取得新截图，不能裁定回看")
            return "stop"
        setattr(cursor, "correction_hint", "动作后缺少新截图，本轮不能裁定；请重新观察一次。")
        return "reask"
    setattr(cursor, "review_observation_missing", 0)
    raw_status = str(getattr(decision, "prior_status", "") or "").strip()
    if invalid_confirmation:
        raw_status = ""
    if action_cap in ("wait_screen_ready", "wait_ms") and raw_status not in {"pass", "fail"}:
        key = str(prior.get("attempt_ref") or f"{mid}:{dispatched}")
        image_hash = str(getattr(ctx, "decision_screenshot_hash", "") or "")
        budget = getattr(cursor, "review_wait_budget", None) or {}
        if budget.get("attempt_ref") != key:
            budget = {"attempt_ref": key, "count": 0, "same_frame_count": 0}
        budget["count"] += 1
        budget["same_frame_count"] = budget["same_frame_count"] + 1 if budget.get("image_hash") == image_hash else 1
        budget["image_hash"] = image_hash
        cursor.review_wait_budget = budget
        if budget["same_frame_count"] >= 2 or budget["count"] >= 3:
            cursor.semantic_stop = f"{mid} 回看持续等待但没有有效结论，已停止；不能据此判定页面失败。"
            if writer:
                writer.append("review/wait_exhausted", {
                    **budget, "milestone_id": mid, "reason": "review_wait_exhausted",
                })
            return "wait_stop"
        # 加载中的等待不是裁定。检查单留着，下一张稳定画面再给 pass 或 fail。
        if writer:
            writer.append(
                "review/hold",
                {
                    "milestone_id": mid,
                    "capability_id": str(prior.get("capability_id") or ""),
                    "reason": "screen_not_ready",
                    "wait_cap": action_cap,
                    "prior_status": raw_status,
                },
            )
        return "none"
    if raw_status not in {"pass", "fail"}:
        reason = "prior_omitted" if not raw_status else "prior_invalid"
        # 没填不是画面结论。留着检查单再问一次，不要把下一步吃成重做。
        asked = getattr(cursor, "visual_prior_reask", None)
        if not isinstance(asked, dict):
            asked = {}
        if asked.get(mid):
            asked.pop(mid, None)
            setattr(cursor, "visual_prior_reask", asked)
            msg = f"LLM 回看输出契约错误：{mid} 未提供合法的 prior_status，尚未验证，已阻塞。"
            setattr(cursor, "semantic_stop", msg)
            setattr(cursor, "visual_prior", None)
            if writer:
                writer.append(
                    "review/error",
                    {
                        "milestone_id": mid,
                        "reason": reason,
                        "error_kind": "llm_output_contract_error",
                        "redo_already_sent": False,
                    },
                )
            return "contract_stop"
        asked[mid] = True
        setattr(cursor, "visual_prior_reask", asked)
        setattr(
            cursor,
            "correction_hint",
            "本回合因上一动作缺少合法 prior_status 而未下发。请先回看 prior_action，"
            "在工具参数中补 pass 或 fail；只有证据不足时才调用等待工具，不得猜测。",
        )
        if writer:
            writer.append(
                "review/hold",
                {
                    "milestone_id": mid,
                    "capability_id": str(prior.get("capability_id") or ""),
                    "reason": reason,
                },
            )
        return "reask"
    asked = getattr(cursor, "visual_prior_reask", None)
    if isinstance(asked, dict) and mid in asked:
        asked.pop(mid, None)
        setattr(cursor, "visual_prior_reask", asked)
        setattr(cursor, "correction_hint", "")
    status = _normalize_prior_status(raw_status)
    cursor.review_wait_budget = None
    transition = same_turn_review_targets(cursor)
    if status != "pass" and transition and transition.get("if_fail"):
        allowed = transition["if_fail"]["capabilities"]
        if action_cap not in allowed:
            setattr(cursor, "correction_hint", f"上一动作未通过，重做必须使用当前目标允许的能力：{', '.join(allowed)}；本轮未派发。")
            if writer:
                writer.append("contract/reask", {
                    "reason_code": "review_redo_action_mismatch", "milestone_id": mid,
                    "capability_id": action_cap, "allowed_capabilities": allowed,
                    "execution_started": False,
                })
            return "reask"
    if writer:
        writer.append(
            "review/same_turn",
            {
                "milestone_id": mid,
                "capability_id": str(prior.get("capability_id") or ""),
                "prior_status": status,
            },
        )
    if status != "pass":
        if str(prior.get("capability_id") or "") == "input_text":
            recovery = recover_input_focus(
                cursor, ctx, reason="input_result_failed", execution_started=True, writer=writer,
            )
            if recovery != "none":
                if writer:
                    writer.append("review/fail", {
                        "milestone_id": mid, "reason": "input_result_failed",
                        "recovery": recovery,
                    })
                if recovery == "retry" and action_cap == "tap_element":
                    return "redo"
                return "reask" if recovery == "retry" else "stop"
        _rewind(cursor, mid)
        setattr(cursor, "pending_visual_review", None)
        sent = getattr(cursor, "visual_redo_sent", None)
        if not isinstance(sent, dict):
            sent = {}
        already = bool(sent.get(mid))
        # 回看判未达成：同位置再点不能被「重复点击=已满足」直接记 pass（见 focus_turn）。
        setattr(cursor, "visual_failed_milestone", mid)
        hint = _hint(cursor, mid, "画面上上一步还没完成")
        setattr(cursor, "correction_hint", hint)
        if writer:
            writer.append(
                "review/fail",
                {"milestone_id": mid, "reason": "prior_fail", "redo_already_sent": already},
            )
        if already:
            sent.pop(mid, None)
            setattr(cursor, "visual_redo_sent", sent)
            setattr(cursor, "semantic_stop", f"{hint} 重做之后画面仍未达成，已停止。")
            setattr(cursor, "visual_prior", None)
            return "stop"
        return "redo"
    sent = getattr(cursor, "visual_redo_sent", None)
    if isinstance(sent, dict) and mid in sent:
        sent.pop(mid, None)
        setattr(cursor, "visual_redo_sent", sent)
    if getattr(cursor, "visual_failed_milestone", "") == mid:
        setattr(cursor, "visual_failed_milestone", "")
    _clear_streak(cursor, mid)
    setattr(cursor, "semantic_stop", "")
    repeated = _action_repeats(cursor, prior, decision)
    _confirm_visual_prior(
        cursor,
        ctx,
        mid,
        capability_id=str(prior.get("capability_id") or ""),
        writer=writer,
        in_prep=in_prep,
        in_check=in_check,
    )
    setattr(cursor, "visual_prior", None)
    setattr(cursor, "pending_visual_review", None)
    gate = getattr(cursor, "progress_gate", None)
    if gate is not None and hasattr(gate, "note_submilestone_pass"):
        gate.note_submilestone_pass()
    if writer:
        writer.append(
            "review/pass",
            {"milestone_id": mid, "reason": "same_turn", "repeated": repeated},
        )
    if confirmation:
        decision.action = None
        if writer:
            writer.append("review/confirmation", {
                "milestone_id": mid, "outcome": "settled", "execution_started": False,
            })
        return "settled"
    if transition and transition.get("if_pass"):
        from mino_nexus.loop.milestone_orchestrator import in_progress_milestone_id

        following = transition["if_pass"]
        active_id = in_progress_milestone_id(cursor)
        allowed = following["capabilities"]
        if active_id == following["milestone_id"] and following.get("program_owned"):
            return "settled"
        if active_id == following["milestone_id"] and action_cap in allowed:
            if writer:
                writer.append("review/advance", {
                    "from_milestone_id": mid, "to_milestone_id": active_id,
                    "capability_id": action_cap, "same_turn": True,
                })
            return "confirmed"
        setattr(cursor, "correction_hint", f"上一动作已通过，当前里程碑为 {active_id}；本次动作不符合回看通过分支，未派发。")
        if writer:
            writer.append("contract/reask", {
                "reason_code": "review_transition_action_mismatch",
                "from_milestone_id": mid, "to_milestone_id": active_id,
                "capability_id": action_cap, "allowed_capabilities": allowed,
                "execution_started": False,
            })
        return "reask"
    return "settled" if repeated or transition else "confirmed"


def settle_pending_review(
    cursor: Any,
    ctx: Any,
    *,
    writer: Any = None,
    in_prep: bool = False,
    in_check: bool = False,
) -> str:
    """下一回合一开始回看。返回 stop / pass / fail / none。"""
    pending = getattr(cursor, "pending_visual_review", None)
    if not isinstance(pending, dict) or not pending.get("milestone_id"):
        return "none"
    mid = str(pending.get("milestone_id") or "")
    dispatched = int(pending.get("dispatch_turn") or 0)
    if dispatched and writer is not None and int(getattr(writer, "turn", 0) or 0) <= dispatched:
        if writer:
            writer.append("review/hold", {
                "milestone_id": mid, "attempt_ref": str(pending.get("attempt_ref") or ""),
                "reason": "fresh_observation_missing",
            })
        return "none"
    cap = str(pending.get("capability_id") or "")
    params = pending.get("params") if isinstance(pending.get("params"), dict) else {}
    summary = str(pending.get("summary") or "")
    ok, reason, label = _evidence(
        cursor,
        ctx,
        mid,
        cap,
        params,
        summary,
        pending.get("landed_labels") if isinstance(pending.get("landed_labels"), list) else [],
    )
    setattr(cursor, "pending_visual_review", None)
    if ok:
        _clear_streak(cursor, mid)
        setattr(cursor, "semantic_stop", "")
        from mino_nexus.loop.milestones import note_tool_pass_milestone

        noted = dict(params)
        if cap == "tap_element" and label and not str(noted.get("selector_text") or "").strip():
            noted["selector_text"] = label
        note_tool_pass_milestone(
            cursor,
            capability_id=cap,
            milestone_id=mid,
            writer=writer,
            params=noted,
            tool_summary=summary,
            ctx=ctx,
        )
        from mino_nexus.loop.step_phase_fsm import try_transition_after_tool_pass

        try_transition_after_tool_pass(
            cursor, ctx, writer=writer, in_prep=in_prep, in_check=in_check
        )
        if writer:
            writer.append(
                "review/pass",
                {"milestone_id": mid, "capability_id": cap, "reason": reason, "label": label},
            )
        return "pass"
    _rewind(cursor, mid)
    streak = _bump_streak(cursor, mid)
    hint = _hint(cursor, mid, label or reason)
    setattr(cursor, "correction_hint", hint)
    if writer:
        writer.append(
            "review/fail",
            {
                "milestone_id": mid,
                "capability_id": cap,
                "reason": reason,
                "label": label,
                "streak": streak,
            },
        )
        if reason in ("exclude", "unlisted"):
            writer.append(
                "semantic/reject",
                {"milestone_id": mid, "reason": reason, "label": label, "streak": streak},
            )
    if streak >= 2:
        setattr(cursor, "semantic_stop", f"{hint} 连续 {streak} 次回看未通过，已停止。")
        return "stop"
    return "fail"


def _landed_at_dispatch(
    ctx: Any,
    cap: str,
    params: dict[str, Any],
    summary: str,
) -> list[str]:
    from mino_nexus.semantic.intent_surface import landed_labels

    labels = landed_labels(params, summary)
    if cap != "tap_element" or ctx is None:
        return labels
    nodes = [n for n in (getattr(ctx, "nav_hierarchy_nodes", None) or []) if isinstance(n, dict)]
    hit = _node_at_milli(nodes, params.get("x"), params.get("y"), ctx)
    if hit is None:
        return labels
    for label in node_text(hit):
        if label and label not in labels:
            labels.append(label)
    return labels


def _evidence(
    cursor: Any,
    ctx: Any,
    mid: str,
    cap: str,
    params: dict[str, Any],
    summary: str,
    landed: list[str] | None = None,
) -> tuple[bool, str, str]:
    from mino_nexus.loop.milestones import read_state

    row = _row_by_id(read_state(cursor), mid)
    if row is None:
        return False, "missing_milestone", ""
    if cap == "input_text":
        return _input_evidence(ctx, row, params, summary)
    if cap == "tap_element":
        nodes = [n for n in (getattr(ctx, "nav_hierarchy_nodes", None) or []) if isinstance(n, dict)]
        return _tap_evidence(row, params, summary, landed or [], nodes)
    return True, "not_visual", ""


def _input_evidence(
    ctx: Any,
    row: dict[str, Any],
    params: dict[str, Any],
    summary: str,
) -> tuple[bool, str, str]:
    from mino_nexus.loop.channel_observation import attach_input_readback
    from mino_nexus.loop.flow_block_exit import milestone_device_exit_ok

    checked = attach_input_readback(ctx, params)
    ok, status, reason = milestone_device_exit_ok(
        row,
        capability_id="input_text",
        field=str(checked.get("field") or ""),
        summary=summary,
        params=checked,
    )
    got = str(checked.get("_field_value") or "")
    if ok and status == "pass" and reason == "field_value_readback":
        return True, reason, got[:80]
    if not ok:
        return False, reason or status or "input_unread", got[:80]
    return False, "input_unverified", got[:80]


def _tap_evidence(
    row: dict[str, Any],
    params: dict[str, Any],
    summary: str,
    landed: list[str],
    nodes: list[dict[str, Any]] | None = None,
) -> tuple[bool, str, str]:
    from mino_nexus.semantic.intent_surface import classify_label, landed_labels, row_has_surface

    if not row_has_surface(row):
        return True, "no_surface", ""
    labels: list[str] = []
    for label in list(landed) + landed_labels(params, summary):
        if label and label not in labels:
            labels.append(label)
    excluded = ""
    matched = ""
    for label in labels:
        kind, phrase = classify_label(label, row, nodes)
        if kind == "exclude" and not excluded:
            excluded = phrase or label
        elif kind == "match" and not matched:
            matched = phrase or label
    if excluded:
        return False, "exclude", excluded
    if matched:
        return True, "landed", matched
    return False, "unlisted", labels[0] if labels else ""


def node_text(node: dict[str, Any]) -> list[str]:
    from mino_nexus.semantic.intent_surface import node_labels

    return node_labels(node)


def _node_at_milli(nodes: list[dict[str, Any]], x: Any, y: Any, ctx: Any) -> dict[str, Any] | None:
    try:
        fx, fy = float(x), float(y)
    except (TypeError, ValueError):
        return None
    wh = getattr(ctx, "viewport_wh", None) or (0, 0)
    width, height = int(wh[0] or 0), int(wh[1] or 0)
    if width <= 0 or height <= 0 or fx > 1000 or fy > 1000:
        px, py = fx, fy
    else:
        px, py = fx / 1000.0 * width, fy / 1000.0 * height
    from mino_nexus.loop.ui_consent import _bounds, _size

    best: dict[str, Any] | None = None
    best_area = 0
    for node in nodes:
        if node.get("clickable") is False:
            continue
        bounds = _bounds(node)
        if len(bounds) < 4:
            continue
        if not (bounds[0] <= px <= bounds[2] and bounds[1] <= py <= bounds[3]):
            continue
        w, h = _size(bounds)
        area = w * h
        if area <= 0:
            continue
        if best is None or area < best_area:
            best = node
            best_area = area
    return best


def _row_by_id(state: dict[str, Any], milestone_id: str) -> dict[str, Any] | None:
    for row in state.get("milestones") or []:
        if isinstance(row, dict) and str(row.get("id") or "") == milestone_id:
            return row
    return None


def _rewind(cursor: Any, milestone_id: str) -> None:
    from mino_nexus.loop.milestones import read_state, write_state

    state = read_state(cursor)
    ms = state.get("milestones") if isinstance(state.get("milestones"), list) else []
    for row in ms:
        if not isinstance(row, dict):
            continue
        rid = str(row.get("id") or "")
        status = str(row.get("status") or "pending").strip().lower()
        if status == "skipped":
            continue
        if rid == milestone_id:
            row["status"] = "in_progress"
        elif status == "in_progress":
            row["status"] = "pending"
    state["milestones"] = ms
    write_state(cursor, state)


def _streaks(cursor: Any) -> dict[str, int]:
    raw = getattr(cursor, "semantic_reject_streak", None)
    if not isinstance(raw, dict):
        return {}
    return {str(k): int(v or 0) for k, v in raw.items()}


def _bump_streak(cursor: Any, milestone_id: str) -> int:
    streaks = _streaks(cursor)
    streaks[milestone_id] = int(streaks.get(milestone_id) or 0) + 1
    setattr(cursor, "semantic_reject_streak", streaks)
    return streaks[milestone_id]


def _clear_streak(cursor: Any, milestone_id: str) -> None:
    streaks = _streaks(cursor)
    streaks.pop(milestone_id, None)
    setattr(cursor, "semantic_reject_streak", streaks)


def _normalize_prior_status(raw: Any) -> str:
    """上一回合只有通过或失败。没填、或旧的 done/pending，都收成这两个。"""
    text = str(raw or "").strip().lower()
    if text in ("pass", "done", "complete", "completed", "ok", "true"):
        return "pass"
    return "fail"


def _prior_still_current(state: dict[str, Any], milestone_id: str) -> bool:
    """这一步还没通过，回看就还有效。后面有别的步骤通过，不能把这次回看丢掉。"""
    for row in state.get("milestones") or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("id") or "") != milestone_id:
            continue
        return str(row.get("status") or "").strip().lower() not in ("pass", "skipped", "failed")
    return False


def _action_repeats(cursor: Any, prior: dict[str, Any], decision: Any) -> bool:
    """仅拦截同一能力且目标相同的再次下发。"""
    action = getattr(decision, "action", None)
    cap = str(getattr(action, "capability_id", "") or "")
    if not cap or cap != str(prior.get("capability_id") or ""):
        return False
    current_mid = str(getattr(decision, "milestone_id", "") or "")
    prior_mid = str(prior.get("milestone_id") or "")
    if current_mid and prior_mid and current_mid != prior_mid:
        return False
    previous = prior.get("params") if isinstance(prior.get("params"), dict) else {}
    current = getattr(action, "params", None)
    current = current if isinstance(current, dict) else {}
    matched_target = False
    for key in ("control_id", "selector_text", "field", "target"):
        old = previous.get(key)
        new = current.get(key)
        if old is not None and new is not None and old != new:
            return False
        matched_target = matched_target or (old is not None and new is not None)
    if all(key in previous and key in current for key in ("x", "y")):
        try:
            same_point = all(abs(float(previous[key]) - float(current[key])) <= 24 for key in ("x", "y"))
        except (TypeError, ValueError):
            same_point = False
        if not same_point:
            return False
        return True
    if matched_target:
        return True
    # 只有能力 ID 一样不足以证明目标相同，下一步可能仍需点击另一个控件。
    return False


def _confirm_visual_prior(
    cursor: Any,
    ctx: Any,
    milestone_id: str,
    *,
    capability_id: str = "",
    writer: Any = None,
    in_prep: bool = False,
    in_check: bool = False,
) -> None:
    """看图确认上一步已完成。不读层级，避免空节点把已输入判成未写入。"""
    from mino_nexus.loop.milestone_orchestrator import advance_in_progress_focus
    from mino_nexus.loop.milestones import read_state, write_state

    state = read_state(cursor)
    ms = state.get("milestones") if isinstance(state.get("milestones"), list) else []
    changed = False
    for row in ms:
        if not isinstance(row, dict) or str(row.get("id") or "") != milestone_id:
            continue
        if str(row.get("status") or "").strip().lower() in ("pass", "skipped", "failed"):
            break
        row["status"] = "pass"
        row["evidence"] = "visual_same_turn"
        changed = True
        break
    if changed:
        if capability_id == "input_text":
            for row in ms:
                if not isinstance(row, dict) or row.get("verify_with_milestone") != milestone_id:
                    continue
                if str(row.get("status") or "") != "pending":
                    continue
                row["status"] = "pass"
                row["evidence"] = f"input_result:{milestone_id}"
                row.pop("verify_with_milestone", None)
                if writer:
                    writer.append(
                        "review/pass",
                        {
                            "milestone_id": str(row.get("id") or ""),
                            "reason": "input_result",
                            "verified_by_milestone": milestone_id,
                        },
                    )
        state["milestones"] = ms
        write_state(cursor, state)
        if milestone_id == "send_code":
            from mino_nexus.loop.login_verification import record_verification_send

            record_verification_send(ctx)
            intents = getattr(cursor, "step_intents_done", None)
            if isinstance(intents, set):
                intents.add("sms_send")
            cursor.login_post_sms_pending = True
            cursor.login_post_sms_stall = 0
        advance_in_progress_focus(cursor, writer=writer)
        from mino_nexus.loop.step_phase_fsm import try_transition_after_tool_pass

        try_transition_after_tool_pass(
            cursor, ctx, writer=writer, in_prep=in_prep, in_check=in_check
        )


def _hint(cursor: Any, milestone_id: str, label: str) -> str:
    from mino_nexus.loop.milestones import read_state

    row = _row_by_id(read_state(cursor), milestone_id) or {}
    allow = "、".join(str(x) for x in (row.get("match") or [])[:8])
    deny = "、".join(str(x) for x in (row.get("exclude") or [])[:8])
    if allow or deny:
        return f"{milestone_id} 只能点 {allow}。不要点 {deny}。这次是「{label or '未知'}」。"
    return f"{milestone_id} 回看未通过（{label or '没有独立证据'}）。"
