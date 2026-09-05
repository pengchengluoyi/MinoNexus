"""HTTP 依赖。"""
from __future__ import annotations

from typing import Any

from fastapi import Header, HTTPException

from mino_nexus.services import auth_store
from mino_nexus.core.http_util import bearer


def current_session(authorization: str = Header(default="")) -> dict[str, Any]:
    try:
        return auth_store.require_session(bearer(authorization))
    except PermissionError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


def require_packs_writer(sess: dict[str, Any]) -> dict[str, Any]:
    if not auth_store.is_admin_role(str(sess.get("role") or "")):
        raise HTTPException(status_code=403, detail="需要管理员才能改能力目录")
    return sess
