"""应用用例。从 apps.env.automation.qa_process.requirements[].draft_cases 拆出。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _public(row) -> dict[str, Any]:
    extra = dict(row.extra or {}) if isinstance(row.extra, dict) else {}
    out = {
        **extra,
        "case_id": row.case_id,
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


def count_cases(app_id: str, requirement_id: str = "") -> int:
    from mino_nexus.core.database import SessionLocal, ensure_db
    from mino_nexus.models.case import AppCase

    aid = str(app_id or "").strip()
    if not aid:
        return 0
    ensure_db()
    db = SessionLocal()
    try:
        q = db.query(AppCase).filter(AppCase.app_id == aid)
        rid = str(requirement_id or "").strip()
        if rid:
            q = q.filter(AppCase.requirement_id == rid)
        return q.count()
    finally:
        db.close()


def list_cases(app_id: str, requirement_id: str = "") -> list[dict[str, Any]]:
    from mino_nexus.core.database import SessionLocal, ensure_db
    from mino_nexus.models.case import AppCase

    aid = str(app_id or "").strip()
    if not aid:
        return []
    ensure_db()
    db = SessionLocal()
    try:
        q = db.query(AppCase).filter(AppCase.app_id == aid)
        rid = str(requirement_id or "").strip()
        if rid:
            q = q.filter(AppCase.requirement_id == rid)
        rows = q.order_by(AppCase.sort_index, AppCase.pk).all()
        return [_public(r) for r in rows]
    finally:
        db.close()


def upsert_cases(
    app_id: str,
    requirement_id: str,
    rows: list[dict[str, Any]],
    *,
    replace: bool = False,
) -> list[dict[str, Any]]:
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.case import AppCase

    aid = str(app_id or "").strip()
    rid = str(requirement_id or "").strip()
    if not aid:
        raise ValueError("缺少 app_id")
    incoming = [x for x in rows if isinstance(x, dict) and str(x.get("case_id") or "").strip()]
    with session_scope() as db:
        if replace and rid:
            for old in db.query(AppCase).filter(AppCase.app_id == aid, AppCase.requirement_id == rid).all():
                db.delete(old)
        keep: list[dict[str, Any]] = []
        for i, raw in enumerate(incoming):
            cid = str(raw.get("case_id") or "").strip()
            extra = dict(raw)
            obj = db.query(AppCase).filter(AppCase.app_id == aid, AppCase.case_id == cid).first()
            fields = dict(
                case_id=cid,
                app_id=aid,
                requirement_id=rid or str(raw.get("requirement_id") or ""),
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
                sort_index=int(raw.get("index") or i),
                updated_at=_now(),
            )
            if obj is None:
                obj = AppCase(**fields)
                db.add(obj)
            else:
                for key, val in fields.items():
                    setattr(obj, key, val)
            keep.append(extra)
        db.flush()
    return list_cases(aid, rid)


def promote_from_env(app: dict[str, Any]) -> int:
    """把 env 里残留的 draft_cases 搬进 app_cases，只搬一次。"""
    aid = str(app.get("id") or "").strip()
    if not aid:
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
        if count_cases(aid, rid):
            continue
        upsert_cases(aid, rid, rows, replace=False)
        moved += len(rows)
    return moved
