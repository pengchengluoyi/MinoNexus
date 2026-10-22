"""用例计数：项目侧读，直接查表，不经用例域。"""
from __future__ import annotations


def count_cases(project_id: str, requirement_id: str = "") -> int:
    from mino_nexus.core.database import SessionLocal, ensure_db
    from mino_nexus.models.case import ProjectCase

    pid = str(project_id or "").strip()
    if not pid:
        return 0
    ensure_db()
    with SessionLocal() as db:
        query = db.query(ProjectCase).filter(ProjectCase.project_id == pid)
        rid = str(requirement_id or "").strip()
        if rid:
            query = query.filter(ProjectCase.requirement_id == rid)
        return int(query.count())
