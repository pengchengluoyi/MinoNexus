"""项目访问：登录用户可使用已有项目。"""
from __future__ import annotations

from mino_nexus.platform.identity import require_user_id


def owned(db, project_id, *, write=True):
    from mino_nexus.models.auth import User
    from mino_nexus.models.project import Project
    actor = require_user_id()
    project = db.get(Project, project_id)
    if project is None or write and db.get(User, actor) is None:
        raise ValueError("project_missing")
    return actor
