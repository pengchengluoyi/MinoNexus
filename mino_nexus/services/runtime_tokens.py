"""Studio 安装 Scout 时领取的短 TTL 节点凭证。"""
from __future__ import annotations

import hmac
import secrets
import time
from typing import Any

_TTL_SEC = 15 * 60


def _now() -> int:
    return int(time.time())


def _purge(db) -> None:
    from mino_nexus.models.token import InstallToken

    now = _now()
    for row in db.query(InstallToken).all():
        if int(row.expires_at or 0) <= now:
            db.delete(row)


def issue(*, user_id: str = "") -> dict[str, Any]:
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.token import InstallToken

    token = secrets.token_urlsafe(24)
    expires_at = _now() + _TTL_SEC
    created_at = _now()
    with session_scope() as db:
        _purge(db)
        db.add(InstallToken(
            token=token,
            user_id=str(user_id or ""),
            expires_at=expires_at,
            created_at=created_at,
        ))
    return {"token": token, "expires_at": expires_at, "ttl_sec": _TTL_SEC}


def peek_token(token: str) -> dict[str, Any] | None:
    from mino_nexus.core.database import SessionLocal, ensure_db
    from mino_nexus.models.token import InstallToken

    tok = str(token or "").strip()
    if not tok:
        return None
    ensure_db()
    db = SessionLocal()
    try:
        _purge(db)
        db.commit()
        row = db.get(InstallToken, tok)
        if row is None:
            return None
        stored = str(row.token or "")
        if not hmac.compare_digest(stored, tok):
            return None
        return {
            "token": stored,
            "user_id": str(row.user_id or ""),
            "expires_at": int(row.expires_at or 0),
            "created_at": int(row.created_at or 0),
        }
    finally:
        db.close()


def install_token_ok(token: str) -> bool:
    return peek_token(token) is not None
