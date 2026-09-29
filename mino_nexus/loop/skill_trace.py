"""把 StepCursor + 执行步骤编成结果信封的 slots。"""
from __future__ import annotations

from typing import Any, Optional


def _status_of(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "queued"
    order = ("fail", "blocked", "running", "pass", "skipped", "queued")
    bag = {str(r.get("status") or "queued") for r in rows}
    for key in order:
        if key in bag:
            return key
        if key == "fail" and bag & {"failed", "give_up", "declined"}:
            return "fail"
        if key == "running" and bag & {"thinking", "checking", "continue"}:
            return "running"
    return "pass" if rows else "queued"


def _ids(rows: list[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    for row in rows:
        seq = row.get("seq") or row.get("step")
        if seq is None:
            continue
        out.append(str(seq))
    return out


def build_slots(
    cursor,
    steps: list[dict[str, Any]],
    *,
    run_status: str = "",
    run_summary: str = "",
) -> dict[str, list[dict[str, Any]]]:
    by_phase: dict[str, list[dict[str, Any]]] = {"prep": [], "do": [], "check": []}
    for row in steps or []:
        phase = str(row.get("loop_phase") or "")
        if phase in by_phase:
            by_phase[phase].append(row)

    prep_rows = by_phase["prep"]
    prep_title = str(getattr(cursor, "precondition", "") or "").strip() or "前置"
    loop_phase = str(getattr(cursor, "phase", "") or "")
    prep = []
    if prep_title or prep_rows:
        if prep_rows:
            prep_status = _status_of(prep_rows)
        elif loop_phase == "prep":
            prep_status = "queued"
        else:
            prep_status = "pass"
        prep.append({
            "id": "prep",
            "title": prep_title.splitlines()[0][:80] if prep_title else "前置",
            "status": prep_status,
            "step_ids": _ids(prep_rows),
        })

    ops: list[dict[str, Any]] = []
    checks: list[dict[str, Any]] = []
    nodes = list(getattr(cursor, "nodes", None) or [])
    for node in nodes:
        n = int(getattr(node, "n", 0) or 0)
        inst = str(getattr(node, "instruction", "") or "").strip()
        exp = str(getattr(node, "expected", "") or "").strip()
        do_rows = [r for r in by_phase["do"] if int(r.get("case_step") or r.get("case_step_index") or 0) == n]
        ck_rows = [r for r in by_phase["check"] if int(r.get("case_step") or r.get("case_step_index") or 0) == n]
        if inst:
            op_st = _status_of(do_rows)
            if not do_rows and loop_phase in ("check", "done") and int(getattr(cursor, "step_ops", 0) or 0) == 0:
                op_st = "skipped"
            ops.append({
                "id": f"op-{n}",
                "title": inst,
                "status": op_st,
                "step_ids": _ids(do_rows),
                "step_num": n,
            })
        if exp:
            ck_st = _status_of(ck_rows)
            if not ck_rows and str(run_status or "").strip().lower() in ("fail", "failed"):
                ck_st = "fail"
            checks.append({
                "id": f"check-{n}",
                "title": exp,
                "status": ck_st,
                "step_ids": _ids(ck_rows),
                "step_num": n,
            })
    return {"prep": prep, "ops": ops, "checks": checks}


def envelope(
    *,
    skill: Optional[dict[str, Any]],
    cursor=None,
    steps: Optional[list[dict[str, Any]]] = None,
    status: str = "",
    summary: str = "",
) -> dict[str, Any]:
    skill = skill if isinstance(skill, dict) else {}
    view = skill.get("view") if isinstance(skill.get("view"), dict) else {}
    view_id = str(view.get("id") or skill.get("view_id") or "job-timeline")
    slots = (
        build_slots(cursor, steps or [], run_status=status, run_summary=summary)
        if cursor is not None
        else {"prep": [], "ops": [], "checks": []}
    )
    case_step = 0
    loop_phase = ""
    if cursor is not None:
        loop_phase = str(getattr(cursor, "phase", "") or "")
        cur_node = getattr(cursor, "current", lambda: None)()
        case_step = 0 if loop_phase == "prep" else int(getattr(cur_node, "n", 0) or 0)
    return {
        "skill_id": str(skill.get("id") or ""),
        "view_id": view_id,
        "status": status,
        "summary": summary,
        "slots": slots,
        "case_step": case_step,
        "loop_phase": loop_phase,
    }
