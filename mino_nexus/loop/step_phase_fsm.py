"""P6-P2/P4：步骤 phase 状态机（里程碑聚合收工，非 signal_done）。"""
from __future__ import annotations

from typing import Any, Optional

from mino_nexus.loop.llm_step_context import step_scope_key
from mino_nexus.loop.vision_flags import step_phase_fsm_v1_enabled
from mino_nexus.loop.milestones import (
    MilestoneVerdict,
    evaluate_milestones,
    milestone_v1_enabled,
    read_state,
    write_state,
    _sync_scope,
    _TERMINAL,
)
def first_active_milestone_id(cursor: Any) -> str:
    from mino_nexus.loop.milestone_orchestrator import in_progress_milestone_id

    fid = in_progress_milestone_id(cursor)
    if fid:
        return fid
    state = read_state(cursor)
    ms = state.get("milestones") if isinstance(state.get("milestones"), list) else []
    for row in ms:
        if not isinstance(row, dict):
            continue
        st = str(row.get("status") or "pending").strip().lower()
        if st in _TERMINAL:
            continue
        return str(row.get("id") or "").strip()
    return ""


def program_fail_review_text(cursor: Any) -> str:
    state = read_state(cursor)
    ms = state.get("milestones") if isinstance(state.get("milestones"), list) else []
    lines: list[str] = []
    for row in ms:
        if not isinstance(row, dict):
            continue
        if str(row.get("status") or "") != "failed":
            continue
        mid = str(row.get("id") or "").strip()
        ev = str(row.get("evidence") or "").strip()
        pf = row.get("program_fail")
        if isinstance(pf, dict) and not ev:
            ev = str(pf.get("reason") or "").strip()
        lines.append(f"- {mid or '?'}: {ev or 'program_fail'}")
    return "\n".join(lines)[:2000]


def check_phase_allows_recovery_cap(cap_id: str) -> bool:
    cid = str(cap_id or "").strip()
    if not cid:
        return False
    if cid.startswith("recover_"):
        return True
    if cid in ("wait_ms", "noop"):
        return True
    return False


def mark_milestone_program_fail(
    cursor: Any,
    *,
    milestone_id: str,
    reason: str,
    cap_id: str = "",
    writer: Any = None,
) -> None:
    if not milestone_v1_enabled():
        return
    mid = str(milestone_id or "").strip()
    state = _sync_scope(cursor, read_state(cursor))
    ms = state.get("milestones") if isinstance(state.get("milestones"), list) else []
    for row in ms:
        if not isinstance(row, dict):
            continue
        if mid and str(row.get("id") or "") != mid:
            continue
        if not mid and str(row.get("status") or "") not in ("pending", "in_progress"):
            continue
        row["status"] = "failed"
        row["evidence"] = str(reason or "")[:400]
        row.setdefault("program_fail", {})
        if isinstance(row["program_fail"], dict):
            row["program_fail"] = {
                **row["program_fail"],
                "cap_id": str(cap_id or ""),
                "reason": str(reason or "")[:300],
            }
        break
    state["milestones"] = ms
    write_state(cursor, state)
    if writer:
        writer.append(
            "milestone/program_fail",
            {
                "milestone_id": mid,
                "cap_id": str(cap_id or ""),
                "reason": str(reason or "")[:300],
                **dict(zip(("case_step", "phase"), step_scope_key(cursor))),
            },
        )


def apply_failure_verdict_from_plan(cursor: Any, plan: Any, *, writer: Any = None) -> bool:
    """规划复核：failure_verdict.blocking=false 时翻案 pending。"""
    fv = getattr(plan, "failure_verdict", None) or {}
    if not isinstance(fv, dict):
        return False
    blocking = fv.get("blocking")
    if blocking is True:
        return False
    if blocking is not False and str(fv.get("blocking") or "").lower() not in ("false", "0", "no"):
        return False
    reason = str(fv.get("reason") or getattr(plan, "thought", "") or "")[:400]
    mid = str(fv.get("milestone_id") or "").strip()
    state = _sync_scope(cursor, read_state(cursor))
    ms = state.get("milestones") if isinstance(state.get("milestones"), list) else []
    changed = False
    for row in ms:
        if not isinstance(row, dict):
            continue
        if mid and str(row.get("id") or "") != mid:
            continue
        if str(row.get("status") or "") != "failed":
            continue
        row["status"] = "pending"
        row["plan_overturn"] = {"reason": reason}
        changed = True
        if mid:
            break
    if not changed:
        return False
    state["milestones"] = ms
    write_state(cursor, state)
    if writer:
        writer.append(
            "milestone/plan_overturn",
            {"milestone_id": mid, "reason": reason, **dict(zip(("case_step", "phase"), step_scope_key(cursor)))},
        )
    return True


def apply_flow_block_skip_ops(cursor: Any, plan: Any, *, writer: Any = None) -> int:
    """P6-P4：规划 flow_block_ops skip_step → milestone skipped。"""
    ops = getattr(plan, "flow_block_ops", None) or []
    if not isinstance(ops, list) or not ops:
        return 0
    state = _sync_scope(cursor, read_state(cursor))
    ms = state.get("milestones") if isinstance(state.get("milestones"), list) else []
    by_id = {str(m.get("id") or ""): m for m in ms if isinstance(m, dict)}
    n = 0
    for op in ops:
        if not isinstance(op, dict):
            continue
        if str(op.get("op") or "").strip().lower() not in ("skip_step", "skip"):
            continue
        sid = str(op.get("step_id") or op.get("milestone_id") or "").strip()
        if not sid or sid not in by_id:
            continue
        row = by_id[sid]
        row["status"] = "skipped"
        row["skip_reason"] = str(op.get("reason") or "plan_skip")[:200]
        row["skipped_by"] = "plan"
        n += 1
    if n:
        state["milestones"] = list(by_id.values())
        write_state(cursor, state)
        if writer:
            writer.append("milestone/skip_applied", {"count": n, "source": "plan"})
    return n


def apply_phase_transition(
    cursor: Any,
    ctx: Any,
    verdict: MilestoneVerdict,
    *,
    writer: Any = None,
    in_prep: bool = False,
    in_check: bool = False,
) -> Optional[str]:
    """里程碑 phase_complete 时程序流转。返回 transition 名或 None。"""
    if not verdict.phase_complete:
        return None
    phase = str(getattr(cursor, "phase", "") or "").strip().lower()
    if (in_check or phase == "check") and str(getattr(cursor, "check_oracle_status", "") or "") == "fail":
        return None
    name = ""
    if in_prep or phase == "prep":
        if hasattr(cursor, "finish_prep"):
            cursor.finish_prep()
        name = "prep_to_do"
    elif in_check or phase == "check":
        if hasattr(cursor, "mark_checked"):
            cursor.mark_checked()
        if hasattr(cursor, "advance") and not cursor.advance():
            name = "check_to_case_done"
        else:
            name = "check_to_next_step"
    elif phase == "do":
        from mino_nexus.loop.flow_block_exit import evaluate_login_block_exit_shadow

        if evaluate_login_block_exit_shadow(cursor, ctx, writer=writer):
            return None
        if hasattr(cursor, "step_goal_met"):
            cursor.step_goal_met = True
        if hasattr(cursor, "enter_check"):
            cursor.enter_check()
        name = "do_to_check"
    if writer and name:
        writer.append(
            "phase/transition",
            {
                "transition": name,
                "from_phase": phase,
                "summary": verdict.summary,
                **dict(zip(("case_step", "phase_key"), step_scope_key(cursor))),
            },
        )
    return name or None


def handle_model_signal_done_blocked(
    cursor: Any,
    ctx: Any,
    *,
    writer: Any = None,
    in_prep: bool = False,
    in_check: bool = False,
) -> tuple[str, Optional[str]]:
    """拦截 LLM signal_done：只按里程碑聚合流转。返回 (action, transition)。"""
    mv = evaluate_milestones(cursor, ctx)
    if mv.status == "failed" and not in_check:
        setattr(cursor, "milestone_review_pending", True)
        return ("failed_review", None)
    if mv.phase_complete:
        tr = apply_phase_transition(
            cursor, ctx, mv, writer=writer, in_prep=in_prep, in_check=in_check
        )
        return ("transition", tr)
    return ("pending", None)


def try_transition_after_tool_pass(
    cursor: Any,
    ctx: Any,
    *,
    writer: Any = None,
    in_prep: bool = False,
    in_check: bool = False,
) -> Optional[str]:
    if not step_phase_fsm_v1_enabled() or not milestone_v1_enabled():
        return None
    mv = evaluate_milestones(cursor, ctx)
    if not mv.phase_complete:
        return None
    return apply_phase_transition(
        cursor, ctx, mv, writer=writer, in_prep=in_prep, in_check=in_check
    )
