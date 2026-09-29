"""进 phase / vision 编排前：程序种子里程碑（与 vision-plan 门控解耦）。"""
from __future__ import annotations

from typing import Any

from mino_nexus.loop.llm_step_context import step_scope_key
from mino_nexus.loop.milestones import milestone_v1_enabled, milestones_empty, read_state


def _run_case(case: dict[str, Any] | None, ctx: Any) -> dict[str, Any] | None:
    if isinstance(case, dict):
        return case
    raw = getattr(ctx, "case", None) if ctx is not None else None
    return raw if isinstance(raw, dict) else None


def ensure_program_milestone_seeds(
    cursor: Any,
    ctx: Any,
    case: dict[str, Any] | None = None,
    *,
    writer: Any = None,
    app_id: str = "",
    include_prep: bool = False,
) -> None:
    """有 do/check_program_plan 时写入里程碑骨架；不依赖 vision_plan 开关。"""
    if not milestone_v1_enabled():
        return
    _, phase = step_scope_key(cursor)
    ph = str(phase or getattr(cursor, "phase", "") or "").strip().lower()
    run_case = _run_case(case, ctx)
    aid = str(app_id or getattr(ctx, "app_id", "") or "")

    if include_prep and ph == "prep":
        from mino_nexus.loop.prep_program_plan import (
            seed_prep_milestones_from_claim,
            sync_prep_internal_milestones,
        )

        seed_prep_milestones_from_claim(cursor, ctx, run_case, writer=writer)
        sync_prep_internal_milestones(cursor, ctx)

    if ph == "do" and milestones_empty(read_state(cursor)):
        from mino_nexus.loop.do_program_plan import seed_do_milestones_from_plan

        seed_do_milestones_from_plan(
            cursor,
            ctx,
            run_case,
            writer=writer,
            app_id=aid,
        )

    if ph == "check":
        from mino_nexus.loop.check_program_plan import seed_check_milestones_from_plan

        seed_check_milestones_from_plan(cursor, run_case, writer=writer)

    if ph in ("do", "check", "prep"):
        from mino_nexus.loop.milestone_orchestrator import ensure_single_in_progress

        ensure_single_in_progress(cursor, writer=writer)


__all__ = ["ensure_program_milestone_seeds"]
