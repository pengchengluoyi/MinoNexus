"""Scout 节点归属落盘。活连接仍在 NodeRegistry；这里记住离线后还能列出的那一行。

无账号归属的历史节点只给管理员看。
管理员可见全部节点。其他用户只见 owner_user_id=自己。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def sanitize_id(raw: str) -> str:
    return "".join(c for c in str(raw or "").lower() if c.isalnum())


def _root() -> dict[str, Any]:
    from mino_nexus.core.database import SessionLocal, ensure_db
    from mino_nexus.models.node import Node

    ensure_db()
    db = SessionLocal()
    try:
        nodes: dict[str, dict[str, Any]] = {}
        for row in db.query(Node).all():
            extra = dict(row.extra or {}) if isinstance(row.extra, dict) else {}
            extra["node_id"] = row.node_id
            extra["studio_id"] = row.studio_id or extra.get("studio_id") or ""
            extra["owner_user_id"] = row.owner_user_id or extra.get("owner_user_id") or ""
            extra["first_seen"] = row.first_seen or extra.get("first_seen") or ""
            extra["updated_at"] = row.updated_at or extra.get("updated_at") or ""
            nodes[row.node_id] = extra
        return {"nodes": nodes}
    finally:
        db.close()


def _node_row(nid: str, row: dict) -> Any:
    from mino_nexus.models.node import Node

    return Node(
        node_id=nid,
        studio_id=str(row.get("studio_id") or ""),
        owner_user_id=str(row.get("owner_user_id") or ""),
        extra=dict(row),
        first_seen=str(row.get("first_seen") or ""),
        updated_at=str(row.get("updated_at") or ""),
    )


def _save(root: dict[str, Any]) -> None:
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.node import Node

    with session_scope() as db:
        keep: set[str] = set()
        for nid, row in (root.get("nodes") or {}).items():
            if not isinstance(row, dict):
                continue
            key = sanitize_id(str(row.get("node_id") or nid))
            if not key:
                continue
            keep.add(key)
            db.merge(_node_row(key, row))
        for row in db.query(Node).all():
            if row.node_id not in keep:
                db.delete(row)


def list_nodes() -> dict[str, dict[str, Any]]:
    root = _root()
    out: dict[str, dict[str, Any]] = {}
    for key, row in (root.get("nodes") or {}).items():
        if isinstance(row, dict) and str(row.get("node_id") or key):
            nid = sanitize_id(str(row.get("node_id") or key))
            if nid:
                out[nid] = dict(row)
                out[nid]["node_id"] = nid
    return out


def get_node(node_id: str) -> dict[str, Any] | None:
    nid = sanitize_id(node_id)
    return list_nodes().get(nid)


def upsert(node_id: str, **fields: Any) -> dict[str, Any]:
    nid = sanitize_id(node_id)
    if not nid:
        return {}
    from sqlalchemy import text
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.node import Node

    with session_scope() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        existing = db.get(Node, nid)
        row = dict(existing.extra or {}) if existing is not None else {}
        if existing is not None:
            row.update(studio_id=existing.studio_id, owner_user_id=existing.owner_user_id,
                       first_seen=existing.first_seen)
        row["node_id"] = nid
        for key, value in fields.items():
            if value is not None:
                row[key] = sanitize_id(str(value)) if key in ("studio_id", "owner_user_id") else value
        row["first_seen"] = row.get("first_seen") or _now_iso()
        row["updated_at"] = _now_iso()
        db.merge(_node_row(nid, row))
    sid = sanitize_id(str(row.get("studio_id") or ""))
    if sid:
        remember_studio(
            sid,
            owner_user_id=str(row.get("owner_user_id") or ""),
            last_scout_id=nid,
        )
    return row


def _studio_root() -> dict[str, Any]:
    from mino_nexus.core.database import SessionLocal, ensure_db
    from mino_nexus.models.node import Studio

    ensure_db()
    db = SessionLocal()
    try:
        studios: dict[str, dict[str, Any]] = {}
        for row in db.query(Studio).all():
            extra = dict(row.extra or {}) if isinstance(row.extra, dict) else {}
            extra["studio_id"] = row.studio_id
            extra["owner_user_id"] = row.owner_user_id or extra.get("owner_user_id") or ""
            extra["updated_at"] = row.updated_at or extra.get("updated_at") or ""
            studios[row.studio_id] = extra
        return {"studios": studios}
    finally:
        db.close()


def _save_studios(root: dict[str, Any]) -> None:
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.node import Studio

    with session_scope() as db:
        keep: set[str] = set()
        for sid, row in (root.get("studios") or {}).items():
            if not isinstance(row, dict):
                continue
            key = sanitize_id(str(row.get("studio_id") or sid))
            if not key:
                continue
            keep.add(key)
            db.merge(Studio(
                studio_id=key,
                owner_user_id=str(row.get("owner_user_id") or ""),
                extra=dict(row),
                updated_at=str(row.get("updated_at") or ""),
            ))
        for row in db.query(Studio).all():
            if row.studio_id not in keep:
                db.delete(row)


def remember_studio(studio_id: str, *, owner_user_id: str = "", last_scout_id: str = "") -> dict[str, Any]:
    sid = sanitize_id(studio_id)
    if not sid:
        return {}
    root = _studio_root()
    prev = root["studios"].get(sid) if isinstance(root["studios"].get(sid), dict) else {}
    row = dict(prev)
    row["studio_id"] = sid
    owner = sanitize_id(owner_user_id)
    if owner and not sanitize_id(str(row.get("owner_user_id") or "")):
        row["owner_user_id"] = owner
    nid = sanitize_id(last_scout_id)
    if nid:
        row["last_scout_id"] = nid
    if not row.get("first_seen"):
        row["first_seen"] = prev.get("first_seen") or _now_iso()
    row["updated_at"] = _now_iso()
    root["studios"][sid] = row
    _save_studios(root)
    return row


def list_studios() -> dict[str, dict[str, Any]]:
    root = _studio_root()
    out: dict[str, dict[str, Any]] = {}
    for key, row in (root.get("studios") or {}).items():
        if not isinstance(row, dict):
            continue
        sid = sanitize_id(str(row.get("studio_id") or key))
        if sid:
            out[sid] = dict(row)
            out[sid]["studio_id"] = sid
    return out


def is_admin(sess: dict[str, Any] | None) -> bool:
    from mino_nexus.services.auth_store import is_admin_role
    return is_admin_role(str((sess or {}).get("role") or ""))


def node_visible(row: dict[str, Any], sess: dict[str, Any] | None, studio_id: str = "") -> bool:
    """账号是可见性依据；studio_id 仅保留为兼容元信息。"""
    sess = sess or {}
    owner = sanitize_id(str(row.get("owner_user_id") or ""))
    uid = sanitize_id(str(sess.get("user_id") or ""))
    return bool(uid and owner == uid)


def ownership_label(row: dict[str, Any], *, studio_id: str = "", user_id: str = "") -> str:
    owner = sanitize_id(str(row.get("owner_user_id") or ""))
    sid = sanitize_id(str(row.get("studio_id") or ""))
    uid = sanitize_id(user_id)
    name = str(row.get("owner_name") or "").strip()
    if not owner:
        return "未归属"
    if uid and owner == uid:
        return name or "当前账号"
    if name:
        return name
    if sid:
        return sid
    return owner or "未归属"


def filter_nodes(
    rows: list[dict[str, Any]],
    sess: dict[str, Any] | None,
    studio_id: str = "",
) -> list[dict[str, Any]]:
    uid = str((sess or {}).get("user_id") or "")
    out: list[dict[str, Any]] = []
    for row in rows:
        # List-only exception for administrator legacy claiming. Node commands,
        # logs, plugins and owned devices continue using node_visible/owner.
        unclaimed = not sanitize_id(str(row.get("owner_user_id") or ""))
        if not node_visible(row, sess, studio_id) and not (is_admin(sess) and unclaimed):
            continue
        item = dict(row)
        item["ownership"] = ownership_label(item, studio_id=studio_id, user_id=uid)
        out.append(item)
    return out
