"""Startup seeds for case evidence capabilities.

Capability ids stay ``note`` and ``sample``: case scripts and compiled
``hook_cap`` values use those names, the same way ``wait_ms`` does.
Session events use the ``domain/name`` stream (``case/note``, ``case/sample``),
alongside ``tool/result`` and ``assert/vision``.

Only these two ids are opened as local evidence. An unknown capability
still fails ``capability_unregistered`` in the router.
"""
from __future__ import annotations

EVIDENCE_IDS = ("note", "sample")
EVENT_TYPES = {"note": "case/note", "sample": "case/sample"}
PLATFORMS = ["mac", "win", "android", "web"]

_SPECS = (
    ("note", "记录说明", "把一段自由文本写入当前会话证据。不操作设备，不代替界面断言。"),
    ("sample", "记录样本", "把样本说明写入当前会话证据。不操作设备，不代替界面断言。"),
)


def _local_impl(cid: str) -> dict:
    return {
        "id": "nexus.local.v1:" + cid,
        "executor": "internal",
        "runs_on": "nexus",
        "platforms": list(PLATFORMS),
        "low_level": {},
        "params_contract": {"version": 1},
        "result_contract": {"version": 1},
        "device_hint_template": {},
    }


def register_evidence_capabilities() -> None:
    """Synchronize note/sample. A configured disable stays disabled."""
    from mino_nexus.catalog import registry
    from mino_nexus.catalog.system_registration import sync_system_capability
    from mino_nexus.catalog.tool_schema import PARAM_DEFAULTS, schema_to_param_rows
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.catalog import CatalogEntry

    with session_scope() as db:
        for cid, title, description in _SPECS:
            sync_system_capability(
                db, cid=cid, kind="generic", title=title, description=description,
                platforms=list(PLATFORMS),
                fields={
                    "event_kind": cid, "caller": "case", "phases": ["prep", "do", "check"],
                    "observe": "program", "channel_contract_version": 1,
                    "implementations": [_local_impl(cid)],
                    "params": schema_to_param_rows(PARAM_DEFAULTS[cid]),
                },
            )
            row = db.query(CatalogEntry).filter(
                CatalogEntry.id == cid, CatalogEntry.kind == "generic",
            ).one()
            row.display_name = title
            row.description = description
            row.ui_coverable = False
            if not row.owner:
                row.owner = "system"
            payload = dict(row.payload_json or {})
            impls = [item for item in (payload.get("implementations") or []) if isinstance(item, dict)]
            local = _local_impl(cid)
            if not any(
                item.get("id") == local["id"] and item.get("executor") == "internal"
                and item.get("runs_on") == "nexus"
                for item in impls
            ):
                impls.append(local)
            payload["implementations"] = impls
            row.payload_json = payload
    registry.reload()
