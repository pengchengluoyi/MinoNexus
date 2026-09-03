"""HTTP 依赖。"""
from __future__ import annotations

from typing import Any

from fastapi import Header, HTTPException

from mino_nexus import auth_store
from mino_nexus.http_util import bearer


def current_session(authorization: str = Header(default="")) -> dict[str, Any]:
    try:
        return auth_store.require_session(bearer(authorization))
    except PermissionError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
