"""会话解析（无框架）：从 Authorization 头得到已认证会话。

`routers/deps.current_session` 只做 HTTP 翻译（PermissionError -> 401），
这样 `api` 层不必反向 import `routers`。
"""
from __future__ import annotations

from typing import Any

from mino_nexus.core.http_util import bearer


def resolve_session(authorization: str) -> dict[str, Any]:
    raw = bearer(authorization)
    from mino_nexus.contract.tokens import resolve_token_session
    from mino_nexus.platform import auth_store

    token_session = resolve_token_session(raw)
    if token_session is not None:
        return token_session
    return auth_store.require_session(raw)
