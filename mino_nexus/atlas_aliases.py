"""图谱别名：JSON 文件，不接 SQLAlchemy。"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from mino_nexus.json_store import load_json, save_json

_FILE = "atlas_aliases.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _root() -> dict[str, Any]:
    raw = load_json(_FILE, {})
    return raw if isinstance(raw, dict) else {}


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
    save_json(_FILE, root)
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
            save_json(_FILE, root)
            return row
    return None


def delete(app_id: str, alias_id: str) -> bool:
    root = _root()
    rows = [x for x in (root.get(app_id) or []) if isinstance(x, dict)]
    nxt = [x for x in rows if str(x.get("id")) != str(alias_id)]
    if len(nxt) == len(rows):
        return False
    root[app_id] = nxt
    save_json(_FILE, root)
    return True
