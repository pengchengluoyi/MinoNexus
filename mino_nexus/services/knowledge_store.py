"""业务知识条目。落 knowledge_entries。"""
from __future__ import annotations

import time
import uuid
from typing import Any

from mino_nexus.models.knowledge import KnowledgeEntry


def _normalize(raw: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    title = str(raw.get("title") or "").strip()
    if not title:
        return None
    extra = {
        k: v for k, v in raw.items()
        if k not in {
            "score", "match_pct", "used", "skip_reason",
            "id", "title", "content", "category", "tags", "app_ids", "enabled",
            "source", "review_status", "account_id", "account_ident",
            "tags_json", "app_ids_json", "extra_json", "updated_at",
        } and v not in (None, "", [], {})
    }
    extra.update(raw.get("extra") or raw.get("extra_json") or {})
    src = str(raw.get("source") or "").strip().lower() or "manual"
    st = str(raw.get("review_status") or "").strip().lower() or "approved"
    return {
        **extra,
        "id": str(raw.get("id") or "").strip() or uuid.uuid4().hex[:12],
        "title": title,
        "content": str(raw.get("content") or "").strip(),
        "category": str(raw.get("category") or "").strip() or "其他",
        "tags": [str(t).strip() for t in (raw.get("tags") or raw.get("tags_json") or []) if str(t).strip()],
        "app_ids": [str(a).strip() for a in (raw.get("app_ids") or raw.get("app_ids_json") or []) if str(a).strip()],
        "enabled": raw.get("enabled", True) is not False,
        "source": src,
        "review_status": st if st in {"approved", "pending", "rejected"} else "approved",
        "account_id": str(raw.get("account_id") or "").strip(),
        "account_ident": str(raw.get("account_ident") or "").strip(),
    }


def _to_public(row: KnowledgeEntry) -> dict[str, Any]:
    extra = dict(row.extra_json or {})
    return {
        **extra,
        "id": row.id,
        "title": row.title or "",
        "content": row.content or "",
        "category": row.category or "其他",
        "tags": list(row.tags_json or []),
        "app_ids": list(row.app_ids_json or []),
        "enabled": row.enabled is not False,
        "source": row.source or "manual",
        "review_status": row.review_status or "approved",
        "account_id": row.account_id or "",
        "account_ident": row.account_ident or "",
    }


def _apply(row: KnowledgeEntry, data: dict[str, Any]) -> None:
    known = {
        "id", "title", "content", "category", "tags", "app_ids", "enabled",
        "source", "review_status", "account_id", "account_ident",
    }
    row.title = data["title"]
    row.content = data["content"]
    row.category = data["category"]
    row.tags_json = data["tags"]
    row.app_ids_json = data["app_ids"]
    row.enabled = data["enabled"]
    row.source = data["source"]
    row.review_status = data["review_status"]
    row.account_id = data["account_id"]
    row.account_ident = data["account_ident"]
    row.extra_json = {k: v for k, v in data.items() if k not in known}
    row.updated_at = int(time.time())


def _match_filters(
    row: dict[str, Any],
    *,
    app_id: str = "",
    account_id: str = "",
    account_ident: str = "",
) -> bool:
    want = str(app_id or "").strip()
    if want and row["app_ids"] and want not in row["app_ids"]:
        return False
    want_acc = str(account_id or "").strip()
    want_ident = str(account_ident or "").strip()
    row_acc = str(row.get("account_id") or "").strip()
    row_ident = str(row.get("account_ident") or "").strip()
    if (want_acc or want_ident) and (row_acc or row_ident):
        if want_acc and row_acc and row_acc != want_acc:
            return False
        if want_ident and row_ident and row_ident != want_ident:
            return False
        if want_acc and not row_acc and row_ident and want_ident and row_ident != want_ident:
            return False
    return True


def list_testing_knowledge(*, app_id: str = "", account_id: str = "", account_ident: str = "") -> list[dict[str, Any]]:
    from mino_nexus.core.database import SessionLocal, ensure_db

    ensure_db()
    db = SessionLocal()
    try:
        rows = db.query(KnowledgeEntry).order_by(KnowledgeEntry.updated_at.desc(), KnowledgeEntry.id).all()
        out: list[dict[str, Any]] = []
        for raw in rows:
            row = _to_public(raw)
            if _match_filters(row, app_id=app_id, account_id=account_id, account_ident=account_ident):
                out.append(row)
        return out
    finally:
        db.close()


def count_for_app(app_id: str) -> int:
    aid = str(app_id or "").strip()
    if not aid:
        return 0
    return len(list_testing_knowledge(app_id=aid))


def save_testing_knowledge(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from mino_nexus.core.database import session_scope

    cleaned: list[dict[str, Any]] = []
    for raw in items or []:
        row = _normalize(raw)
        if row:
            cleaned.append(row)
    with session_scope() as db:
        keep = {row["id"] for row in cleaned}
        for existing in db.query(KnowledgeEntry).all():
            if existing.id not in keep:
                db.delete(existing)
        for data in cleaned:
            row = db.get(KnowledgeEntry, data["id"])
            if row is None:
                row = KnowledgeEntry(id=data["id"])
                db.add(row)
            _apply(row, data)
    return cleaned


def upsert_knowledge_item(item: dict[str, Any]) -> dict[str, Any]:
    data = _normalize(item)
    if not data:
        raise ValueError("知识条目缺少标题")
    from mino_nexus.core.database import session_scope

    with session_scope() as db:
        row = db.get(KnowledgeEntry, data["id"])
        if row is None:
            row = KnowledgeEntry(id=data["id"])
            db.add(row)
        _apply(row, data)
    return data


def delete_knowledge_item(kid: str) -> bool:
    kid = str(kid or "").strip()
    if not kid:
        return False
    from mino_nexus.core.database import session_scope

    with session_scope() as db:
        row = db.get(KnowledgeEntry, kid)
        if row is None:
            return False
        db.delete(row)
    return True


def review_knowledge_item(kid: str, *, action: str, updates: dict[str, Any] | None = None) -> dict[str, Any]:
    kid = str(kid or "").strip()
    act = str(action or "").strip().lower()
    if act not in {"approve", "reject"}:
        raise ValueError("action 必须是 approve 或 reject")
    existing = next((x for x in list_testing_knowledge() if str(x.get("id")) == kid), None)
    if not existing:
        raise KeyError(kid)
    if act == "reject":
        delete_knowledge_item(kid)
        return {**existing, "review_status": "rejected", "deleted": True}
    payload = dict(existing)
    if isinstance(updates, dict):
        payload.update({k: v for k, v in updates.items() if v is not None})
    payload["id"] = kid
    payload["review_status"] = "approved"
    return upsert_knowledge_item(payload)


def _insert_if_new(db, item: dict[str, Any]) -> int:
    data = _normalize(item)
    if not data or db.get(KnowledgeEntry, data["id"]) is not None:
        return 0
    entry = KnowledgeEntry(id=data["id"])
    _apply(entry, data)
    db.add(entry)
    return 1


def migrate_legacy(db) -> int:
    """把旧 settings.payload 和 leftover learned YAML 搬进 knowledge_entries。已有 id 跳过。"""
    return _migrate_from_settings(db) + _migrate_from_learned_files(db)


def _migrate_from_settings(db) -> int:
    from mino_nexus.models.settings import Settings

    row = db.get(Settings, "main")
    raw = dict(row.payload or {}) if row and isinstance(row.payload, dict) else {}
    items = raw.get("knowledge")
    if not isinstance(items, list) or not items:
        return 0
    n = 0
    for item in items:
        if isinstance(item, dict):
            n += _insert_if_new(db, item)
    raw.pop("knowledge", None)
    if row is not None:
        row.payload = raw
    return n


def _parse_learned_yaml(text: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    current: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith("- ") and current is not None:
            val = line[2:].strip().strip("'\"")
            bucket = out.setdefault(current, [])
            if isinstance(bucket, list):
                bucket.append(val)
            continue
        if ":" not in line or line.startswith(" "):
            continue
        key, _, rest = line.partition(":")
        key = key.strip()
        rest = rest.strip().strip("'\"")
        current = key
        if rest == "":
            out[key] = []
        elif rest.lower() in {"true", "false"}:
            out[key] = rest.lower() == "true"
        else:
            out[key] = rest
    return out


def _migrate_from_learned_files(db) -> int:
    from mino_nexus.core.paths import data_dir

    folder = data_dir() / "packs" / "learned" / "knowledge" / "entries"
    if not folder.is_dir():
        return 0
    n = 0
    for path in sorted(folder.glob("*.yaml")) + sorted(folder.glob("*.yml")):
        try:
            raw = _parse_learned_yaml(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        if not raw.get("id"):
            raw["id"] = path.stem
        n += _insert_if_new(db, raw)
    return n
