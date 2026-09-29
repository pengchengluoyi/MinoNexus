"""P6：看图规划（agent-vision-plan）——只维护 success_criteria.milestones。"""
from __future__ import annotations

from typing import Any, Optional

from mino_nexus.ai.planner import vision_plan_turn
from mino_nexus.ai.schemas import VisionPlanDecision
from mino_nexus.loop.llm_step_context import step_scope_key, success_criteria_for_llm
from mino_nexus.loop.milestones import (
    _normalize_milestone_row,
    milestone_v1_enabled,
    read_state,
    write_state,
    _sync_scope,
)
from mino_nexus.loop.vision_flags import vision_plan_v1_enabled

_PREP_APPEND_HINTS = ("确认", "验收", "登录态", "session", "游客", "未登录", "已登录", "guest")


def _program_milestones_open(cursor: Any) -> bool:
    """程序种下的步骤还没走完时，不接受规划另起一条业务步骤。"""
    from mino_nexus.loop.milestones import _TERMINAL, read_state

    ms = read_state(cursor).get("milestones") or []
    program = [
        m
        for m in ms
        if isinstance(m, dict)
        and (
            str(m.get("source") or "") in ("prep_program", "do_program", "program_seed")
            or str(m.get("source_block") or "").strip()
        )
    ]
    if not program:
        return False
    return any(str(m.get("status") or "pending") not in _TERMINAL for m in program)


def _row_cap_ids(raw: dict[str, Any]) -> set[str]:
    caps: set[str] = set()
    for key in ("hook_cap", "device_cap", "cap"):
        val = str((raw or {}).get(key) or "").strip()
        if val:
            caps.add(val)
    listed = (raw or {}).get("exec_caps")
    if isinstance(listed, list):
        caps.update(str(item or "").strip() for item in listed if str(item or "").strip())
    return caps


def _is_tap_row(raw: dict[str, Any]) -> bool:
    return bool(_row_cap_ids(raw) & {"tap_element", "accept_legal_consent"})


def _is_wait_ready_row(raw: dict[str, Any]) -> bool:
    caps = _row_cap_ids(raw)
    return bool(caps) and caps <= {"wait_screen_ready", "wait_ms"} and "wait_screen_ready" in caps


def _wait_already_passed(cursor: Any) -> bool:
    return int(getattr(cursor, "wait_ready_passes", 0) or 0) >= 1


def _do_plan_locks_append(cursor: Any, case: dict[str, Any] | None) -> bool:
    """登录这类逻辑块种下之后不再追加。看图操作步仍允许补下一步。"""
    from mino_nexus.loop.do_program_plan import has_do_program_steps, resolve_do_program_plan

    if not has_do_program_steps(cursor, case):
        return False
    plan = resolve_do_program_plan(case, cursor)
    steps = plan.get("steps") if isinstance(plan.get("steps"), list) else []
    return any(
        isinstance(step, dict)
        and (str(step.get("kind") or "") == "flow_block" or str(step.get("block_id") or "").strip())
        for step in steps
    )


def hold_visual_do_exit_until_effect(cursor: Any, ctx: Any) -> None:
    """看图操作步：预期还没出现在屏上时，一次点击不能把准出带走。"""
    if str(getattr(cursor, "phase", "") or "").strip().lower() != "do":
        return
    cur = cursor.current() if hasattr(cursor, "current") else None
    expected = str(getattr(cur, "expected", "") or "").strip()
    if not expected or int(getattr(cursor, "step_effect_hit_streak", 0) or 0) > 0:
        return
    case = _case_from_ctx(ctx if ctx is not None else getattr(cursor, "run_context", None))
    if _do_plan_locks_append(cursor, case):
        return
    from mino_nexus.loop.milestones import read_state, write_state

    state = read_state(cursor)
    if state.get("exit_allowed") is not True:
        return
    state["exit_allowed"] = False
    write_state(cursor, state)


def retire_repeat_wait_focus(cursor: Any, *, writer: Any = None) -> bool:
    """同一屏已经判过可读之后，丢掉还排着的等待页就绪，避免一张张空卡。"""
    if not _wait_already_passed(cursor):
        return False
    from mino_nexus.loop.milestones import _sync_scope, read_state, write_state
    from mino_nexus.loop.milestone_orchestrator import ensure_single_in_progress

    state = _sync_scope(cursor, read_state(cursor))
    ms = [m for m in (state.get("milestones") or []) if isinstance(m, dict)]
    changed = False
    for row in ms:
        st = str(row.get("status") or "pending").strip().lower()
        if st not in ("pending", "in_progress"):
            continue
        if not _is_wait_ready_row(row):
            continue
        row["status"] = "skipped"
        row["skip_reason"] = "wait_already_ready"
        changed = True
    if not changed:
        return False
    state["milestones"] = ms
    write_state(cursor, state)
    ensure_single_in_progress(cursor, writer=writer)
    if writer is not None:
        try:
            writer.append("plan_append/rejected", {"reason": "wait_already_ready", "phase": "prep"})
        except Exception:  # noqa: BLE001
            pass
    return True


def _prep_plan_append_allowed(raw: dict[str, Any]) -> bool:
    kind = str((raw or {}).get("kind") or "").strip().lower()
    if kind in ("checkpoint", "internal"):
        return True
    if kind == "hook" and str((raw or {}).get("evaluate") or "").strip():
        return True
    title = str((raw or {}).get("title") or "")
    return any(h in title for h in _PREP_APPEND_HINTS)


def _case_from_ctx(ctx: Any) -> dict[str, Any] | None:
    raw = getattr(ctx, "case", None) if ctx is not None else None
    return raw if isinstance(raw, dict) else None


def filter_plan_milestones_append(
    cursor: Any,
    ctx: Any,
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """do 有 program_plan 时禁止 plan 追加业务边；check 仅允许对齐已有 checkpoint id。未注册 hook 丢掉。"""
    kept: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    if not rows:
        return kept, rejected
    from mino_nexus.catalog.skill_channel import (
        case_menu_ids,
        milestone_caps_registered,
        rewrite_milestone_aliases,
    )

    _, phase = step_scope_key(cursor)
    menu_ids = case_menu_ids(ctx, str(phase or "")) if ctx is not None else set()
    registered: list[dict[str, Any]] = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        raw = rewrite_milestone_aliases(raw)
        if menu_ids and not milestone_caps_registered(raw, menu_ids):
            raw["_reject_reason"] = "unregistered"
            rejected.append(raw)
            continue
        registered.append(raw)
    rows = registered
    if not rows:
        return kept, rejected
    ph = str(phase or "").strip().lower()
    case = _case_from_ctx(ctx)

    if ph == "do":
        if _do_plan_locks_append(cursor, case):
            for raw in rows:
                item = dict(raw)
                item["_reject_reason"] = "program_plan_locked"
                rejected.append(item)
            return kept, rejected

    if ph == "prep":
        from mino_nexus.loop.milestones import read_state

        ms = read_state(cursor).get("milestones") or []
        has_prep_program = any(
            isinstance(m, dict) and str(m.get("source") or "") == "prep_program" for m in ms
        )
        if has_prep_program:
            program_done = not _program_milestones_open(cursor)
            for raw in rows:
                if _is_wait_ready_row(raw) and _wait_already_passed(cursor):
                    item = dict(raw)
                    item["_reject_reason"] = "wait_already_ready"
                    rejected.append(item)
                    continue
                if program_done and _is_tap_row(raw):
                    kept.append(raw)
                    continue
                if program_done:
                    item = dict(raw)
                    item["_reject_reason"] = "program_plan_done"
                    rejected.append(item)
                    continue
                if _prep_plan_append_allowed(raw):
                    kept.append(raw)
                else:
                    item = dict(raw)
                    item["_reject_reason"] = "prep_append_filtered"
                    rejected.append(item)
            return kept, rejected

    if ph == "check":
        from mino_nexus.loop.check_program_plan import resolve_check_program_plan

        plan = resolve_check_program_plan(case, cursor)
        cps = plan.get("checkpoints") if isinstance(plan.get("checkpoints"), list) else []
        if cps:
            allowed = {
                str(cp.get("id") or "").strip()
                for cp in cps
                if isinstance(cp, dict) and str(cp.get("id") or "").strip()
            }
            for raw in rows:
                mid = str((raw or {}).get("id") or "").strip()
                if mid and mid in allowed:
                    kept.append(raw)
                else:
                    rejected.append(raw)
            return kept, rejected

    return list(rows), rejected


def reset_plan_context_for_check(cursor: Any) -> None:
    setattr(cursor, "vision_plan_last", None)
    setattr(cursor, "vision_assert_done", False)


def _append_rows(
    ms: list[dict[str, Any]],
    by_id: dict[str, dict[str, Any]],
    rows: list[dict[str, Any]],
    *,
    source: str,
) -> int:
    n = 0
    for raw in rows:
        row = _normalize_milestone_row(raw, warnings=[])
        if not row:
            continue
        mid = str(row.get("id") or "").strip()
        if mid and mid in by_id:
            continue
        row["source"] = source
        ms.append(row)
        by_id[mid] = row
        n += 1
    return n


def apply_vision_plan_to_cursor(
    cursor: Any,
    plan: VisionPlanDecision,
    *,
    writer: Any = None,
    ctx: Any = None,
) -> None:
    """合并规划结果：更新状态 + 末尾追加；禁止整表替换。"""
    setattr(cursor, "vision_plan_last", plan)
    if not milestone_v1_enabled():
        return

    state = _sync_scope(cursor, read_state(cursor))
    ms = [dict(m) for m in (state.get("milestones") or []) if isinstance(m, dict)]
    by_id = {str(m.get("id") or ""): m for m in ms if str(m.get("id") or "")}

    append_rows = list(plan.milestones_append or [])
    if not append_rows and plan.milestones:
        append_rows = list(plan.milestones)
    run_ctx = ctx if ctx is not None else getattr(cursor, "run_context", None)
    kept, rejected = filter_plan_milestones_append(cursor, run_ctx, append_rows)
    if rejected and writer is not None:
        try:
            _, phase = step_scope_key(cursor)
            writer.append(
                "plan_append/rejected",
                {
                    "phase": phase,
                    "count": len(rejected),
                    "ids": [str((r or {}).get("id") or "")[:64] for r in rejected[:8]],
                    "reason": str((rejected[0] or {}).get("_reject_reason") or "program_plan_locked"),
                },
            )
        except Exception:  # noqa: BLE001
            pass
    _append_rows(ms, by_id, kept, source="plan_append")
    from mino_nexus.loop.milestones import _TERMINAL

    still_open = [
        m
        for m in ms
        if isinstance(m, dict)
        and not m.get("optional")
        and str(m.get("status") or "pending") not in _TERMINAL
    ]
    # 逻辑块已经走完、追加又被拒绝时才准出。看图操作步还要继续点，不能因此离开本步。
    _, phase_now = step_scope_key(cursor)
    visual_do = str(phase_now or "") == "do" and not _do_plan_locks_append(
        cursor, _case_from_ctx(run_ctx)
    )
    only_repeat_waits = bool(rejected) and all(
        str((row or {}).get("_reject_reason") or "") == "wait_already_ready" for row in rejected
    )
    failed_open = any(isinstance(m, dict) and str(m.get("status") or "") == "failed" for m in ms)
    if only_repeat_waits and not still_open and str(phase_now or "") == "do" and not failed_open:
        plan.exit_allowed = True
    if (
        rejected
        and not still_open
        and not visual_do
        and not only_repeat_waits
        and not failed_open
    ):
        plan.exit_allowed = True

    for cp in plan.checkpoints_plan or []:
        if not isinstance(cp, dict):
            continue
        cid = str(cp.get("id") or cp.get("checkpoint_id") or "").strip()
        if cid and cid in by_id:
            continue
        _append_rows(
            ms,
            by_id,
            [
                {
                    "id": cid or None,
                    "title": cp.get("title") or cp.get("expect") or cid,
                    "kind": "checkpoint",
                    "status": "pending",
                    "assert": cp,
                }
            ],
            source="plan_checkpoint",
        )

    state["milestones"] = ms
    state["status"] = "in_progress"
    if bool(getattr(plan, "exit_allowed", False)) or bool(
        getattr(plan, "step_requirements_complete", False)
    ):
        state["exit_allowed"] = True
    write_state(cursor, state)

    from mino_nexus.loop.milestone_orchestrator import ensure_single_in_progress
    from mino_nexus.loop.step_phase_fsm import apply_flow_block_skip_ops

    apply_flow_block_skip_ops(cursor, plan, writer=writer)
    ensure_single_in_progress(cursor, writer=writer)


def run_vision_plan_turn_if_enabled(
    *,
    cursor: Any,
    ctx: Any,
    writer: Any,
    shot: Any,
    steps: list[dict[str, Any]],
    inspect_slots: dict[str, str],
    provider_id: str = "",
    review_program_fail: str = "",
) -> Optional[VisionPlanDecision]:
    if not vision_plan_v1_enabled():
        return None
    from mino_nexus.loop.step_phase_fsm import (
        apply_failure_verdict_from_plan,
        program_fail_review_text,
    )

    review = str(review_program_fail or "").strip()
    if not review and getattr(cursor, "milestone_review_pending", False):
        review = program_fail_review_text(cursor)
    from mino_nexus.loop.vision_observation import apply_vision_observation_slots

    apply_vision_observation_slots(inspect_slots, cursor, writer=writer)
    case_step, phase = step_scope_key(cursor)
    from mino_nexus.catalog.skill_channel import case_menu_ids

    criteria = dict(success_criteria_for_llm(cursor))
    allowed = sorted(case_menu_ids(ctx, phase))
    if allowed:
        criteria["allowed_capability_ids"] = allowed
    plan = vision_plan_turn(
        run_context=ctx,
        cursor=cursor,
        width=int(getattr(shot, "width", None) or 1080),
        height=int(getattr(shot, "height", None) or 1920),
        image_base64=(
            str(getattr(shot, "image_base64", "") or "")
            if callable(getattr(shot, "has_image", None)) and shot.has_image()
            else ""
        ),
        image_mime=str(getattr(shot, "image_mime", "") or "image/png"),
        hierarchy_text=str(inspect_slots.get("hierarchy_text") or ""),
        success_criteria=criteria,
        knowledge_hint=str(inspect_slots.get("knowledge_hint") or ""),
        knowledge_body=str(inspect_slots.get("knowledge_body") or ""),
        nav_assist=str(inspect_slots.get("nav_assist") or ""),
        doc_context=str(inspect_slots.get("doc_context") or ""),
        provider_id=provider_id or None,
        review_program_fail=review,
    )
    apply_vision_plan_to_cursor(cursor, plan, writer=writer, ctx=ctx)
    if review:
        apply_failure_verdict_from_plan(cursor, plan, writer=writer)
        setattr(cursor, "milestone_review_pending", False)
    if writer is not None:
        try:
            st = read_state(cursor)
            writer.append(
                "plan/vision",
                {
                    "phase": phase,
                    "case_step": case_step,
                    "thought": (plan.thought or "")[:500],
                    "milestone_count": len(st.get("milestones") or []),
                    "parse_warnings": list(plan.parse_warnings or []),
                    "review_mode": bool(review_program_fail),
                },
            )
        except Exception:  # noqa: BLE001
            pass
    return plan
