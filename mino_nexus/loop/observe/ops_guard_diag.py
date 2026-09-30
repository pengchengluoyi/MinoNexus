"""运维向 guard / 探针诊断字段（写入 session_events guard/block）。"""
from __future__ import annotations

from typing import Any


def enrich_guard_block_payload(
    base: dict[str, Any],
    *,
    guard_ctx: dict[str, Any] | None,
    cur: Any = None,
    probe_diag: dict[str, Any] | None = None,
) -> dict[str, Any]:
    out = dict(base or {})
    ctx = guard_ctx if isinstance(guard_ctx, dict) else {}
    step_cursor = ctx.get("step_cursor")
    if cur is not None:
        out["step_n"] = int(getattr(cur, "n", 0) or 0)
        out["instruction"] = str(getattr(cur, "instruction", "") or "")[:120]
        out["expected"] = str(getattr(cur, "expected", "") or "")[:120]
    elif step_cursor is not None:
        c = getattr(step_cursor, "current", lambda: None)()
        if c is not None:
            out["step_n"] = int(getattr(c, "n", 0) or 0)
            out["instruction"] = str(getattr(c, "instruction", "") or "")[:120]
            out["expected"] = str(getattr(c, "expected", "") or "")[:120]
    out["phase"] = str(ctx.get("phase") or "")
    loc = ctx.get("nav_localized") if isinstance(ctx.get("nav_localized"), dict) else {}
    out["nav_state_id"] = str(loc.get("chosen") or "")
    try:
        out["nav_confidence"] = float(loc.get("confidence") or 0)
    except (TypeError, ValueError):
        out["nav_confidence"] = 0.0
    out["screen_fp"] = str(ctx.get("screen_fp") or "")[:16]
    if step_cursor is not None:
        pg = getattr(step_cursor, "progress_gate", None)
        if pg is not None:
            out["fuse_block_streak"] = int(getattr(pg, "fuse_block_streak", 0) or 0)
            out["no_progress_streak"] = int(getattr(pg, "no_progress_streak", 0) or 0)
            states = list(getattr(pg, "_post_states", None) or [])
            if states:
                out["fuse_state_tail"] = states[-6:]
        out["require_do_work_streak"] = int(getattr(step_cursor, "require_do_work_streak", 0) or 0)
        done = getattr(step_cursor, "step_intents_done", None)
        if done:
            out["intents_done"] = sorted(str(x) for x in done if str(x))[:12]
    if probe_diag:
        out["probe"] = dict(probe_diag)
    return out


def note_guard_block(step_cursor: Any, payload: dict[str, Any]) -> None:
    if step_cursor is None:
        return
    gid = str(payload.get("guard_id") or payload.get("dispatch_gate_code") or "unknown")
    counts = getattr(step_cursor, "guard_block_counts", None)
    if not isinstance(counts, dict):
        counts = {}
        setattr(step_cursor, "guard_block_counts", counts)
    counts[gid] = int(counts.get(gid) or 0) + 1
    setattr(step_cursor, "last_guard_block", dict(payload))


def session_end_guard_summary(step_cursor: Any) -> dict[str, Any]:
    if step_cursor is None:
        return {}
    return {
        "guard_block_counts": dict(getattr(step_cursor, "guard_block_counts", None) or {}),
        "last_guard_block": dict(getattr(step_cursor, "last_guard_block", None) or {}),
        "require_do_work_streak": int(getattr(step_cursor, "require_do_work_streak", 0) or 0),
    }
