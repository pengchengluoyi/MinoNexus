"""check 阶段：step_program_keys → checkpoint 里程碑 + check_program_plan。"""
from __future__ import annotations

import json
from typing import Any

from mino_nexus.loop.llm_step_context import step_scope_key
from mino_nexus.loop.milestones import (
    _log_milestones_snapshot,
    milestone_v1_enabled,
    milestones_empty,
    milestones_from_check_plan,
    read_state,
    write_state,
    _sync_scope,
)
from mino_nexus.services.case_step_key_compiler import (
    build_check_program_plan_for_step,
    step_bundle,
    sync_case_step_program_keys,
)


def _case_with_meta(case: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(case, dict):
        return {}
    meta = case.get("meta")
    if isinstance(meta, dict) and meta.get("step_program_keys"):
        return case
    return sync_case_step_program_keys(dict(case))


def resolve_check_program_plan(case: dict[str, Any] | None, cursor: Any) -> dict[str, Any]:
    case_step, phase = step_scope_key(cursor)
    if phase != "check" or case_step <= 0:
        return {}
    cached = getattr(cursor, "check_program_plan", None)
    if isinstance(cached, dict) and int(cached.get("case_step") or 0) == case_step:
        return cached
    c = _case_with_meta(case)
    bundle = step_bundle(c, case_step)
    plan = bundle.get("check_program_plan") if isinstance(bundle.get("check_program_plan"), dict) else None
    if not plan or not plan.get("checkpoints"):
        cur = cursor.current() if hasattr(cursor, "current") else None
        exp = str(getattr(cur, "expected", "") or "").strip()
        instr = str(getattr(cur, "instruction", "") or "").strip()
        plan = build_check_program_plan_for_step(case_step, exp, instruction=instr)
    setattr(cursor, "check_program_plan", plan)
    return plan


def milestones_from_check_program(plan: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for cp in plan.get("checkpoints") or []:
        if not isinstance(cp, dict):
            continue
        cid = str(cp.get("id") or "").strip() or f"ck{len(rows)}"
        assert_payload = cp.get("assert") if isinstance(cp.get("assert"), dict) else {}
        rows.append(
            {
                "id": cid,
                "title": str(cp.get("title") or cid)[:240],
                "kind": "checkpoint",
                "status": "pending",
                "optional": bool(cp.get("optional")),
                "source": str(cp.get("source") or "check_program"),
                "key_ref": str(cp.get("key_ref") or ""),
                "checkpoint_kind": str(assert_payload.get("mode") or "vlm"),
                "assert": assert_payload,
            }
        )
    return rows


def seed_check_milestones_from_plan(
    cursor: Any,
    case: dict[str, Any] | None,
    *,
    writer: Any = None,
) -> dict[str, Any] | None:
    if not milestone_v1_enabled():
        return None
    _, phase = step_scope_key(cursor)
    if phase != "check":
        return None
    state = _sync_scope(cursor, read_state(cursor))
    plan = resolve_check_program_plan(case, cursor)
    cps = plan.get("checkpoints") if isinstance(plan.get("checkpoints"), list) else []
    if cps and (milestones_empty(state) or _only_generic_check_seed(state)):
        rows = milestones_from_check_program(plan)
        if rows:
            state["milestones"] = rows
            state["status"] = "in_progress"
            write_state(cursor, state)
            _log_milestones_snapshot(writer, read_state(cursor), source="program_seed:check_keys")
            if writer:
                try:
                    writer.append(
                        "milestone/program_seed",
                        {
                            "phase": "check",
                            "source": "check_keys",
                            "case_step": plan.get("case_step"),
                            "checkpoint_count": len(rows),
                        },
                    )
                except Exception:  # noqa: BLE001
                    pass
    elif writer and plan.get("fallback_lines"):
        from mino_nexus.loop.do_program_plan import _log_key_fallback

        _log_key_fallback(writer, case, cursor, plan.get("fallback_lines") or [])
    return plan


def _only_generic_check_seed(state: dict[str, Any]) -> bool:
    ms = state.get("milestones") if isinstance(state.get("milestones"), list) else []
    if not ms:
        return True
    return not any(isinstance(m, dict) and m.get("source") == "check_program" for m in ms)


def seed_check_milestones_after_enter(
    cursor: Any,
    case: dict[str, Any] | None,
    *,
    expected: str = "",
    instruction: str = "",
    writer: Any = None,
) -> None:
    """替代/增强 on_enter_check_phase 的纯 build_check_plan 种子。"""
    plan = resolve_check_program_plan(case, cursor)
    if plan.get("checkpoints"):
        seed_check_milestones_from_plan(cursor, case, writer=writer)
        return
    if not milestone_v1_enabled():
        return
    state = _sync_scope(cursor, read_state(cursor))
    if milestones_empty(state):
        state["milestones"] = milestones_from_check_plan(expected, instruction=instruction)
        state["status"] = "in_progress"
        write_state(cursor, state)


def check_program_plan_json(case: dict[str, Any] | None, cursor: Any) -> str:
    plan = resolve_check_program_plan(case, cursor)
    return json.dumps(plan or {}, ensure_ascii=False, indent=2, default=str)
