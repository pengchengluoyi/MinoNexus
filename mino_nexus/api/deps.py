"""HTTP 依赖。"""
from __future__ import annotations

from typing import Any

from fastapi import Depends, Header, HTTPException

from mino_nexus.platform import auth_store
from mino_nexus.core.http_util import bearer


def current_session(authorization: str = Header(default="")) -> dict[str, Any]:
    from mino_nexus.platform.session_auth import resolve_session

    try:
        return resolve_session(authorization)
    except PermissionError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


def require_project_write(project_id: str, sess: dict[str, Any]) -> dict[str, Any]:
    from mino_nexus.resources.project_members import require_project_access
    try:
        require_project_access(project_id, sess)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Project not found") from exc
    except PermissionError as exc:
        raise HTTPException(status_code=401, detail={"reason_code": "login_required",
                                                     "message": "请先登录"}) from exc
    return sess


def project_maintainer(project_id: str, sess: dict[str, Any] = Depends(current_session)) -> dict[str, Any]:
    from mino_nexus.resources import project_store as ps

    pid = str(project_id or "").strip()
    if not ps.find_project(pid):
        app = ps.find_app(pid)
        pid = str((app or {}).get("project_id") or pid)
    return require_project_write(pid, sess)


def app_maintainer(app_id: str, sess: dict[str, Any] = Depends(current_session)) -> dict[str, Any]:
    from mino_nexus.resources import project_store as ps
    app = ps.find_app(app_id)
    project_id = str((app or {}).get("project_id") or "") or (app_id if ps.find_project(app_id) else "")
    if not project_id:
        return sess
    return require_project_write(project_id, sess)


def require_packs_writer(sess: dict[str, Any]) -> dict[str, Any]:
    if not auth_store.is_admin_role(str(sess.get("role") or "")):
        raise HTTPException(status_code=403, detail="需要管理员才能改能力目录")
    return sess
