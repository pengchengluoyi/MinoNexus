"""图谱别名。落 m_atlas_alias。"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _root() -> dict[str, Any]:
    from mino_nexus.core.database import SessionLocal, ensure_db
    from mino_nexus.models.atlas import AtlasAlias

    ensure_db()
    db = SessionLocal()
    try:
        out: dict[str, list[dict[str, Any]]] = {}
        for row in db.query(AtlasAlias).all():
            payload = dict(row.payload or {}) if isinstance(row.payload, dict) else {}
            if not payload.get("id"):
                payload["id"] = str(row.id)
            out.setdefault(str(row.app_id), []).append(payload)
        return out
    finally:
        db.close()


def _save(root: dict[str, Any]) -> None:
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.atlas import AtlasAlias

    with session_scope() as db:
        db.query(AtlasAlias).delete()
        for app_id, rows in root.items():
            if not isinstance(rows, list):
                continue
            for row in rows:
                if isinstance(row, dict):
                    db.add(AtlasAlias(app_id=str(app_id), payload=dict(row)))


def list_aliases(app_id: str) -> list[dict[str, Any]]:
    rows = _root().get(app_id) or []
    return [x for x in rows if isinstance(x, dict)]


def upsert(app_id: str, row: dict[str, Any]) -> dict[str, Any]:
    root = _root()
    rows = [x for x in (root.get(app_id) or []) if isinstance(x, dict)]
    rid = str(row.get("id") or "").strip() or uuid.uuid4().hex[:12]
    item = {
        **row,
        "id": rid,
        "updated_at": _now(),
    }
    for i, old in enumerate(rows):
        if str(old.get("id")) == rid:
            rows[i] = item
            break
    else:
        rows.append(item)
    root[app_id] = rows
    _save(root)
    return item


def patch(app_id: str, alias_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    rows = list_aliases(app_id)
    for row in rows:
        if str(row.get("id")) == str(alias_id):
            row.update({k: v for k, v in payload.items() if v is not None})
            row["updated_at"] = _now()
            root = _root()
            root[app_id] = [
                row if str(x.get("id")) == str(alias_id) else x for x in (root.get(app_id) or [])
            ]
            _save(root)
            return row
    return None


def delete(app_id: str, alias_id: str) -> bool:
    root = _root()
    rows = [x for x in (root.get(app_id) or []) if isinstance(x, dict)]
    nxt = [x for x in rows if str(x.get("id")) != str(alias_id)]
    if len(nxt) == len(rows):
        return False
    root[app_id] = nxt
    _save(root)
    return True
