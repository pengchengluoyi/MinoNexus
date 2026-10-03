"""里程碑 Plan / Exec 编排与 in_progress 焦点（见 docs/架构/V2/里程碑-Plan-Exec-状态机.md）。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from mino_nexus.ai.schemas import AgentAction, AgentDecision
from mino_nexus.loop.llm_step_context import step_scope_key
from mino_nexus.loop.milestones import (
    _TERMINAL,
    evaluate_milestones,
    milestone_v1_enabled,
    milestones_empty,
    read_state,
    write_state,
    _sync_scope,
)
from mino_nexus.loop.vision_flags import vision_exec_v1_enabled, vision_plan_v1_enabled


def _milestones_list(state: dict[str, Any]) -> list[dict[str, Any]]:
    return [m for m in (state.get("milestones") or []) if isinstance(m, dict)]


def count_open_milestones(state: dict[str, Any]) -> int:
    n = 0
    for row in _milestones_list(state):
        st = str(row.get("status") or "pending").strip().lower()
        if st in ("pending", "in_progress"):
            n += 1
    return n


def in_progress_milestone(state: dict[str, Any]) -> Optional[dict[str, Any]]:
    for row in _milestones_list(state):
        if str(row.get("status") or "").strip().lower() == "in_progress":
            return row
    return None


def in_progress_milestone_id(cursor: Any) -> str:
    row = in_progress_milestone(read_state(cursor))
    return str(row.get("id") or "").strip() if row else ""


def _mark_model_recovery(cursor: Any, ctx: Any) -> None:
    """平时菜单没有 fsm_navigate。模型卡住，或当前步就是导航时，才放回菜单。"""
    if ctx is None:
        return
    focus = in_progress_milestone(read_state(cursor)) or {}
    hook = str(focus.get("hook_cap") or focus.get("device_cap") or "").strip()
    stuck = int(getattr(cursor, "plan_only_streak", 0) or 0) >= 1
    gate = getattr(cursor, "progress_gate", None)
    steered = bool(gate is not None and int(getattr(gate, "fuse_block_streak", 0) or 0) >= 1)
    setattr(ctx, "allow_model_recovery", hook == "fsm_navigate" or stuck or steered)


def ensure_single_in_progress(cursor: Any, *, writer: Any = None) -> bool:
    """至多一条 in_progress；若无则首条 pending → in_progress。"""
    if not milestone_v1_enabled():
        return False
    state = _sync_scope(cursor, read_state(cursor))
    ms = _milestones_list(state)
    if not ms:
        return False
    from mino_nexus.catalog.skill_channel import apply_channel_skip, case_menu_ids, milestone_caps_registered

    ctx = getattr(cursor, "run_context", None)
    _mark_model_recovery(cursor, ctx)
    platform = str(getattr(ctx, "platform", "") or "")
    _, phase = step_scope_key(cursor)
    menu_ids = case_menu_ids(ctx, phase) if ctx is not None else set()
    for row in ms:
        if not isinstance(row, dict):
            continue
        if platform and apply_channel_skip(row, platform):
            continue
        st = str(row.get("status") or "pending").strip().lower()
        if st in ("pass", "failed", "skipped"):
            continue
        if menu_ids and not milestone_caps_registered(row, menu_ids, allow_program=True):
            row["status"] = "skipped"
            row["skip_reason"] = "unregistered"
            row["skipped_by"] = "menu"
    focus_id = ""
    pending_idx = -1
    for i, row in enumerate(ms):
        st = str(row.get("status") or "pending").strip().lower()
        if st == "in_progress":
            if focus_id:
                row["status"] = "pending"
            else:
                focus_id = str(row.get("id") or "")
        elif st == "pending" and pending_idx < 0:
            pending_idx = i
    if not focus_id and pending_idx >= 0:
        ms[pending_idx]["status"] = "in_progress"
        focus_id = str(ms[pending_idx].get("id") or "")
    if focus_id:
        state["milestones"] = ms
        state["status"] = "in_progress"
        write_state(cursor, state)
        if writer:
            writer.append("milestone/focus", {"milestone_id": focus_id, **dict(zip(("case_step", "phase"), step_scope_key(cursor)))})
        return True
    return False


def advance_in_progress_focus(cursor: Any, *, writer: Any = None) -> bool:
    """焦点完成后：下一条 pending → in_progress。"""
    return ensure_single_in_progress(cursor, writer=writer)


def needs_vision_plan_gate(state: dict[str, Any]) -> bool:
    """空列表或仅 1 条 open 时规划。prep/do 全部终态但未准出时再规划一次，决定追加或准出。"""
    if milestones_empty(state):
        return True
    open_n = count_open_milestones(state)
    if open_n == 1:
        return True
    if open_n == 0:
        phase = str(state.get("phase") or "").strip().lower()
        if phase in ("prep", "do") and state.get("exit_allowed") is not True:
            return True
    return False


_SKIP_PLAN_CAPS = frozenset({"tap_element", "input_text", "swipe", "scroll", "press_key"})
_KEEP_PLAN_CAPS = frozenset(
    {
        "relogin",
        "confirm_login_state",
        "wait_screen_ready",
        "wait_ms",
        "lease_account",
        "get_otp",
    }
)
_LOGIN_FOCUS_IDS = frozenset(
    {
        "login_entry",
        "account_field",
        "account_fill",
        "send_code",
        "otp_fetch",
        "otp_field",
        "otp_fill",
        "legal_consent",
        "submit_login",
        "login_state",
    }
)


def _milestone_cap(row: dict[str, Any]) -> str:
    return str(row.get("device_cap") or row.get("hook_cap") or row.get("cap") or "").strip()


def _blocking_overlay(ctx: Any) -> str:
    """权限二选一或系统挡屏。协议勾选框不算，登录页上本来就有。"""
    if ctx is None:
        return ""
    overlay = str(getattr(ctx, "system_overlay", "") or "").strip().lower()
    if overlay in ("yes", "true", "1"):
        return "system_overlay"
    nodes = [n for n in (getattr(ctx, "nav_hierarchy_nodes", None) or []) if isinstance(n, dict)]
    from mino_nexus.loop.recovery_permission import permission_choice_dialog_present

    if permission_choice_dialog_present(nodes):
        return "permission_dialog"
    return ""


def _sparse_blocking_sheet(ctx: Any) -> bool:
    """少按钮、有长文案、底部一条宽按钮：像还没写进里程碑的协议层。信息流按钮很多，不算。"""
    if ctx is None:
        return False
    from mino_nexus.loop.ui_consent import _bounds, _center, _label_text, _screen_wh

    nodes = [n for n in (getattr(ctx, "nav_hierarchy_nodes", None) or []) if isinstance(n, dict)]
    labeled = []
    long_text = False
    for node in nodes:
        text = _label_text(node)
        if len(text) >= 12:
            long_text = True
        if node.get("clickable") and text:
            labeled.append(node)
    if not long_text or len(labeled) > 6:
        return False
    sw, sh = _screen_wh(nodes)
    if sw <= 0 or sh <= 0:
        return False
    for node in labeled:
        text = _label_text(node)
        if len(text) > 8:
            continue
        b = _bounds(node)
        width = b[2] - b[0]
        cy = _center(b)[1]
        if width >= sw * 0.45 and cy > sh * 0.45:
            return True
    return False


def _login_form_mismatches_focus(ctx: Any, row: dict[str, Any]) -> bool:
    """当前步不是登录块，屏上却已经是登录表单：仍要规划，把登录插到前面。"""
    mid = str(row.get("id") or "")
    if mid in _LOGIN_FOCUS_IDS or str(row.get("source_block") or "").strip():
        return False
    nodes = [n for n in (getattr(ctx, "nav_hierarchy_nodes", None) or []) if isinstance(n, dict)]
    if not nodes:
        return False
    from mino_nexus.loop.ui_channel import ui_channel_from_ctx, ui_channel_label
    from mino_nexus.loop.ui_sms_request import credential_field_for_login
    from mino_nexus.services.account_credential_text import login_kind_from_ctx

    hit = credential_field_for_login(
        nodes,
        login_kind=login_kind_from_ctx(ctx),
        channel=ui_channel_label(ui_channel_from_ctx(ctx)),
    )
    return hit is not None


def seeded_single_focus_skips_plan(state: dict[str, Any], ctx: Any) -> bool:
    """程序种下的焦点动作已经明确：直接执行，不再打一轮规划。挡屏或失败除外。"""
    if count_open_milestones(state) != 1:
        return False
    row = in_progress_milestone(state)
    if not isinstance(row, dict):
        return False
    if str(row.get("status") or "") == "failed" or row.get("program_fail"):
        return False
    if _blocking_overlay(ctx) or _unlisted_agree_control(state, ctx):
        return False
    mid = str(row.get("id") or "")
    source = str(row.get("source") or "")
    programish = source in ("prep_program", "do_program", "check_program", "program_seed") or bool(
        str(row.get("source_block") or "").strip()
    )
    if mid in _LOGIN_FOCUS_IDS and mid != "login_state":
        if _login_form_mismatches_focus(ctx, row):
            return False
        return True
    if not programish:
        cap = _milestone_cap(row)
        if cap in _KEEP_PLAN_CAPS:
            return False
        kind = str(row.get("kind") or "").strip().lower()
        if cap not in _SKIP_PLAN_CAPS and not kind.startswith("visual"):
            return False
    if _login_form_mismatches_focus(ctx, row):
        return False
    if _sparse_blocking_sheet(ctx) and not programish:
        kind = str(row.get("kind") or "").strip().lower()
        cap = _milestone_cap(row)
        if not kind.startswith("visual") and cap not in _SKIP_PLAN_CAPS:
            return False
    return True


def _unlisted_agree_control(state: dict[str, Any], ctx: Any) -> bool:
    """屏上有「同意」类按钮，但当前里程碑还没写这一步。前置收工和跳过规划都会漏掉协议弹窗。"""
    if ctx is None:
        return False
    from mino_nexus.loop.ui_consent import _label_text

    nodes = [n for n in (getattr(ctx, "nav_hierarchy_nodes", None) or []) if isinstance(n, dict)]
    found = False
    for node in nodes:
        if not node.get("clickable"):
            continue
        text = _label_text(node)
        if text in ("同意", "Agree", "Accept", "我同意") or text.startswith("同意并"):
            found = True
            break
    if not found:
        return False
    for row in _milestones_list(state):
        if str(row.get("status") or "").strip().lower() in _TERMINAL:
            continue
        blob = f"{row.get('id') or ''} {row.get('title') or ''}"
        if "agree" in blob.lower() or "同意" in blob:
            return False
    return True


def _login_finished_but_form_open(state: dict[str, Any], ctx: Any) -> bool:
    """登录块步骤都点过了，登录表单还在：不能据此进入校验。"""
    ids = {str(row.get("id") or "") for row in _milestones_list(state)}
    if "submit_login" not in ids and "account_fill" not in ids:
        return False
    nodes = [n for n in (getattr(ctx, "nav_hierarchy_nodes", None) or []) if isinstance(n, dict)]
    if not nodes:
        return False
    from mino_nexus.loop.ui_channel import ui_channel_from_ctx, ui_channel_label
    from mino_nexus.loop.ui_sms_request import credential_field_for_login
    from mino_nexus.services.account_credential_text import login_kind_from_ctx

    return (
        credential_field_for_login(
            nodes,
            login_kind=login_kind_from_ctx(ctx),
            channel=ui_channel_label(ui_channel_from_ctx(ctx)),
        )
        is not None
    )


def program_exit_allowed(state: dict[str, Any], ctx: Any) -> bool:
    """必填都终态，且屏上没有还没写进列表的障碍：程序准出，不打 exit_latch。"""
    phase = str(state.get("phase") or "").strip().lower()
    # 前置的协议弹窗是 exit_latch 规划追加的。这里提前准出会直接跳过「同意」。
    if phase == "prep":
        return False
    if _blocking_overlay(ctx) or _sparse_blocking_sheet(ctx) or _unlisted_agree_control(state, ctx):
        return False
    if _login_finished_but_form_open(state, ctx):
        return False
    last: Optional[dict[str, Any]] = None
    for row in _milestones_list(state):
        if row.get("optional"):
            continue
        if str(row.get("status") or "").strip().lower() not in _TERMINAL:
            return False
        last = row
    if last is None:
        return False
    if _milestone_cap(last) in ("wait_screen_ready", "wait_ms"):
        return False
    return True


def _may_eval_auto_phase_transition(cursor: Any, state: dict[str, Any]) -> bool:
    """open=0 时是否允许仅凭 evaluate 自动 phase 流转（do 须有过操作或 do 子里程碑）。"""
    from mino_nexus.loop.milestones import milestones_for_phase_eval

    _, ph = step_scope_key(cursor)
    scoped = milestones_for_phase_eval(state, cursor)
    if ph == "do":
        if not scoped:
            return False
        if int(getattr(cursor, "step_ops", 0) or 0) <= 0:
            if not any(str(m.get("source") or "") in ("do_program", "program_seed:do_keys") for m in scoped):
                return False
            if any(
                str(m.get("status") or "pending").strip().lower() in ("pending", "in_progress")
                for m in scoped
                if not m.get("optional")
            ):
                return False
    return True


def needs_stuck_replan(cursor: Any, ctx: Any, state: dict[str, Any]) -> bool:
    """全部终态但未 phase_complete（含失败）→ 停止空转。未准出不算失败，交给规划补准出。"""
    if milestones_empty(state) or count_open_milestones(state) > 0:
        return False
    phase = str(state.get("phase") or "").strip().lower()
    failed = any(
        isinstance(row, dict) and str(row.get("status") or "").strip().lower() == "failed"
        for row in (state.get("milestones") or [])
    )
    if phase in ("prep", "do") and not failed and state.get("exit_allowed") is not True:
        return False
    mv = evaluate_milestones(cursor, ctx)
    if mv.phase_complete:
        return False
    return str(mv.status or "").strip().lower() in ("failed", "in_progress")


def may_run_vision_exec(cursor: Any) -> bool:
    if not vision_exec_v1_enabled() or not milestone_v1_enabled():
        return True
    state = read_state(cursor)
    if milestones_empty(state):
        return False
    if count_open_milestones(state) == 0:
        return False
    return in_progress_milestone(state) is not None


_VISUAL_STEP_CAPS = ("tap_element", "input_text", "swipe_direction", "press_key")


_PHASE_DEVICE_SKILLS = frozenset({
    "tap_element",
    "input_text",
    "swipe_direction",
    "swipe_element_to_element",
    "long_press_element",
    "multi_tap",
    "press_key",
    "wait_ms",
    "wait_screen_ready",
    "set_clipboard",
    "dismiss_ime",
})


def focus_exec_menu_ids(cursor: Any, ctx: Any = None) -> Optional[set[str]]:
    """视觉步放开本阶段设备技能。租号、取码、清缓存仍只出现在自己的里程碑上。"""
    row = in_progress_milestone(read_state(cursor))
    if not row:
        return None
    listed = row.get("exec_caps")
    if isinstance(listed, list):
        caps = {str(x).strip() for x in listed if str(x).strip()}
        if caps:
            return caps
    device = str(row.get("device_cap") or "").strip()
    hook = str(row.get("hook_cap") or "").strip()
    if device and hook and device != hook and (row.get("lease_ready") or row.get("otp_ready")):
        return {device}
    kind = str(row.get("kind") or "").strip().lower()
    if kind in ("visual_action", "visual_tap", "visual_input"):
        return _phase_device_menu(cursor, ctx)
    if hook or device:
        return {hook or device}
    return None


def _phase_device_menu(cursor: Any, ctx: Any) -> set[str]:
    from mino_nexus.catalog.skill_channel import case_menu_ids, is_program_caller

    phase = step_scope_key(cursor)[1] if cursor is not None else "do"
    menu = case_menu_ids(ctx, phase) if ctx is not None else set()
    device = {cap for cap in menu if cap in _PHASE_DEVICE_SKILLS and not is_program_caller(cap)}
    return device or set(_VISUAL_STEP_CAPS)


def success_criteria_for_exec_llm(cursor: Any) -> dict[str, Any]:
    """exec 注入：标出 in_progress 为 active_focus。"""
    state = dict(read_state(cursor))
    fid = in_progress_milestone_id(cursor)
    from mino_nexus.loop.visual_review import prior_action_payload

    prior = prior_action_payload(cursor)
    if prior:
        state["prior_action"] = prior
    if fid:
        state["active_focus_milestone_id"] = fid
        for row in state.get("milestones") or []:
            if isinstance(row, dict) and str(row.get("id") or "") == fid:
                if row.get("match") or row.get("exclude"):
                    state["active_surface"] = {
                        "milestone_id": fid,
                        "match": list(row.get("match") or []),
                        "exclude": list(row.get("exclude") or []),
                        "match_when": list(row.get("match_when") or []),
                    }
                break
    return state


def complete_in_progress_on_tool(
    cursor: Any,
    *,
    capability_id: str,
    ok: bool,
    summary: str = "",
    writer: Any = None,
) -> bool:
    """仅更新当前 in_progress 里程碑终态，并推进焦点。"""
    if not milestone_v1_enabled():
        return False
    state = _sync_scope(cursor, read_state(cursor))
    ms = _milestones_list(state)
    focus = in_progress_milestone(state)
    if not focus:
        return False
    cap = str(capability_id or "").strip()
    if ok:
        focus["status"] = "pass"
        focus["evidence"] = (summary or cap)[:400]
        focus.pop("fail_streak", None)
    else:
        focus["status"] = "failed"
        focus["evidence"] = (summary or cap)[:400]
    state["milestones"] = ms
    write_state(cursor, state)
    if ok:
        from mino_nexus.loop.fuse.interrupt_stack import maybe_pop_interrupt

        maybe_pop_interrupt(cursor, writer=writer)
    advance_in_progress_focus(cursor, writer=writer)
    return True


def apply_step_requirements_complete(
    cursor: Any,
    ctx: Any,
    plan: Any,
    *,
    writer: Any = None,
    in_prep: bool = False,
    in_check: bool = False,
) -> Optional[str]:
    """plan 声明本步要求已满足且无 open 里程碑 → 阶段流转。"""
    from mino_nexus.loop.step_phase_fsm import apply_phase_transition, step_phase_fsm_v1_enabled

    if not step_phase_fsm_v1_enabled():
        return None
    complete = bool(getattr(plan, "exit_allowed", False)) or bool(
        getattr(plan, "step_requirements_complete", False)
    )
    if not complete and read_state(cursor).get("exit_allowed") is not True:
        return None
    state = read_state(cursor)
    if count_open_milestones(state) > 0:
        return None
    mv = evaluate_milestones(cursor, ctx)
    if not mv.phase_complete:
        return None
    return apply_phase_transition(
        cursor, ctx, mv, writer=writer, in_prep=in_prep, in_check=in_check
    )


@dataclass
class OrchestratedTurn:
    decision: Optional[AgentDecision] = None
    plan_only: bool = False
    phase_transition: Optional[str] = None


def run_vision_orchestrated_turn(
    *,
    cursor: Any,
    ctx: Any,
    writer: Any,
    shot: Any,
    steps: list[dict[str, Any]],
    inspect_slots: dict[str, str],
    provider_id: str,
    phase_tool_kinds: list[str] | None,
    send_image: bool,
    in_prep: bool = False,
    in_check: bool = False,
    review_program_fail: str = "",
) -> OrchestratedTurn:
    """单 turn：按状态机 gate plan → 可选 exec。"""
    from mino_nexus.loop.vision_plan import run_vision_plan_turn_if_enabled
    from mino_nexus.loop.vision_exec import run_vision_exec_turn

    if not (vision_plan_v1_enabled() or vision_exec_v1_enabled()):
        return OrchestratedTurn()

    if ctx is not None:
        setattr(cursor, "run_context", ctx)
        _mark_model_recovery(cursor, ctx)
    run_case = getattr(ctx, "case", None) if ctx is not None else None
    from mino_nexus.loop.program_plan_seed import ensure_program_milestone_seeds

    ensure_program_milestone_seeds(
        cursor,
        ctx,
        run_case if isinstance(run_case, dict) else None,
        writer=writer,
        app_id=str(getattr(ctx, "app_id", "") or ""),
        include_prep=False,
    )
    from mino_nexus.loop.fuse.interrupt_stack import maybe_pop_interrupt

    maybe_pop_interrupt(cursor, writer=writer)
    ensure_single_in_progress(cursor, writer=writer)
    from mino_nexus.loop.flow_block_runner_v2 import skip_login_entry_if_form_visible
    from mino_nexus.loop.vision_plan import retire_repeat_wait_focus

    skip_login_entry_if_form_visible(cursor, ctx, writer=writer)
    retire_repeat_wait_focus(cursor, writer=writer)
    state = read_state(cursor)

    if not milestones_empty(state) and count_open_milestones(state) == 0 and _may_eval_auto_phase_transition(
        cursor, state
    ):
        granted_exit = False
        if state.get("exit_allowed") is not True and program_exit_allowed(state, ctx):
            granted_exit = True
            state["exit_allowed"] = True
            state = _sync_scope(cursor, state)
            write_state(cursor, state)
            if writer:
                writer.append(
                    "orchestrator/plan_gate",
                    {
                        "reason": "program_exit",
                        "open": 0,
                        **dict(zip(("case_step", "phase"), step_scope_key(cursor))),
                    },
                )
            state = read_state(cursor)
        mv = evaluate_milestones(cursor, ctx)
        transitioned = False
        if mv.phase_complete:
            from mino_nexus.loop.step_phase_fsm import apply_phase_transition, step_phase_fsm_v1_enabled

            if step_phase_fsm_v1_enabled():
                tr = apply_phase_transition(
                    cursor, ctx, mv, writer=writer, in_prep=in_prep, in_check=in_check
                )
                if tr:
                    transitioned = True
                    return OrchestratedTurn(phase_transition=tr, plan_only=True)
        if granted_exit and not transitioned:
            state = read_state(cursor)
            state["exit_allowed"] = False
            write_state(cursor, state)

    plan_obj = None

    state = read_state(cursor)
    stuck = needs_stuck_replan(cursor, ctx, state)
    if stuck:
        mv = evaluate_milestones(cursor, ctx)
        summary = str(mv.summary or review_program_fail or "里程碑失败，停止空转")[:500]
        if writer:
            writer.append(
                "orchestrator/plan_gate",
                {
                    "reason": "stuck_stop",
                    "open": count_open_milestones(state),
                    **dict(zip(("case_step", "phase"), step_scope_key(cursor))),
                },
            )
        return OrchestratedTurn(
            decision=AgentDecision(status="give_up", thought=summary),
            plan_only=True,
        )
    plan_gate = vision_plan_v1_enabled() and needs_vision_plan_gate(state)
    if plan_gate:
        open_n = count_open_milestones(read_state(cursor))
        if open_n == 1 and seeded_single_focus_skips_plan(read_state(cursor), ctx):
            if writer:
                focus = in_progress_milestone(read_state(cursor)) or {}
                writer.append(
                    "orchestrator/plan_gate",
                    {
                        "reason": "skip_seeded_focus",
                        "open": 1,
                        "milestone_id": str(focus.get("id") or ""),
                        **dict(zip(("case_step", "phase"), step_scope_key(cursor))),
                    },
                )
            plan_gate = False
    if plan_gate:
        from mino_nexus.action_space.scheme import action_scheme
        from mino_nexus.loop.visual_review import has_visual_prior

        if (
            action_scheme(ctx) == "visual"
            and has_visual_prior(cursor)
            and in_progress_milestone(read_state(cursor)) is not None
        ):
            plan_gate = False
            if writer:
                writer.append(
                    "orchestrator/plan_gate",
                    {
                        "reason": "skip_visual_prior",
                        "open": count_open_milestones(read_state(cursor)),
                        **dict(zip(("case_step", "phase"), step_scope_key(cursor))),
                    },
                )
    if plan_gate:
        open_n = count_open_milestones(read_state(cursor))
        if writer:
            writer.append(
                "orchestrator/plan_gate",
                {
                    "reason": (
                        "empty"
                        if milestones_empty(read_state(cursor))
                        else (
                            "exit_latch"
                            if open_n == 0
                            else "open_eq_1"
                        )
                    ),
                    "open": open_n,
                    **dict(zip(("case_step", "phase"), step_scope_key(cursor))),
                },
            )
        plan_obj = run_vision_plan_turn_if_enabled(
            cursor=cursor,
            ctx=ctx,
            writer=writer,
            shot=shot,
            steps=steps,
            inspect_slots=inspect_slots,
            provider_id=provider_id,
            review_program_fail="",
        )
        ensure_single_in_progress(cursor, writer=writer)
        from mino_nexus.loop.vision_plan import retire_repeat_wait_focus

        retire_repeat_wait_focus(cursor, writer=writer)
        if getattr(cursor, "milestone_hold_exec", False):
            setattr(cursor, "milestone_hold_exec", False)
            thought = str(getattr(plan_obj, "thought", "") or "已把步骤插到当前焦点之前，本回合改执行新的第一条")
            return OrchestratedTurn(
                decision=AgentDecision(status="continue", thought=thought[:500]),
                plan_only=True,
            )
        if plan_obj is not None:
            tr = apply_step_requirements_complete(
                cursor, ctx, plan_obj, writer=writer, in_prep=in_prep, in_check=in_check
            )
            if tr:
                return OrchestratedTurn(phase_transition=tr, plan_only=True)

    if not may_run_vision_exec(cursor):
        if writer:
            writer.append(
                "orchestrator/exec_skip",
                {
                    "reason": "no_in_progress",
                    "open": count_open_milestones(read_state(cursor)),
                    **dict(zip(("case_step", "phase"), step_scope_key(cursor))),
                },
            )
        thought = str(getattr(plan_obj, "thought", "") or "") if plan_obj else "等待规划补齐里程碑"
        return OrchestratedTurn(
            decision=AgentDecision(status="continue", thought=thought[:500]),
            plan_only=True,
        )

    if not vision_exec_v1_enabled():
        return OrchestratedTurn()

    focus = in_progress_milestone(read_state(cursor))
    focus_cap = str((focus or {}).get("hook_cap") or "").strip()
    focus_obs = str((focus or {}).get("observe") or "exec").strip().lower()
    focus_params = (
        dict((focus or {}).get("params") or {})
        if isinstance((focus or {}).get("params"), dict)
        else {}
    )
    if focus_obs == "program" and focus_cap:
        title = str((focus or {}).get("title") or focus_cap)
        return OrchestratedTurn(
            decision=AgentDecision(
                status="continue",
                thought=title[:240],
                action=AgentAction(capability_id=focus_cap, params=focus_params),
            )
        )
    if in_prep and focus_cap:
        from mino_nexus.loop.router_proxy import is_local_cap

        if is_local_cap(focus_cap):
            device = str((focus or {}).get("device_cap") or "").strip()
            ready = bool((focus or {}).get("lease_ready") or (focus or {}).get("otp_ready"))
            if not (device and device != focus_cap and ready):
                title = str((focus or {}).get("title") or focus_cap)
                return OrchestratedTurn(
                    decision=AgentDecision(
                        status="continue",
                        thought=title[:240],
                        action=AgentAction(capability_id=focus_cap, params={}),
                    )
                )

    from mino_nexus.loop.binding_cache import try_replay_decision

    replayed = try_replay_decision(cursor, ctx, writer=writer, focus=focus)
    if replayed is not None:
        return OrchestratedTurn(decision=replayed)

    exec_mode = "replay"
    try:
        from mino_nexus.loop.fuse.interrupt_stack import stack_depth

        if stack_depth(cursor) > 0:
            exec_mode = "interrupt"
        elif focus_obs == "visual_each_run":
            exec_mode = "heal_skip"
    except Exception:  # noqa: BLE001
        if focus_obs == "visual_each_run":
            exec_mode = "heal_skip"

    decision = run_vision_exec_turn(
        cursor=cursor,
        ctx=ctx,
        writer=writer,
        shot=shot,
        steps=steps,
        inspect_slots=inspect_slots,
        provider_id=provider_id,
        phase_tool_kinds=phase_tool_kinds,
        session_block="",
        send_image=send_image,
        exec_mode=exec_mode,
    )
    warnings = list(getattr(decision, "parse_warnings", None) or [])
    if "focused cap missing from menu" in warnings:
        state = _sync_scope(cursor, read_state(cursor))
        ms = _milestones_list(state)
        focus = in_progress_milestone(state)
        if focus is not None:
            from mino_nexus.catalog.skill_channel import is_program_caller

            cap = str(focus.get("hook_cap") or focus.get("device_cap") or "").strip()
            # 程序自己派的能力不在用例菜单里。缺菜单不等于未注册，不能把里程碑跳过。
            if not is_program_caller(cap):
                focus["status"] = "skipped"
                focus["skip_reason"] = "unregistered"
                focus["skipped_by"] = "menu"
                state["milestones"] = ms
                write_state(cursor, state)
                ensure_single_in_progress(cursor, writer=writer)
        return OrchestratedTurn(
            decision=AgentDecision(
                status="continue",
                thought=str(getattr(decision, "thought", "") or "已跳过未注册能力")[:500],
            ),
            plan_only=True,
        )
    return OrchestratedTurn(decision=decision)
