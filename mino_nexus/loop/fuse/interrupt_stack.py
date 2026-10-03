"""意外挡屏：interrupt 栈（P2），主图 id 不变，短恢复序列压栈执行。"""
from __future__ import annotations

from typing import Any, Optional

from mino_nexus.loop.llm_step_context import step_scope_key
from mino_nexus.loop.milestone_orchestrator import ensure_single_in_progress, in_progress_milestone
from mino_nexus.loop.milestones import _normalize_milestone_row, milestone_v1_enabled, read_state, write_state, _sync_scope

SCHEMA = "mino.interrupt_stack.v1"
MAX_DEPTH = 2
_FOCUS_FAIL_BEFORE_PUSH = 2
_PROMOTE_AFTER = 2

def _resolve_steps(cursor: Any, ctx: Any, *, reason: str) -> list[dict[str, Any]]:
    from mino_nexus.loop.interrupt_recovery import resolve_interrupt_steps

    router = getattr(ctx, "router", None) if ctx is not None else None
    return resolve_interrupt_steps(
        cursor,
        ctx,
        router=router,
        reason=reason,
    )


def _stack(cursor: Any) -> list[dict[str, Any]]:
    raw = getattr(cursor, "interrupt_stack_v1", None)
    if isinstance(raw, list):
        return [x for x in raw if isinstance(x, dict)]
    return []


def _set_stack(cursor: Any, frames: list[dict[str, Any]]) -> None:
    cursor.interrupt_stack_v1 = list(frames)


def stack_depth(cursor: Any) -> int:
    return len(_stack(cursor))


def _note_promote_hint(cursor: Any, parent_id: str, reason: str, writer: Any) -> None:
    """同一父步连续打断成功：只发提示，不改用例。"""
    pid = str(parent_id or "").strip()
    if not pid:
        return
    bag = getattr(cursor, "interrupt_promote_v1", None)
    if not isinstance(bag, dict):
        bag = {}
    n = int(bag.get(pid) or 0) + 1
    bag[pid] = n
    cursor.interrupt_promote_v1 = bag
    if n < _PROMOTE_AFTER or writer is None:
        return
    writer.append(
        "interrupt/promote_hint",
        {
            "parent_step_id": pid,
            "reason": str(reason or "")[:120],
            "success_streak": n,
            "hint": "可收进逻辑块或 optional 边；需人工确认，默认不改用例",
        },
    )


def note_in_progress_tool_fail(
    cursor: Any,
    ctx: Any,
    *,
    writer: Any = None,
    reason: str = "",
) -> bool:
    """连续失败达阈值 → push interrupt（若未超深度）。"""
    if not milestone_v1_enabled():
        return False
    if stack_depth(cursor) >= MAX_DEPTH:
        return False
    state = read_state(cursor)
    focus = in_progress_milestone(state)
    if not focus:
        return False
    fid = str(focus.get("id") or "").strip()
    streak = int(focus.get("fail_streak") or 0) + 1
    ms = [dict(m) for m in (state.get("milestones") or []) if isinstance(m, dict)]
    for row in ms:
        if str(row.get("id") or "") == fid:
            row["fail_streak"] = streak
            break
    state = _sync_scope(cursor, state)
    state["milestones"] = ms
    write_state(cursor, state)
    if streak < _FOCUS_FAIL_BEFORE_PUSH:
        return False
    return push_interrupt(
        cursor,
        ctx,
        parent_step_id=str(focus.get("id") or ""),
        reason=reason or "focus_fail",
        steps=_resolve_steps(cursor, ctx, reason=reason or "focus_fail"),
        writer=writer,
    )


def push_interrupt(
    cursor: Any,
    ctx: Any,
    *,
    parent_step_id: str,
    reason: str,
    steps: list[dict[str, Any]],
    writer: Any = None,
) -> bool:
    if not milestone_v1_enabled():
        return False
    pid = str(parent_step_id or "").strip()
    if not pid:
        return False
    frames = _stack(cursor)
    if len(frames) >= MAX_DEPTH:
        return False
    state = _sync_scope(cursor, read_state(cursor))
    ms = [dict(m) for m in (state.get("milestones") or []) if isinstance(m, dict)]
    saved = [dict(m) for m in ms]
    for row in saved:
        if str(row.get("id") or "") == pid and str(row.get("status") or "") == "in_progress":
            row["status"] = "blocked"
            row["block_reason"] = str(reason or "")[:200]
            break
    interrupt_ms: list[dict[str, Any]] = []
    for i, raw in enumerate(steps):
        row = _normalize_milestone_row(raw, warnings=[])
        if not row:
            continue
        iid = str(row.get("id") or f"interrupt_{pid}_{i}")[:64]
        row["id"] = iid
        row["source"] = "interrupt"
        row["parent_step_id"] = pid
        row["status"] = "pending"
        interrupt_ms.append(row)
    if not interrupt_ms:
        return False
    frame = {
        "schema": SCHEMA,
        "parent_step_id": pid,
        "reason": str(reason or "")[:200],
        "saved_milestones": saved,
        "interrupt_ids": [str(m.get("id") or "") for m in interrupt_ms],
    }
    frames.append(frame)
    _set_stack(cursor, frames)
    state["milestones"] = interrupt_ms
    state["status"] = "in_progress"
    write_state(cursor, state)
    ensure_single_in_progress(cursor, writer=writer)
    if writer:
        case_step, phase = step_scope_key(cursor)
        writer.append(
            "interrupt/push",
            {
                "schema": SCHEMA,
                "parent_step_id": pid,
                "reason": reason[:120],
                "depth": len(frames),
                "step_count": len(interrupt_ms),
                "case_step": case_step,
                "phase": phase,
                "stack": [
                    {
                        "parent_step_id": f.get("parent_step_id"),
                        "reason": str(f.get("reason") or "")[:80],
                    }
                    for f in frames
                ],
            },
        )
    return True


def maybe_pop_interrupt(cursor: Any, *, writer: Any = None) -> bool:
    """interrupt 步全部终态 → 恢复主图并继续 parent。"""
    frames = _stack(cursor)
    if not frames:
        return False
    state = read_state(cursor)
    ms = [m for m in (state.get("milestones") or []) if isinstance(m, dict)]
    frame = frames[-1]
    iids = {str(x) for x in (frame.get("interrupt_ids") or []) if str(x)}
    if not iids:
        return False
    open_interrupt = False
    interrupt_passed = True
    for row in ms:
        if str(row.get("id") or "") not in iids:
            continue
        st = str(row.get("status") or "pending").strip().lower()
        if st in ("pending", "in_progress"):
            open_interrupt = True
            break
        if st not in ("passed", "skipped"):
            interrupt_passed = False
    if open_interrupt:
        return False
    parent_id = str(frame.get("parent_step_id") or "")
    restored = [dict(m) for m in (frame.get("saved_milestones") or []) if isinstance(m, dict)]
    for row in restored:
        if str(row.get("id") or "") == parent_id:
            row["status"] = "in_progress"
            row.pop("block_reason", None)
            row["fail_streak"] = 0
    frames.pop()
    _set_stack(cursor, frames)
    state = _sync_scope(cursor, read_state(cursor))
    state["milestones"] = restored
    state["status"] = "in_progress"
    write_state(cursor, state)
    ensure_single_in_progress(cursor, writer=writer)
    if writer:
        case_step, phase = step_scope_key(cursor)
        writer.append(
            "interrupt/pop",
            {
                "parent_step_id": parent_id,
                "depth": len(frames),
                "case_step": case_step,
                "phase": phase,
                "focus_restored": parent_id,
            },
        )
        if interrupt_passed:
            _note_promote_hint(cursor, parent_id, str(frame.get("reason") or ""), writer)
    return True


def try_push_on_fuse_block(
    cursor: Any,
    ctx: Any,
    *,
    writer: Any = None,
    message: str = "",
) -> bool:
    text = str(message or "")
    if "陷入死循环" in text or "强制停止" in text:
        return False
    gate = getattr(cursor, "progress_gate", None)
    if gate is not None and str(getattr(gate, "last_intervention", "") or "") == "stop":
        return False
    if stack_depth(cursor) >= MAX_DEPTH:
        return False
    focus = in_progress_milestone(read_state(cursor))
    if not focus:
        return False
    return push_interrupt(
        cursor,
        ctx,
        parent_step_id=str(focus.get("id") or ""),
        reason=f"fuse:{message[:80]}",
        steps=_resolve_steps(cursor, ctx, reason=f"fuse:{message[:80]}"),
        writer=writer,
    )


__all__ = [
    "note_in_progress_tool_fail",
    "push_interrupt",
    "maybe_pop_interrupt",
    "try_push_on_fuse_block",
    "stack_depth",
]
