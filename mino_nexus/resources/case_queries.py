"""用例行的只读查询：资源域直接用 SQL 读，不 import 用例域。

同一张 sqlite 表上的读取不构成模块依赖——用例的写入口仍在 `casework`。
"""
from __future__ import annotations

from typing import Any


def count_cases(project_id: str, requirement_id: str = "") -> int:
    from mino_nexus.core.database import SessionLocal, ensure_db
    from mino_nexus.models.case import ProjectCase

    pid = str(project_id or "").strip()
    if not pid:
        return 0
    ensure_db()
    db = SessionLocal()
    try:
        query = db.query(ProjectCase).filter(ProjectCase.project_id == pid)
        rid = str(requirement_id or "").strip()
        if rid:
            query = query.filter(ProjectCase.requirement_id == rid)
        return query.count()
    finally:
        db.close()


def list_cases(project_id: str, requirement_id: str = "") -> list[dict[str, Any]]:
    from mino_nexus.core.database import SessionLocal, ensure_db
    from mino_nexus.models.case import ProjectCase

    pid = str(project_id or "").strip()
    if not pid:
        return []
    ensure_db()
    db = SessionLocal()
    try:
        query = db.query(ProjectCase).filter(ProjectCase.project_id == pid)
        rid = str(requirement_id or "").strip()
        if rid:
            query = query.filter(ProjectCase.requirement_id == rid)
        rows = query.order_by(ProjectCase.sort_index, ProjectCase.pk).all()
        return [case_public(row) for row in rows]
    finally:
        db.close()


def list_project_requirements(project_id: str) -> list[dict[str, Any]]:
    """聚合项目下各 App 的 qa_process.requirements（按 id 去重）。"""
    from mino_nexus.resources import app_automation as aas
    from mino_nexus.resources import project_store as ps

    pid = str(project_id or "").strip()
    if not pid or ps.find_project(pid) is None:
        return []
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for app in ps.list_apps(pid):
        app_row = ps.find_app(str(app.get("id") or ""))
        if not app_row:
            continue
        qp = aas.get_automation_config(app_row, hydrate_cases=False).get("qa_process") or {}
        for req in qp.get("requirements") or []:
            if not isinstance(req, dict):
                continue
            rid = str(req.get("id") or "").strip()
            if not rid or rid in seen:
                continue
            seen.add(rid)
            out.append({
                "id": rid,
                "title": str(req.get("title") or req.get("external_id") or rid),
                "external_id": str(req.get("external_id") or ""),
            })
    return out


def case_public(row) -> dict[str, Any]:
    extra = dict(row.extra or {}) if isinstance(row.extra, dict) else {}
    out = {
        **extra,
        "case_id": row.case_id,
        "project_id": row.project_id,
        "name": row.name or extra.get("name") or row.case_id,
        "module": row.module or extra.get("module") or "",
        "platform": row.platform or extra.get("platform") or "",
        "aspect": row.aspect or extra.get("aspect") or "正向",
        "precondition": row.precondition or extra.get("precondition") or "",
        "steps": list(row.steps or extra.get("steps") or []),
        "expected": list(row.expected or extra.get("expected") or []),
        "steps_raw": row.steps_raw or extra.get("steps_raw") or "",
        "expected_raw": row.expected_raw or extra.get("expected_raw") or "",
        "point_ids": list(row.point_ids or extra.get("point_ids") or []),
        "source": row.source or extra.get("source") or "",
        "requirement_id": row.requirement_id or extra.get("requirement_id") or "",
        "index": row.sort_index,
        "updated_at": row.updated_at or "",
    }
    from mino_nexus.contract.case_preview import stored_preview_lines

    if out.get("source_revision_id"):
        from mino_nexus.contract.case_view import readiness
        meta = dict(out.get("meta") or {})
        issue = readiness(out)
        if issue:
            spk = meta.get("step_program_keys") if isinstance(meta.get("step_program_keys"), dict) else {}
            job_status = str((out.get("compile_job") or {}).get("status") or out.get("compile_state") or "").lower()
            in_flight = job_status in ("queued", "running", "claimed", "preparing")
            if not (spk and in_flight):
                # Do not display an old frozen plan as the understanding of new text.
                meta["step_program_keys"] = {}
                for key in ("steps_parsed", "expected_parsed", "automation_details", "precondition_parsed", "payload"):
                    out.pop(key, None)
                out["meta"] = meta
                return out
            out["meta"] = meta
        if "v5_draft_step_program_keys" in meta:
            meta["step_program_keys"] = meta["v5_draft_step_program_keys"]
            out["meta"] = meta
    steps_parsed, expected_parsed = stored_preview_lines(out)
    out["steps_parsed"] = steps_parsed
    out["expected_parsed"] = expected_parsed
    return out
