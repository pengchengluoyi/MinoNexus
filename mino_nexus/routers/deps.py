"""HTTP 依赖。"""
from __future__ import annotations

from typing import Any

from fastapi import Header, HTTPException

from mino_nexus.services import auth_store
from mino_nexus.core.http_util import bearer


def current_session(authorization: str = Header(default="")) -> dict[str, Any]:
    raw = bearer(authorization)
    from mino_nexus.services import assistant_store

    if assistant_store.is_api_token(raw):
        # 助手 token / MCP token：权限等同其主人，可随时在 Workbench 吊销
        uid = assistant_store.resolve_api_token(raw)
        sess = auth_store.api_session_for(uid) if uid else None
        if sess is None:
            raise HTTPException(status_code=401, detail="token 无效或已吊销")
        return sess
    try:
        return auth_store.require_session(bearer(authorization))
    except PermissionError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


def require_packs_writer(sess: dict[str, Any]) -> dict[str, Any]:
    if not auth_store.is_admin_role(str(sess.get("role") or "")):
        raise HTTPException(status_code=403, detail="需要管理员才能改能力目录")
    return sess
