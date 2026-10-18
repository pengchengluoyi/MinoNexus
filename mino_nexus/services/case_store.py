"""项目用例库。真源表 project_cases。"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Literal

ConflictAction = Literal["overwrite", "skip"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _public(row) -> dict[str, Any]:
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
    from mino_nexus.services.case_key_registry import stored_preview_lines

    if out.get("source_revision_id"):
        from mino_nexus.services.case_compilation import readiness
        meta = dict(out.get("meta") or {})
        if readiness(out):
            # Do not display an old frozen plan as the understanding of new text.
            meta["step_program_keys"] = {}
            for key in ("steps_parsed", "expected_parsed", "automation_details", "precondition_parsed", "payload"):
                out.pop(key, None)
            out["meta"] = meta
            return out
        if "v5_draft_step_program_keys" in meta:
            meta["step_program_keys"] = meta["v5_draft_step_program_keys"]
            out["meta"] = meta
    steps_parsed, expected_parsed = stored_preview_lines(out)
    out["steps_parsed"] = steps_parsed
    out["expected_parsed"] = expected_parsed
    return out


def allocate_case_id(project_id: str) -> str:
    pid = str(project_id or "").strip()
    if not pid:
        raise ValueError("缺少 project_id")
    for _ in range(32):
        cid = f"case-{uuid.uuid4().hex[:8]}"
        if get_case(pid, cid) is None:
            return cid
    raise ValueError("无法分配用例编号")


def get_case_ids(project_id: str, case_ids: list[str]) -> list[str]:
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.case import ProjectCase

    with session_scope() as db:
        return [row[0] for row in db.query(ProjectCase.case_id).filter(
            ProjectCase.project_id == project_id, ProjectCase.case_id.in_(case_ids)).all()]


def list_case_page(project_id: str, *, limit: int, offset: int = 0, search: str = "", module: str = "",
                   app_id: str = "", result: str = "all") -> dict[str, Any]:
    from sqlalchemy import func, or_
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.case import ProjectCase
    from mino_nexus.services.run_store import last_case_results

    results = last_case_results(app_id) if result != "all" else {}
    with session_scope() as db:
        base = db.query(ProjectCase).filter(ProjectCase.project_id == project_id)
        query = base
        if search:
            query = query.filter(or_(ProjectCase.case_id.contains(search, autoescape=True),
                                     ProjectCase.name.contains(search, autoescape=True),
                                     ProjectCase.module.contains(search, autoescape=True),
                                     ProjectCase.precondition.contains(search, autoescape=True)))
        if module:
            query = query.filter(func.coalesce(func.nullif(func.trim(ProjectCase.module), ""), "未分组") == module)
        if result == "failed":
            ids = [cid for cid, row in results.items() if str(row["status"] or "").lower() in ("fail", "failed", "error", "exception")]
            query = query.filter(ProjectCase.case_id.in_(ids))
        elif result == "never":
            query = query.filter(ProjectCase.case_id.not_in(list(results)))
        total = query.count()
        rows = query.order_by(ProjectCase.sort_index, ProjectCase.pk).limit(limit).offset(offset).all()
        cases = [{"case_id": row.case_id, "title": row.name or row.case_id, "name": row.name or row.case_id,
                  "module": row.module or "", "platform": row.platform or "", "precondition": (row.precondition or "")[:300],
                  "steps": [str(value)[:300] for value in (row.steps or [])[:3]],
                  "expected": [str(value)[:300] for value in (row.expected or [])[:3]],
                  **{key: (row.extra or {}).get(key) for key in ("source_revision", "source_revision_id", "compile_state", "compile_job", "effective_artifact")}} for row in rows]
        return {"cases": cases, "total": total, "limit": limit, "offset": offset, "project_id": project_id}


def case_modules(project_id: str) -> dict[str, Any]:
    from sqlalchemy import func
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.case import ProjectCase

    module = func.coalesce(func.nullif(func.trim(ProjectCase.module), ""), "未分组")
    with session_scope() as db:
        rows = db.query(module, func.count()).filter(ProjectCase.project_id == project_id).group_by(module).all()
        return {"items": [{"name": name, "count": count} for name, count in rows], "total": sum(row[1] for row in rows)}


def count_cases(project_id: str, requirement_id: str = "") -> int:
    from mino_nexus.core.database import SessionLocal, ensure_db
    from mino_nexus.models.case import ProjectCase

    pid = str(project_id or "").strip()
    if not pid:
        return 0
    ensure_db()
    db = SessionLocal()
    try:
        q = db.query(ProjectCase).filter(ProjectCase.project_id == pid)
        rid = str(requirement_id or "").strip()
        if rid:
            q = q.filter(ProjectCase.requirement_id == rid)
        return q.count()
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
        q = db.query(ProjectCase).filter(ProjectCase.project_id == pid)
        rid = str(requirement_id or "").strip()
        if rid:
            q = q.filter(ProjectCase.requirement_id == rid)
        rows = q.order_by(ProjectCase.sort_index, ProjectCase.pk).all()
        return [_public(r) for r in rows]
    finally:
        db.close()


def find_similar_case(
    project_id: str,
    requirement_id: str,
    row: dict[str, Any],
) -> dict[str, Any] | None:
    """同需求下按名称（+ 模块）找内容相同的已有用例。无编号导入时防重复。"""
    name = str(row.get("name") or "").strip()
    if not name:
        return None
    from sqlalchemy import func, or_
    from mino_nexus.core.database import SessionLocal, ensure_db
    from mino_nexus.models.case import ProjectCase

    module = str(row.get("module") or "").strip()
    rid = str(requirement_id or "").strip()
    ensure_db()
    database = SessionLocal()
    try:
        case_name = func.coalesce(func.nullif(ProjectCase.name, ""), func.nullif(ProjectCase.extra["name"].as_string(), ""), ProjectCase.case_id)
        query = database.query(ProjectCase).filter(ProjectCase.project_id == str(project_id or "").strip(), func.trim(case_name) == name)
        if rid:
            query = query.filter(ProjectCase.requirement_id == rid)
        if module:
            case_module = func.trim(func.coalesce(func.nullif(ProjectCase.module, ""), ProjectCase.extra["module"].as_string(), ""))
            query = query.filter(or_(case_module == "", case_module == module))
        match = query.order_by(ProjectCase.sort_index, ProjectCase.pk).first()
        return _public(match) if match else None
    finally:
        database.close()


def get_case(project_id: str, case_id: str) -> dict[str, Any] | None:
    from mino_nexus.core.database import SessionLocal, ensure_db
    from mino_nexus.models.case import ProjectCase

    pid = str(project_id or "").strip()
    cid = str(case_id or "").strip()
    if not pid or not cid:
        return None
    ensure_db()
    db = SessionLocal()
    try:
        row = (
            db.query(ProjectCase)
            .filter(ProjectCase.project_id == pid, ProjectCase.case_id == cid)
            .first()
        )
        return _public(row) if row else None
    finally:
        db.close()


def _row_fields(
    raw: dict[str, Any],
    *,
    project_id: str,
    requirement_id: str,
    sort_index: int,
    force_recompile_resource_key: bool = False,
    compiled: bool = False,
) -> dict[str, Any]:
    from copy import deepcopy
    from mino_nexus.services.v5_publication import guard_case_row
    from mino_nexus.services.case_compilation import CHAIN_FIELDS, COMPILED_META
    raw = deepcopy(raw)
    cid = str(raw.get("case_id") or "").strip()
    old = get_case(project_id, cid)
    for key in CHAIN_FIELDS:
        raw.pop(key, None)
    meta = dict(raw.get("meta") or {})
    for key in COMPILED_META:
        meta.pop(key, None)
    if old:
        for key in COMPILED_META:
            if key in (old.get("meta") or {}):
                meta[key] = deepcopy(old["meta"][key])
    raw["meta"] = meta
    # The source transaction is deterministic. Preview/client compiled output is
    # never an authoritative artifact; only the fenced worker can activate it.
    raw["name"] = str(raw.get("name") or raw.get("title") or cid)
    raw["precondition"] = str(raw.get("precondition") or raw.get("pre") or "")
    for field in ("steps", "expected"):
        raw[field] = list(raw.get(field) or [])
        raw[field + "_raw"] = str(raw.get(field + "_raw") or "")
    raw["platform"] = str(raw.get("platform") or "")
    raw = guard_case_row({**raw, "project_id": project_id})
    if force_recompile_resource_key:
        raw.pop("resource_key", None)
        raw["meta"].pop("resource_key", None)
    cid = str(raw.get("case_id") or "").strip()
    extra = dict(raw)
    return dict(
        case_id=cid,
        project_id=project_id,
        requirement_id=requirement_id or str(raw.get("requirement_id") or ""),
        name=str(raw.get("name") or raw.get("title") or cid),
        module=str(raw.get("module") or ""),
        platform=str(raw.get("platform") or ""),
        aspect=str(raw.get("aspect") or "正向"),
        precondition=str(raw.get("precondition") or raw.get("pre") or ""),
        steps=list(raw.get("steps") or []),
        expected=list(raw.get("expected") or []),
        steps_raw=str(raw.get("steps_raw") or ""),
        expected_raw=str(raw.get("expected_raw") or ""),
        point_ids=[str(x) for x in (raw.get("point_ids") or []) if str(x).strip()],
        source=str(raw.get("source") or "generated"),
        extra=extra,
        sort_index=int(raw.get("index") if raw.get("index") is not None else sort_index),
        updated_at=_now(),
    )


def _retain_publication(obj, fields):
    """Re-read server-owned state in the writing transaction."""
    from copy import deepcopy
    old = dict(obj.extra or {})
    new = dict(fields["extra"])
    from mino_nexus.services.v5_publication import source_view
    from mino_nexus.services.case_review import sync as sync_review
    # Rebase server-owned revision/history on the row held by this transaction.
    new["v5_draft_revision"] = int(old.get("v5_draft_revision") or 1) + int(source_view(_public(obj)) != source_view(new))
    new = sync_review(new, old)
    if old.get("v5_publication"):
        new["v5_publication"] = deepcopy(old["v5_publication"])
        changed_resource = new.get("resource_key") != old["v5_publication"].get("resource_key")
        new["v5_plan_state"] = "needs_revalidate" if changed_resource else old.get("v5_plan_state", "published")
        meta = dict(new.get("meta") or {})
        meta["step_program_keys"] = deepcopy(old["v5_publication"]["compiled_keys"])
        new["meta"] = meta
    fields["extra"] = new



def _persist_fields(db, fields, obj=None, *, origin=None):
    from mino_nexus.models.case import ProjectCase
    from mino_nexus.services.case_compilation import record_change
    previous = _public(obj) if obj is not None else None
    binding = (obj.extra or {}).get("source_binding") if obj is not None else None
    fields["extra"].pop("source_binding", None)
    if binding:
        from mino_nexus.models.case_source import CaseSource
        source = db.get(CaseSource, binding["source_id"])
        mapped = set((source.configuration if source else {}).get("columns") or {}) - {"external_case_no"}
        if source is None or any(getattr(obj, key) != fields[key] for key in mapped):
            raise ValueError("source_bound_fields_readonly")
        fields["extra"]["source_binding"] = dict(binding)
    if previous is not None:
        from copy import deepcopy
        previous["meta"] = deepcopy((obj.extra or {}).get("meta") or {})
    if obj is None:
        obj = ProjectCase(**fields)
        db.add(obj)
    else:
        _retain_publication(obj, fields)
        for key, value in fields.items():
            setattr(obj, key, value)
    db.flush()
    record_change(db, obj, previous, origin=origin)
    db.flush()
    return obj

def save_case(project_id: str, row: dict[str, Any], *, requirement_id: str = "") -> dict[str, Any]:
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.case import ProjectCase

    pid = str(project_id or "").strip()
    if not pid:
        raise ValueError("缺少 project_id")
    cid = str(row.get("case_id") or "").strip()
    if not cid:
        cid = allocate_case_id(pid)
        row = {**row, "case_id": cid}
    rid = str(requirement_id or row.get("requirement_id") or "").strip()
    existing = get_case(pid, cid)
    pre_new = str(row.get("precondition") or row.get("pre") or "").strip()
    pre_old = str((existing or {}).get("precondition") or "").strip()
    force_rk = bool(existing and pre_new != pre_old and "resource_key" not in row)
    fields = _row_fields(
        row,
        project_id=pid,
        requirement_id=rid,
        sort_index=int(row.get("index") or 0),
        force_recompile_resource_key=force_rk,
    )
    from sqlalchemy import text
    with session_scope() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        obj = db.query(ProjectCase).filter_by(project_id=pid, case_id=cid).one_or_none()
        # Preparation happened before the lock; reject concurrent source saves.
        if (existing is None) != (obj is None) or (obj is not None and obj.updated_at != existing.get("updated_at")):
            raise ValueError("case_revision_conflict")
        _persist_fields(db, fields, obj)
    saved = get_case(pid, cid)
    return saved or {**row, "case_id": cid, "project_id": pid, "requirement_id": rid}


def upsert_cases(
    project_id: str,
    requirement_id: str,
    rows: list[dict[str, Any]],
    *,
    replace: bool = False,
) -> list[dict[str, Any]]:
    """LLM / 配置回写。replace=True 时整需求替换。"""
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.case import ProjectCase

    pid = str(project_id or "").strip()
    rid = str(requirement_id or "").strip()
    if not pid:
        raise ValueError("缺少 project_id")
    incoming = [x for x in rows if isinstance(x, dict) and str(x.get("case_id") or "").strip()]
    if replace and rid and not incoming:
        return list_cases(pid, rid)
    from sqlalchemy import text
    expected_rows = {raw["case_id"]: get_case(pid, raw["case_id"]) for raw in incoming}
    prepared = [_row_fields(raw, project_id=pid, requirement_id=rid, sort_index=i) for i, raw in enumerate(incoming)]
    with session_scope() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        for fields in prepared:
            current = db.query(ProjectCase).filter_by(project_id=pid, case_id=fields["case_id"]).one_or_none()
            expected = expected_rows[fields["case_id"]]
            if (current is None) != (expected is None) or current is not None and current.updated_at != expected["updated_at"]:
                raise ValueError("case_revision_conflict")
        incoming_ids = {fields["case_id"] for fields in prepared}
        if replace and rid:
            for old in db.query(ProjectCase).filter_by(project_id=pid, requirement_id=rid).all():
                if old.case_id not in incoming_ids and not (old.extra or {}).get("v5_publication") and not (old.extra or {}).get("source_binding"):
                    db.delete(old)
        for fields in prepared:
            obj = db.query(ProjectCase).filter_by(project_id=pid, case_id=fields["case_id"]).one_or_none()
            _persist_fields(db, fields, obj, origin={"kind": "upsert"})
    return list_cases(pid, rid)


def apply_import_row(
    project_id: str,
    requirement_id: str,
    row: dict[str, Any],
    *,
    on_conflict: ConflictAction = "skip",
    compiled: bool = False,
) -> tuple[str, dict[str, Any] | None]:
    """Legacy callers share the same transactional revision/job path."""
    from sqlalchemy import text
    from mino_nexus.core.database import session_scope
    prepared = prepare_import_row(project_id, requirement_id, row, on_conflict=on_conflict, compiled=compiled)
    if prepared.get("status") == "skipped":
        return "skipped", get_case(project_id, prepared["case_id"])
    with session_scope() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        return apply_prepared_import(db, prepared)


def prepare_import_row(project_id, requirement_id, row, *, on_conflict="skip", compiled=False, expected_updated_at=None):
    payload = dict(row)
    case_id = str(payload.get("case_id") or "").strip()
    existing = get_case(project_id, case_id) if case_id else find_similar_case(project_id, requirement_id, payload)
    if existing and on_conflict == "skip":
        return {"status": "skipped", "case_id": existing["case_id"]}
    if existing and compiled and expected_updated_at is None:
        raise ValueError("import_target_changed")
    if existing and expected_updated_at is not None and existing.get("updated_at") != expected_updated_at:
        raise ValueError("import_target_changed")
    case_id = str(existing["case_id"]) if existing else case_id or allocate_case_id(project_id)
    payload.update(case_id=case_id, source=payload.get("source") or "import")
    fields = _row_fields(payload, project_id=project_id, requirement_id=requirement_id,
        sort_index=int(payload.get("index") or (existing or {}).get("index") or 0), compiled=compiled)
    return {"fields": fields, "expected_updated_at": existing.get("updated_at") if existing else None,
            "expected_exists": bool(existing)}


def apply_prepared_import(database, prepared):
    from copy import deepcopy
    from mino_nexus.models.case import ProjectCase

    fields = deepcopy(prepared["fields"])
    existing = database.query(ProjectCase).filter_by(project_id=fields["project_id"], case_id=fields["case_id"]).one_or_none()
    if bool(existing) != prepared["expected_exists"] or existing and existing.updated_at != prepared["expected_updated_at"]:
        raise ValueError("import_target_changed")
    status = "updated" if existing is not None else "created"
    existing = _persist_fields(database, fields, existing, origin=prepared.get("origin") or {"kind": "import"})
    return status, _public(existing)


def delete_case(project_id: str, case_id: str) -> bool:
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.case import ProjectCase

    pid = str(project_id or "").strip()
    cid = str(case_id or "").strip()
    if not pid or not cid:
        return False
    with session_scope() as db:
        obj = (
            db.query(ProjectCase)
            .filter(ProjectCase.project_id == pid, ProjectCase.case_id == cid)
            .first()
        )
        if obj is None:
            return False
        if (obj.extra or {}).get("source_binding"):
            raise ValueError("source_bound_fields_readonly")
        db.delete(obj)
        db.flush()
    return True


def delete_cases(project_id: str, case_ids: list[str]) -> int:
    removed = 0
    for cid in case_ids:
        if delete_case(project_id, str(cid or "").strip()):
            removed += 1
    return removed


def promote_from_env(app: dict[str, Any]) -> int:
    """把 env 里残留的 draft_cases 搬进 project_cases，只搬一次。"""
    pid = str(app.get("project_id") or "").strip()
    aid = str(app.get("id") or "").strip()
    if not pid or not aid:
        return 0
    env = app.get("env") if isinstance(app.get("env"), dict) else {}
    auto = env.get("automation") if isinstance(env.get("automation"), dict) else {}
    qp = auto.get("qa_process") if isinstance(auto.get("qa_process"), dict) else {}
    moved = 0
    for req in qp.get("requirements") or []:
        if not isinstance(req, dict):
            continue
        drafts = req.get("draft_cases") if isinstance(req.get("draft_cases"), list) else []
        rows = [x for x in drafts if isinstance(x, dict) and str(x.get("case_id") or "").strip()]
        if not rows:
            continue
        rid = str(req.get("id") or "")
        existing_ids = {str(c.get("case_id") or "") for c in list_cases(pid, rid)}
        to_add = [x for x in rows if str(x.get("case_id") or "") not in existing_ids]
        if not to_add:
            continue
        upsert_cases(pid, rid, to_add, replace=False)
        moved += len(to_add)
    return moved
