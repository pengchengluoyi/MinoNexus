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


def focus_exec_menu_ids(cursor: Any) -> Optional[set[str]]:
    """in_progress 已明确时，执行菜单只留这一步的能力。"""
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
    hook = hook or device
    if hook:
        return {hook}
    kind = str(row.get("kind") or "").strip().lower()
    if kind in ("visual_action", "visual_tap", "visual_input"):
        return set(_VISUAL_STEP_CAPS)
    return None


def success_criteria_for_exec_llm(cursor: Any) -> dict[str, Any]:
    """exec 注入：标出 in_progress 为 active_focus。"""
    state = dict(read_state(cursor))
    fid = in_progress_milestone_id(cursor)
    if fid:
        state["active_focus_milestone_id"] = fid
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
        from mino_nexus.loop.interrupt_stack import maybe_pop_interrupt

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
    from mino_nexus.loop.interrupt_stack import maybe_pop_interrupt

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
        mv = evaluate_milestones(cursor, ctx)
        if mv.phase_complete:
            from mino_nexus.loop.step_phase_fsm import apply_phase_transition, step_phase_fsm_v1_enabled

            if step_phase_fsm_v1_enabled():
                tr = apply_phase_transition(
                    cursor, ctx, mv, writer=writer, in_prep=in_prep, in_check=in_check
                )
                if tr:
                    return OrchestratedTurn(phase_transition=tr, plan_only=True)

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
            decision=AgentDecision(status="fail", thought=summary),
            plan_only=True,
        )
    plan_gate = vision_plan_v1_enabled() and needs_vision_plan_gate(state)
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
    )
    warnings = list(getattr(decision, "parse_warnings", None) or [])
    if "focused cap missing from menu" in warnings:
        state = _sync_scope(cursor, read_state(cursor))
        ms = _milestones_list(state)
        focus = in_progress_milestone(state)
        if focus is not None:
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
