"""项目协作：所有登录用户可访问和使用所有项目；维护者名册保留为管理信息。"""
from __future__ import annotations

import time
from typing import Any

from mino_nexus.core.database import session_scope
from mino_nexus.models.project_member import ProjectMember


def _uid(sess: dict[str, Any] | None) -> str:
    return str((sess or {}).get("user_id") or "").strip()


def _is_admin(sess: dict[str, Any] | None) -> bool:
    from mino_nexus.platform.auth_store import is_admin_role
    return is_admin_role(str((sess or {}).get("role") or ""))


def _project(project_id: str) -> dict[str, Any]:
    from mino_nexus.resources import project_store as ps
    row = ps.find_project(project_id)
    if not row:
        raise KeyError("project_not_found")
    return row


def is_maintainer(project_id: str, sess: dict[str, Any] | None) -> bool:
    uid = _uid(sess)
    if not uid:
        return False
    if _is_admin(sess):
        return True
    if str(_project(project_id).get("created_by") or "") == uid:
        return True
    with session_scope() as db:
        return db.get(ProjectMember, (project_id, uid)) is not None


def require_project_access(project_id: str, sess: dict[str, Any] | None) -> None:
    """Project collaboration is shared across authenticated users."""
    if not _uid(sess):
        raise PermissionError("login_required")
    _project(project_id)


def require_maintainer(project_id: str, sess: dict[str, Any] | None) -> None:
    """Compatibility for existing callers; membership no longer gates project use."""
    require_project_access(project_id, sess)


def list_maintainers(project_id: str) -> list[dict[str, Any]]:
    from mino_nexus.platform.auth_store import public_user_by_id
    creator = str(_project(project_id).get("created_by") or "")
    with session_scope() as db:
        rows = [(r.user_id, r.added_by, r.added_at) for r in db.query(ProjectMember).filter_by(project_id=project_id)]
    out = []
    for uid, added_by, added_at in ([(creator, "", 0)] if creator else []) + [r for r in rows if r[0] != creator]:
        pub = public_user_by_id(uid) or {}
        out.append({"user_id": uid, "name": str(pub.get("name") or pub.get("username") or ""),
                    "email": str(pub.get("email") or ""), "source": "creator" if uid == creator else "member",
                    "added_by": added_by, "added_at": added_at})
    return out


def add_maintainer(project_id: str, user_id: str, sess: dict[str, Any] | None) -> list[dict[str, Any]]:
    from mino_nexus.platform.auth_store import public_user_by_id
    if not _is_admin(sess):
        raise PermissionError("admin_required")
    _project(project_id)
    uid = str(user_id or "").strip()
    if not uid or not public_user_by_id(uid):
        raise ValueError("user_not_found")
    with session_scope() as db:
        if db.get(ProjectMember, (project_id, uid)) is None:
            db.add(ProjectMember(project_id=project_id, user_id=uid, role="maintainer",
                                 added_by=_uid(sess), added_at=int(time.time())))
    return list_maintainers(project_id)


def remove_maintainer(project_id: str, user_id: str, sess: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not _is_admin(sess):
        raise PermissionError("admin_required")
    if str(_project(project_id).get("created_by") or "") == str(user_id or ""):
        raise ValueError("creator_is_implicit_maintainer")
    with session_scope() as db:
        db.query(ProjectMember).filter_by(project_id=project_id, user_id=str(user_id or "")).delete()
    return list_maintainers(project_id)
