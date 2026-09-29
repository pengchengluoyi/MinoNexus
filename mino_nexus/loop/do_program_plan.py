"""do 阶段：step_program_keys → 子里程碑 + do_program_plan（供 agent-vision-plan 只读）。"""
from __future__ import annotations

import json
from typing import Any

from mino_nexus.loop.llm_step_context import step_scope_key
from mino_nexus.loop.milestones import (
    _log_milestones_snapshot,
    milestone_v1_enabled,
    milestones_empty,
    milestones_from_flow_steps,
    read_state,
    write_state,
    _sync_scope,
)
from mino_nexus.services.case_step_key_compiler import (
    build_do_program_plan_for_step,
    step_bundle,
    sync_case_step_program_keys,
)
from mino_nexus.services.nav_flow_block_catalog import resolve_effective_steps


def _case_with_meta(case: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(case, dict):
        return {}
    meta = case.get("meta")
    if isinstance(meta, dict) and meta.get("step_program_keys"):
        return case
    return sync_case_step_program_keys(dict(case))


def resolve_do_program_plan(
    case: dict[str, Any] | None,
    cursor: Any,
    *,
    ctx: Any = None,
) -> dict[str, Any]:
    case_step, phase = step_scope_key(cursor)
    if phase != "do" or case_step <= 0:
        return {}
    cached = getattr(cursor, "do_program_plan", None)
    if isinstance(cached, dict) and int(cached.get("case_step") or 0) == case_step:
        return cached
    c = _case_with_meta(case)
    bundle = step_bundle(c, case_step)
    plan = bundle.get("do_program_plan") if isinstance(bundle.get("do_program_plan"), dict) else None
    if not plan or not plan.get("steps"):
        cur = cursor.current() if hasattr(cursor, "current") else None
        instr = str(getattr(cur, "instruction", "") or "").strip()
        plan = build_do_program_plan_for_step(case_step, instr)
    setattr(cursor, "do_program_plan", plan)
    return plan


def milestones_from_do_program(plan: dict[str, Any], *, app_id: str = "", ctx: Any = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for step in plan.get("steps") or []:
        if not isinstance(step, dict):
            continue
        kind = str(step.get("kind") or "visual_action").strip().lower()
        sid = str(step.get("id") or "").strip() or f"do_{len(rows)}"
        if kind == "flow_block":
            bid = str(step.get("block_id") or "").strip()
            if bid:
                block_steps = resolve_effective_steps(app_id=str(app_id or ""), block_id=bid)
                block_ms = milestones_from_flow_steps(block_steps, block_id=bid)
                for m in block_ms:
                    row = dict(m)
                    row["source"] = "do_program"
                    row["source_block"] = bid
                    row["key_ref"] = str(step.get("key_ref") or "")
                    rows.append(row)
                continue
        row: dict[str, Any] = {
            "id": sid,
            "title": str(step.get("title") or sid)[:240],
            "kind": "hook" if kind == "hook" else ("visual_action" if kind == "nav" else kind),
            "status": "pending",
            "optional": bool(step.get("optional")),
            "source": "do_program",
            "key_ref": str(step.get("key_ref") or ""),
        }
        caps = step.get("exec_caps")
        if isinstance(caps, list) and caps:
            row["exec_caps"] = [str(x) for x in caps if str(x).strip()]
        if kind == "hook" or step.get("hook_cap"):
            row["kind"] = "hook"
            row["hook_cap"] = str(step.get("hook_cap") or "tap_element")
        if kind == "nav" and step.get("nav_target"):
            row["hook_cap"] = "fsm_navigate"
            row["nav_target"] = str(step.get("nav_target") or "")
        rows.append(row)
    return rows


def seed_do_milestones_from_plan(
    cursor: Any,
    ctx: Any,
    case: dict[str, Any] | None,
    *,
    writer: Any = None,
    app_id: str = "",
) -> dict[str, Any] | None:
    if not milestone_v1_enabled():
        return None
    _, phase = step_scope_key(cursor)
    if phase != "do":
        return None
    state = _sync_scope(cursor, read_state(cursor))
    if not milestones_empty(state):
        return getattr(cursor, "do_program_plan", None)
    plan = resolve_do_program_plan(case, cursor, ctx=ctx)
    steps = plan.get("steps") if isinstance(plan.get("steps"), list) else []
    if not steps:
        if writer and plan.get("fallback_lines"):
            _log_key_fallback(writer, case, cursor, plan.get("fallback_lines") or [])
        return plan
    rows = milestones_from_do_program(plan, app_id=app_id, ctx=ctx)
    if not rows:
        return plan
    state["milestones"] = rows
    state["status"] = "in_progress"
    block_id = ""
    for step in steps:
        if not isinstance(step, dict):
            continue
        if str(step.get("kind") or "").strip().lower() != "flow_block":
            continue
        bid = str(step.get("block_id") or "").strip()
        if bid:
            block_id = bid
            break
    if not block_id:
        for row in rows:
            if isinstance(row, dict) and str(row.get("source_block") or "").strip():
                block_id = str(row["source_block"]).strip()
                break
    if block_id:
        state["block_ref"] = {
            "block_id": block_id,
            "from_milestone": "program_seed:do_keys",
        }
    write_state(cursor, state)
    _log_milestones_snapshot(writer, read_state(cursor), source="program_seed:do_keys")
    if writer:
        try:
            writer.append(
                "milestone/program_seed",
                {
                    "phase": "do",
                    "source": "do_keys",
                    "case_step": plan.get("case_step"),
                    "step_count": len(rows),
                },
            )
        except Exception:  # noqa: BLE001
            pass
    return plan


def do_program_plan_json(case: dict[str, Any] | None, cursor: Any, ctx: Any = None) -> str:
    plan = resolve_do_program_plan(case, cursor, ctx=ctx)
    return json.dumps(plan or {}, ensure_ascii=False, indent=2, default=str)


def has_do_program_steps(cursor: Any, case: dict[str, Any] | None) -> bool:
    plan = resolve_do_program_plan(case, cursor)
    return bool(plan.get("steps"))


def _log_key_fallback(writer: Any, case: dict[str, Any] | None, cursor: Any, lines: list[Any]) -> None:
    if not writer or not lines:
        return
    case_step, phase = step_scope_key(cursor)
    try:
        writer.append(
            "key_compile/fallback",
            {
                "telemetry": "red",
                "phase": phase,
                "case_step": case_step,
                "lines": lines[:12],
            },
        )
    except Exception:  # noqa: BLE001
        pass
