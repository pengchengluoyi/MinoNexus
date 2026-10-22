"""轻量任务记录。对齐上游 `rTask` 列表/创建，执行循环另迁。"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _public(row) -> dict[str, Any]:
    return {
        "id": row.id,
        "app_id": row.app_id or "",
        "name": row.name or "",
        "type": row.task_type or "",
        "status": row.status or "pending",
        "created_at": row.created_at or "",
    }


def create_task(app_id: str, name: str, type_: str) -> dict[str, Any]:
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.task import Task

    row = Task(
        id=str(uuid.uuid4()),
        app_id=app_id,
        name=name,
        task_type=type_,
        status="pending",
        created_at=_now(),
    )
    with session_scope() as db:
        db.add(row)
        db.flush()
        return _public(row)


def list_tasks(app_id: str = "", type_: str = "", keyword: str = "") -> list[dict[str, Any]]:
    from mino_nexus.core.database import SessionLocal, ensure_db
    from mino_nexus.models.task import Task

    ensure_db()
    db = SessionLocal()
    try:
        q = db.query(Task)
        if app_id:
            q = q.filter(Task.app_id == app_id)
        if type_ and type_ != "all":
            q = q.filter(Task.task_type == type_)
        rows = [_public(t) for t in q.all()]
    finally:
        db.close()
    if keyword:
        kw = keyword.lower()
        rows = [t for t in rows if kw in str(t.get("name") or "").lower()]
    rows.sort(key=lambda t: str(t.get("created_at") or ""), reverse=True)
    return rows
