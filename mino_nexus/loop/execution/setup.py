"""跑批初始化：从用例与 skill 算出这次运行的骨架（预算、游标、导航、槽位）。

纯装配，不碰设备；执行/守卫/收尾都在 `agent_loop._run_loop` 里。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mino_nexus.ai.skill_store import get_skill
from mino_nexus.contract.step_pointer import build_seq_nodes, compute_case_wall_budget_sec
from mino_nexus.loop.nav_runtime import NavRuntime
from mino_nexus.loop.sop_runtime import normalize_inspections, normalize_phases
from mino_nexus.loop.step_pointer import StepCursor

_TURN_SAFETY_CAP = 500


@dataclass
class RunSetup:
    case: dict[str, Any]
    skill: dict[str, Any]
    sop: dict[str, Any]
    phases: Any
    inspections: Any
    wall_budget_sec: float
    max_steps: int
    cursor: Any
    inspect_slots: dict[str, str]
    doc_stuck_used: int
    doc_stuck_budget: int
    nav: Any
    target_pkg: str


def prepare_run(
    *,
    case: dict[str, Any] | None,
    resolved_case: Any,
    run_type: str,
    ctx: Any,
    provider_id: str,
    scout_run_id: str,
    overview: str = "",
) -> RunSetup:

    if resolved_case is not None:
        case = resolved_case.case
    skill = get_skill("run-case") or {}
    sop = skill.get("sop") if isinstance(skill.get("sop"), dict) else {}
    phases = normalize_phases(sop.get("phases"))
    inspections = normalize_inspections(sop.get("inspections") if "inspections" in sop else None)
    wall_budget_sec = 0
    try:
        wall_budget_sec = compute_case_wall_budget_sec(case)
        max_steps = _TURN_SAFETY_CAP
    except (TypeError, ValueError):
        max_steps = _TURN_SAFETY_CAP
        wall_budget_sec = compute_case_wall_budget_sec(case)
    if resolved_case is not None:
        wall_budget_sec = resolved_case.wall_budget_sec
        max_steps = resolved_case.max_steps
    cursor = StepCursor(
        build_seq_nodes(case),
        precondition=str(case.get("precondition") or "").strip(),
    )
    if resolved_case is not None:
        cursor.resolved_case = resolved_case
        cursor.execution_context = ctx
        cursor.verified_check_points = {}
        cursor.phase = "prep"
        meta = case.get("meta") if isinstance(case.get("meta"), dict) else {}
        summary = meta.get("intent_summary") if isinstance(meta.get("intent_summary"), dict) else {}
        first_intent = next((str(x) for x in (summary.get("intents") or []) if str(x)), "")
        if first_intent:
            setattr(cursor, "current_intent_id", first_intent)
    ctx._step_cursor = cursor
    from mino_nexus.loop.step_contract import bind_run_cursor

    bind_run_cursor(cursor)
    inspect_slots: dict[str, str] = {
        "session_block": "", "hierarchy_text": "", "knowledge_hint": "", "knowledge_body": "",
        "nav_assist": "", "doc_context": "",
    }
    try:
        from mino_nexus.knowledge.doc_embed import bootstrap_case_doc_query

        _app_id = str(getattr(ctx, "app_id", "") or "").strip()
        _qvec = bootstrap_case_doc_query(case=case, app_id=_app_id)
        if _qvec:
            ctx.doc_query_vec = _qvec
    except Exception:
        pass
    doc_stuck_used = 0
    doc_stuck_budget = 2
    # 开关关着 / 没配 NavFSM 时为 None，主循环下面三处调用全部跳过（loop/nav_runtime.py 顶部注释）
    nav = NavRuntime.for_run(
        ctx=ctx,
        case=case,
        run_type=run_type,
        run_id=scout_run_id,
        provider_id=provider_id,
    )

    target_pkg = str(getattr(ctx, "target_package", "") or "")

    return RunSetup(
        case=case,
        skill=skill,
        sop=sop,
        phases=phases,
        inspections=inspections,
        wall_budget_sec=wall_budget_sec,
        max_steps=max_steps,
        cursor=cursor,
        inspect_slots=inspect_slots,
        doc_stuck_used=doc_stuck_used,
        doc_stuck_budget=doc_stuck_budget,
        nav=nav,
        target_pkg=target_pkg,
    )
