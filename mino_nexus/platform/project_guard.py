"""项目写权限判定：项目不存在或无维护权一律拒绝。"""
from __future__ import annotations

from mino_nexus.platform.identity import require_user_id


def _maintainer(db, project, actor) -> bool:
    from mino_nexus.models.auth import User
    from mino_nexus.models.project_member import ProjectMember
    from mino_nexus.platform.auth_store import is_admin_role
    user = db.get(User, actor)
    # Same rule as project_members, evaluated in the held transaction. Its
    # legacy project-store reader may lazily write names in another session.
    return bool(project and user and (is_admin_role(user.role) or project.created_by == actor
        or db.get(ProjectMember, (project.id, actor)) is not None))


def owned(db, project_id, *, write=True):
    from mino_nexus.models.project import Project
    actor = require_user_id()
    project = db.get(Project, project_id)
    if project is None or write and not _maintainer(db, project, actor):
        raise ValueError("project_missing")
    return actor
