"""Startup seed for Scout ``self_prove`` on the mcp0.0.1 line.

The capability id is ``self_prove``. It is not a local executor. Android
selects the catalog implementation ``self_prove.adb``. iOS uses the
branch's existing ``ios_wda`` family: the catalog implementation
``self_prove.ios_wda`` when that channel is connected, and ``WDA_CAPS``
so the same fallback still applies. ``device_hint.udid`` is already
filled for iOS devices.

``RESULT.data.schema`` is ``self_prove.v1``. The fields live in
``docs/基础框架/PROTOCOL.md`` §4.4.3. This module does not declare a
second result schema. Requires a Scout that implements MinoScout #2.
"""
from __future__ import annotations

CAPABILITY_ID = "self_prove"
# Catalog platforms stay inside the V7 set. ``ios`` is only the wire proof
# family for executor ios_wda, not a catalog target platform.
PLATFORMS = ["android"]

_IMPLS = (
    {
        "id": "self_prove.adb",
        "executor": "adb",
        "runs_on": "scout",
        "platforms": ["android"],
        "low_level": {},
        "params_contract": {"version": 1},
        "result_contract": {"version": 1},
        "device_hint_template": {"execution_mode": "agent"},
    },
    {
        "id": "self_prove.ios_wda",
        "executor": "ios_wda",
        "runs_on": "scout",
        "platforms": ["ios"],
        "low_level": {},
        "params_contract": {"version": 1},
        "result_contract": {"version": 1},
        "device_hint_template": {"execution_mode": "agent"},
    },
)


def register_self_prove_capability() -> None:
    """Synchronize the Scout route. A configured disable stays disabled."""
    from mino_nexus.catalog import registry
    from mino_nexus.catalog.system_registration import sync_system_capability
    from mino_nexus.catalog.tool_schema import PARAM_DEFAULTS, schema_to_param_rows
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.catalog import CatalogEntry

    description = (
        "一次读取当前设备、节点和可选安装包的自证，发给 Scout 执行。"
        "Android 的 executor_order 是 adb；iOS 是 ios_wda。"
        "设备应答即为 pass，缺摘要或证书仍 pass，差距在 missing。"
    )
    with session_scope() as db:
        sync_system_capability(
            db, cid=CAPABILITY_ID, kind="generic", title="执行环境自证",
            description=description, platforms=list(PLATFORMS),
            fields={
                "event_kind": CAPABILITY_ID, "caller": "case",
                "phases": ["prep", "do", "check"], "observe": "program",
                "channel_contract_version": 1,
                "implementations": [dict(item) for item in _IMPLS],
                "params": schema_to_param_rows(PARAM_DEFAULTS[CAPABILITY_ID]),
            },
        )
        row = db.query(CatalogEntry).filter(
            CatalogEntry.id == CAPABILITY_ID, CatalogEntry.kind == "generic",
        ).one()
        row.display_name = "执行环境自证"
        row.description = description
        row.ui_coverable = False
        if not row.owner:
            row.owner = "system"
        payload = dict(row.payload_json or {})
        payload["implementations"] = [dict(item) for item in _IMPLS]
        row.payload_json = payload
    registry.reload()
