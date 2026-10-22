"""短期安装凭证、原子绑定及单节点长期凭证。"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from typing import Any

_TTL_SEC = 15 * 60
_STATUS_RETENTION_SEC = 7 * 24 * 60 * 60


def _now() -> int:
    return int(time.time())


def _purge_install(db) -> None:
    from mino_nexus.models.token import InstallToken

    db.query(InstallToken).filter(InstallToken.expires_at < _now() - _STATUS_RETENTION_SEC).delete()


def issue(*, user_id: str = "") -> dict[str, Any]:
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.token import InstallToken

    if not user_id:
        raise ValueError("安装凭证必须关联登录账号")
    token = secrets.token_urlsafe(24)
    install_id = secrets.token_hex(16)
    created_at = _now()
    expires_at = created_at + _TTL_SEC
    with session_scope() as db:
        _purge_install(db)
        db.add(InstallToken(token=token, install_id=install_id, user_id=user_id,
                            expires_at=expires_at, created_at=created_at))
    return {"token": token, "install_id": install_id, "expires_at": expires_at, "ttl_sec": _TTL_SEC}


def installation_status(install_id: str, *, user_id: str) -> dict[str, Any] | None:
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.token import InstallToken, NodeCredential
    from mino_nexus.models.node import Node
    from mino_nexus.resources.node_store import sanitize_id

    with session_scope() as db:
        row = db.query(InstallToken).filter_by(install_id=install_id, user_id=user_id).first()
        if row is None:
            return None
        if row.node_id:
            credential = db.get(NodeCredential, row.node_id)
            node = db.get(Node, row.node_id)
            if (credential is None or credential.revoked_at or node is None
                    or node.owner_user_id != sanitize_id(user_id)
                    or row.credential_digest != hashlib.sha256(credential.token.encode()).hexdigest()):
                return {"install_id": row.install_id, "node_id": "", "expires_at": row.expires_at,
                        "registered_at": row.registered_at or 0, "status": "revoked"}
        return {"install_id": row.install_id, "node_id": row.node_id or "",
                "expires_at": row.expires_at, "registered_at": row.registered_at or 0,
                "status": "registered" if row.node_id else "expired" if row.expires_at <= _now() else "pending"}


def peek_token(token: str) -> dict[str, Any] | None:
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.token import InstallToken

    with session_scope() as db:
        row = db.get(InstallToken, str(token or "").strip())
        if row is None or row.expires_at <= _now():
            return None
        return {"kind": "install", "token": row.token, "user_id": row.user_id,
                "expires_at": row.expires_at, "created_at": row.created_at}


def peek_node_token(token: str) -> dict[str, Any] | None:
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.token import NodeCredential

    with session_scope() as db:
        row = db.query(NodeCredential).filter_by(token=str(token or "").strip(), revoked_at=0).first()
        if row is None:
            return None
        return {"kind": "node", "token": row.token, "node_id": row.node_id,
                "user_id": row.user_id, "created_at": row.created_at}


def authenticate_registration(token: str, requested: str) -> dict[str, Any]:
    from sqlalchemy import text
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.node import Node
    from mino_nexus.models.token import InstallToken, NodeCredential
    from mino_nexus.resources.node_register import resolve_register_node_id
    from mino_nexus.resources.node_store import sanitize_id

    tok = str(token or "").strip()
    requested_id = sanitize_id(requested)
    with session_scope() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        install = db.get(InstallToken, tok)
        if install is None:
            credential = db.query(NodeCredential).filter_by(token=tok, revoked_at=0).first()
            if credential is None:
                raise PermissionError("节点凭证无效或已吊销，请重新生成安装命令")
            if requested_id != credential.node_id:
                raise PermissionError("节点凭证与本机 node_id 不匹配")
            node = db.get(Node, credential.node_id)
            if node is None or node.owner_user_id != sanitize_id(credential.user_id):
                raise PermissionError("节点归属已变更，请由管理员处理后重新安装")
            return {"node_id": credential.node_id, "user_id": credential.user_id,
                    "node_token": "", "warnings": []}

        if install.expires_at <= _now() or not install.user_id:
            raise PermissionError("安装凭证已过期，请重新生成安装命令")
        if len(requested_id) < 8:
            raise PermissionError("本机 node_id 无效，请检查 Scout 身份配置")
        if install.node_id:
            if install.requested_node_id != requested_id:
                raise PermissionError("安装凭证已被另一台电脑使用，请为每台电脑生成新命令")
            credential = db.get(NodeCredential, install.node_id)
            node = db.get(Node, install.node_id)
            if (credential is None or credential.revoked_at or node is None
                    or node.owner_user_id != sanitize_id(install.user_id)
                    or not hmac.compare_digest(install.credential_digest or "", hashlib.sha256(credential.token.encode()).hexdigest())):
                raise PermissionError("本次安装的节点凭证已失效，请重新生成安装命令")
            return {"node_id": install.node_id, "user_id": install.user_id,
                    "node_token": credential.token, "warnings": []}

        existing = db.get(Node, requested_id)
        stored = {"node_id": existing.node_id, "owner_user_id": existing.owner_user_id} if existing else {}
        assigned, warnings = resolve_register_node_id(requested_id, is_install_token=True,
                                                       user_id=install.user_id, stored=stored)
        credential = db.get(NodeCredential, assigned)
        if credential is None:
            credential = NodeCredential(node_id=assigned, token=secrets.token_urlsafe(32),
                                        user_id=install.user_id, created_at=_now(), revoked_at=0)
            db.add(credential)
        elif credential.revoked_at:
            credential.token = secrets.token_urlsafe(32)
            credential.created_at = _now()
            credential.revoked_at = 0
            credential.user_id = install.user_id
        elif sanitize_id(credential.user_id) != sanitize_id(install.user_id):
            raise PermissionError("节点凭证属于其他账号，请先由管理员吊销")
        if existing is None:
            db.add(Node(node_id=assigned, owner_user_id=sanitize_id(install.user_id)))
        install.requested_node_id = requested_id
        install.node_id = assigned
        install.registered_at = _now()
        install.credential_digest = hashlib.sha256(credential.token.encode()).hexdigest()
        return {"node_id": assigned, "user_id": install.user_id,
                "node_token": credential.token, "warnings": warnings}


def revoke_node_credential(node_id: str) -> None:
    from sqlalchemy import text
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.token import NodeCredential

    with session_scope() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        row = db.get(NodeCredential, node_id)
        if row is not None:
            row.revoked_at = _now()


def install_token_ok(token: str) -> bool:
    return peek_token(token) is not None


def node_token_ok(token: str) -> bool:
    return peek_node_token(token) is not None
