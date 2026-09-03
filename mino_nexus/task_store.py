"""轻量任务记录。对齐上游 `rTask` 列表/创建，执行循环另迁。"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from mino_nexus.json_store import load_json, save_json

_FILE = "tasks.json"


def _root() -> dict[str, Any]:
    raw = load_json(_FILE, {})
    if not isinstance(raw, dict):
        raw = {}
    raw.setdefault("tasks", [])
    if not isinstance(raw["tasks"], list):
        raw["tasks"] = []
    return raw


def create_task(app_id: str, name: str, type_: str) -> dict[str, Any]:
    root = _root()
    row = {
        "id": str(uuid.uuid4()),
        "app_id": app_id,
        "name": name,
        "type": type_,
        "status": "pending",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    root["tasks"].append(row)
    save_json(_FILE, root)
    return row


def list_tasks(app_id: str = "", type_: str = "", keyword: str = "") -> list[dict[str, Any]]:
    rows = [t for t in _root()["tasks"] if isinstance(t, dict)]
    if app_id:
        rows = [t for t in rows if t.get("app_id") == app_id]
    if type_ and type_ != "all":
        rows = [t for t in rows if t.get("type") == type_]
    if keyword:
        kw = keyword.lower()
        rows = [t for t in rows if kw in str(t.get("name") or "").lower()]
    rows.sort(key=lambda t: str(t.get("created_at") or ""), reverse=True)
    return rows
