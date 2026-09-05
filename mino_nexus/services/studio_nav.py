"""Studio 侧栏 / 设置入口允许名单。Console 决定，Nexus 落盘，Studio 只读。"""
from __future__ import annotations

from typing import Any


ENTRIES: list[dict[str, str]] = [
    {"id": "testing", "label": "测试工作台", "group": "work"},
    {"id": "agent", "label": "Agent", "group": "work"},
    {"id": "knowledge", "label": "知识工作台", "group": "work"},
    {"id": "runtime", "label": "运行与设备", "group": "settings"},
    {"id": "scout", "label": "Scout 节点", "group": "settings"},
    {"id": "keys", "label": "模型密钥", "group": "settings"},
    {"id": "dispatch", "label": "调用记录", "group": "settings"},
    {"id": "plugins", "label": "插件配置", "group": "settings"},
]

DEFAULT_ALLOWED = ("testing", "agent", "knowledge", "runtime", "scout", "keys", "dispatch")
_NAV_VERSION = 2


def catalog() -> list[dict[str, str]]:
    return [dict(row) for row in ENTRIES]


def _known_ids() -> set[str]:
    return {str(row["id"]) for row in ENTRIES}


def _normalize(ids: Any) -> list[str]:
    known = _known_ids()
    out: list[str] = []
    if not isinstance(ids, list):
        return [i for i in DEFAULT_ALLOWED if i in known]
    for raw in ids:
        item = str(raw or "").strip()
        if item in known and item not in out:
            out.append(item)
    return out


def _row() -> dict[str, Any]:
    from mino_nexus.core.database import SessionLocal, ensure_db
    from mino_nexus.models.nav import StudioNav

    ensure_db()
    db = SessionLocal()
    try:
        row = db.get(StudioNav, "main")
        if row is None:
            return {"allowed": list(DEFAULT_ALLOWED), "v": _NAV_VERSION}
        return {"allowed": list(row.allowed or []), "v": int(row.version or 0)}
    finally:
        db.close()


def _persist(allowed: list[str], version: int) -> None:
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.nav import StudioNav

    with session_scope() as db:
        row = db.get(StudioNav, "main")
        if row is None:
            db.add(StudioNav(id="main", allowed=allowed, version=version))
        else:
            row.allowed = allowed
            row.version = version


def get_allowed() -> list[str]:
    raw = _row()
    allowed = _normalize(raw.get("allowed"))
    try:
        version = int(raw.get("v") or 0)
    except (TypeError, ValueError):
        version = 0
    if version < _NAV_VERSION and "scout" in _known_ids() and "scout" not in allowed:
        if "runtime" in allowed:
            allowed.insert(allowed.index("runtime") + 1, "scout")
        else:
            allowed.append("scout")
        _persist(allowed, _NAV_VERSION)
    return allowed


def save_allowed(ids: list[str] | None) -> dict[str, Any]:
    allowed = _normalize(ids)
    _persist(allowed, _NAV_VERSION)
    return public_nav()


def public_nav() -> dict[str, Any]:
    allowed = get_allowed()
    return {
        "entries": catalog(),
        "allowed": allowed,
        "default_allowed": list(DEFAULT_ALLOWED),
    }


def is_allowed(entry_id: str) -> bool:
    return str(entry_id or "").strip() in get_allowed()
