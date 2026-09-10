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
    module = str(row.get("module") or "").strip()
    rid = str(requirement_id or "").strip()
    for ex in list_cases(project_id, rid):
        if str(ex.get("name") or "").strip() != name:
            continue
        ex_mod = str(ex.get("module") or "").strip()
        if module and ex_mod and ex_mod != module:
            continue
        return ex
    return None


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
) -> dict[str, Any]:
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
    fields = _row_fields(row, project_id=pid, requirement_id=rid, sort_index=int(row.get("index") or 0))
    with session_scope() as db:
        obj = (
            db.query(ProjectCase)
            .filter(ProjectCase.project_id == pid, ProjectCase.case_id == cid)
            .first()
        )
        if obj is None:
            obj = ProjectCase(**fields)
            db.add(obj)
        else:
            for key, val in fields.items():
                setattr(obj, key, val)
        db.flush()
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
    with session_scope() as db:
        if replace and rid:
            for old in db.query(ProjectCase).filter(
                ProjectCase.project_id == pid,
                ProjectCase.requirement_id == rid,
            ).all():
                db.delete(old)
        for i, raw in enumerate(incoming):
            cid = str(raw.get("case_id") or "").strip()
            fields = _row_fields(raw, project_id=pid, requirement_id=rid, sort_index=i)
            obj = (
                db.query(ProjectCase)
                .filter(ProjectCase.project_id == pid, ProjectCase.case_id == cid)
                .first()
            )
            if obj is None:
                obj = ProjectCase(**fields)
                db.add(obj)
            else:
                for key, val in fields.items():
                    setattr(obj, key, val)
        db.flush()
    return list_cases(pid, rid)


def apply_import_row(
    project_id: str,
    requirement_id: str,
    row: dict[str, Any],
    *,
    on_conflict: ConflictAction = "skip",
) -> tuple[str, dict[str, Any] | None]:
    """导入单行。返回 (created|updated|skipped, case_dict)。"""
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.case import ProjectCase

    pid = str(project_id or "").strip()
    rid = str(requirement_id or "").strip()
    if not pid or not rid:
        raise ValueError("缺少 project_id 或 requirement_id")
    payload = dict(row)
    cid = str(payload.get("case_id") or "").strip()
    if not cid:
        similar = find_similar_case(pid, rid, payload)
        if similar:
            if on_conflict == "skip":
                return "skipped", similar
            cid = str(similar.get("case_id") or "").strip()
            payload["case_id"] = cid
    if not cid:
        cid = allocate_case_id(pid)
        payload["case_id"] = cid
    existing = get_case(pid, cid)
    if existing:
        if on_conflict == "skip":
            return "skipped", existing
        fields = _row_fields(
            {**payload, "source": payload.get("source") or "import"},
            project_id=pid,
            requirement_id=rid,
            sort_index=int(payload.get("index") or existing.get("index") or 0),
        )
        with session_scope() as db:
            obj = (
                db.query(ProjectCase)
                .filter(ProjectCase.project_id == pid, ProjectCase.case_id == cid)
                .first()
            )
            if obj is None:
                return "skipped", existing
            for key, val in fields.items():
                setattr(obj, key, val)
            db.flush()
        return "updated", get_case(pid, cid)
    fields = _row_fields(
        {**payload, "source": payload.get("source") or "import"},
        project_id=pid,
        requirement_id=rid,
        sort_index=int(payload.get("index") or 0),
    )
    with session_scope() as db:
        db.add(ProjectCase(**fields))
        db.flush()
    return "created", get_case(pid, cid)


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
