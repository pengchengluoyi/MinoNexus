"""Studio 安装 Scout 的短 TTL 凭证 + 节点长期 node_token。"""
from __future__ import annotations

import hmac
import secrets
import time
from typing import Any

_TTL_SEC = 15 * 60


def _now() -> int:
    return int(time.time())


def _purge_install(db) -> None:
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
        _purge_install(db)
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
        _purge_install(db)
        db.commit()
        row = db.get(InstallToken, tok)
        if row is None:
            return None
        stored = str(row.token or "")
        if not hmac.compare_digest(stored, tok):
            return None
        return {
            "kind": "install",
            "token": stored,
            "user_id": str(row.user_id or ""),
            "expires_at": int(row.expires_at or 0),
            "created_at": int(row.created_at or 0),
        }
    finally:
        db.close()


def consume_install_token(token: str) -> None:
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.token import InstallToken

    tok = str(token or "").strip()
    if not tok:
        return
    with session_scope() as db:
        row = db.get(InstallToken, tok)
        if row is not None:
            db.delete(row)


def peek_node_token(token: str) -> dict[str, Any] | None:
    from mino_nexus.core.database import SessionLocal, ensure_db
    from mino_nexus.models.token import NodeCredential

    tok = str(token or "").strip()
    if not tok:
        return None
    ensure_db()
    db = SessionLocal()
    try:
        row = db.query(NodeCredential).filter(NodeCredential.token == tok).first()
        if row is None:
            return None
        stored = str(row.token or "")
        if not hmac.compare_digest(stored, tok):
            return None
        return {
            "kind": "node",
            "token": stored,
            "node_id": str(row.node_id or ""),
            "user_id": str(row.user_id or ""),
            "created_at": int(row.created_at or 0),
        }
    finally:
        db.close()


def issue_node_credential(*, node_id: str, user_id: str = "") -> str:
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.token import NodeCredential
    from mino_nexus.services.node_store import sanitize_id

    nid = sanitize_id(node_id)
    if not nid:
        raise ValueError("node_id required")
    token = secrets.token_urlsafe(32)
    created = _now()
    with session_scope() as db:
        row = db.get(NodeCredential, nid)
        if row is None:
            db.add(NodeCredential(
                node_id=nid,
                token=token,
                user_id=str(user_id or ""),
                created_at=created,
            ))
        else:
            row.token = token
            row.user_id = str(user_id or row.user_id or "")
            row.created_at = created
    return token


def install_token_ok(token: str) -> bool:
    return peek_token(token) is not None


def node_token_ok(token: str) -> bool:
    return peek_node_token(token) is not None
