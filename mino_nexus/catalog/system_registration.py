"""Current system declarations; only explicit built-in IDs call this registrar."""
from __future__ import annotations

from copy import deepcopy
from typing import Any


def sync_system_capability(db: Any, *, cid: str, kind: str, title: str,
                           platforms: list[str], fields: dict[str, Any],
                           description: str = "") -> None:
    from mino_nexus.catalog.contracts import validate_capability_payload
    from mino_nexus.models.catalog import CatalogEntry

    validate_capability_payload(fields, platforms=platforms, lifecycle="active")
    rows = db.query(CatalogEntry).filter(CatalogEntry.id == cid).order_by(CatalogEntry.pk).all()
    row = next((item for item in rows if item.kind == kind), None)
    if row is None and len(rows) == 1:
        row = rows[0]
        row.kind = kind
    if row is None:
        row = CatalogEntry(id=cid, kind=kind, display_name=title, description=description,
                           enabled=all(item.enabled for item in rows),
                           lifecycle="active", payload_json={})
        db.add(row)
    # Do not delete duplicate records or their payloads. The declared kind is the
    # sole runtime identity; other kinds are retained as inactive audit records.
    for item in rows:
        if item is not row:
            item.enabled = False
            item.lifecycle = "deprecated"
    payload = dict(row.payload_json or {})
    implementations = payload.get("implementations")
    payload.update(deepcopy(fields))
    # Configured execution bindings remain authoritative, including explicit [].
    if isinstance(implementations, list):
        payload["implementations"] = implementations
    payload.pop("parameters", None)
    row.payload_json = payload
    row.platforms_json = list(platforms)
    row.visible_to_json = ["system"]
    db.flush()
