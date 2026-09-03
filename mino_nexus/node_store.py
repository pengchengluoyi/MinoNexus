"""Scout 节点归属落盘。活连接仍在 NodeRegistry；这里记住离线后还能列出的那一行。

未归属（无 owner_user_id 且无 studio_id）只给管理员看。
管理员可见全部节点。其他用户只见 owner_user_id=自己，或 studio_id=本工作台。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from mino_nexus.json_store import load_json, save_json

_FILE = "nodes.json"
_STUDIO_FILE = "studios.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def sanitize_id(raw: str) -> str:
    return "".join(c for c in str(raw or "").lower() if c.isalnum())


def _root() -> dict[str, Any]:
    raw = load_json(_FILE, {})
    if not isinstance(raw, dict):
        raw = {}
    nodes = raw.get("nodes")
    if not isinstance(nodes, dict):
        raw["nodes"] = {}
    return raw


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
    root = _root()
    prev = root["nodes"].get(nid) if isinstance(root["nodes"].get(nid), dict) else {}
    row = dict(prev)
    row["node_id"] = nid
    for key, value in fields.items():
        if key in ("studio_id", "owner_user_id"):
            cleaned = sanitize_id(str(value or ""))
            if cleaned or key not in row:
                row[key] = cleaned
            continue
        if value is None:
            continue
        row[key] = value
    if not row.get("first_seen"):
        row["first_seen"] = prev.get("first_seen") or _now_iso()
    row["updated_at"] = _now_iso()
    root["nodes"][nid] = row
    save_json(_FILE, root)
    sid = sanitize_id(str(row.get("studio_id") or ""))
    if sid:
        remember_studio(
            sid,
            owner_user_id=str(row.get("owner_user_id") or ""),
            last_scout_id=nid,
        )
    return row


def _studio_root() -> dict[str, Any]:
    raw = load_json(_STUDIO_FILE, {})
    if not isinstance(raw, dict):
        raw = {}
    studios = raw.get("studios")
    if not isinstance(studios, dict):
        raw["studios"] = {}
    return raw


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
    save_json(_STUDIO_FILE, root)
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
    from mino_nexus.auth_store import is_admin_role
    return is_admin_role(str((sess or {}).get("role") or ""))


def node_visible(row: dict[str, Any], sess: dict[str, Any] | None, studio_id: str = "") -> bool:
    """未归属仅管理员；管理员可见全部；其他人只见自己的账号或本工作台。"""
    sess = sess or {}
    owner = sanitize_id(str(row.get("owner_user_id") or ""))
    sid = sanitize_id(str(row.get("studio_id") or ""))
    uid = sanitize_id(str(sess.get("user_id") or ""))
    want_studio = sanitize_id(studio_id)
    if not owner and not sid:
        return is_admin(sess)
    if uid and owner == uid:
        return True
    if want_studio and sid == want_studio:
        return True
    return is_admin(sess)


def ownership_label(row: dict[str, Any], *, studio_id: str = "", user_id: str = "") -> str:
    owner = sanitize_id(str(row.get("owner_user_id") or ""))
    sid = sanitize_id(str(row.get("studio_id") or ""))
    uid = sanitize_id(user_id)
    want_studio = sanitize_id(studio_id)
    name = str(row.get("owner_name") or "").strip()
    if not owner and not sid:
        return "未归属"
    if uid and owner == uid:
        return name or "当前账号"
    if want_studio and sid == want_studio:
        return "本工作台"
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
        if not node_visible(row, sess, studio_id):
            continue
        item = dict(row)
        item["ownership"] = ownership_label(item, studio_id=studio_id, user_id=uid)
        out.append(item)
    return out
