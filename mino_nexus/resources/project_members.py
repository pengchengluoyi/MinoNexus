"""项目协作：所有登录用户可访问和使用所有项目。"""
from __future__ import annotations

from typing import Any

def _uid(sess: dict[str, Any] | None) -> str:
    return str((sess or {}).get("user_id") or "").strip()


def _project(project_id: str) -> dict[str, Any]:
    from mino_nexus.resources import project_store as ps
    row = ps.find_project(project_id)
    if not row:
        raise KeyError("project_not_found")
    return row


def require_project_access(project_id: str, sess: dict[str, Any] | None) -> None:
    """Project collaboration is shared across authenticated users."""
    if not _uid(sess):
        raise PermissionError("login_required")
    _project(project_id)


def require_maintainer(project_id: str, sess: dict[str, Any] | None) -> None:
    """登录即可使用项目；名册已撤除。"""
    require_project_access(project_id, sess)
