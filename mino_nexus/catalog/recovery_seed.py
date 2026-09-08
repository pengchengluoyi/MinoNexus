"""L0 恢复规则种子（insert-only）+ 已有规则 payload 升级。"""
from __future__ import annotations

from typing import Any

_RECOVERY_PAYLOAD_V2: dict[str, Any] = {
    "priority": 100,
    "mode": "execute",
    "max_attempts": 2,
    "match": {
        "evidence_any": [
            {"screen_blocked": "yes"},
            {"capture_black": "yes"},
            {"capture_ok": "no"},
        ],
    },
    "verify": {
        "evidence": {
            "screen_blocked": "no",
            "capture_black": "no",
            "capture_ok": "yes",
        },
    },
    "actions": [
        {"capability": "wake_screen", "params": {}},
        {"capability": "dismiss_keyguard", "params": {}},
    ],
    "prompt_snippet": (
        "设备锁屏、休眠，或截图全黑/无法读取时，先唤醒再解除锁屏。"
        "移动端锁屏时截图常为全黑，与 capture_black 等价处理。"
    ),
    "evidence_notes": [
        "screen_blocked=yes：probe 报告 asleep 或 keyguard",
        "capture_black=yes：截图均值亮度极低（常见于锁屏）",
        "capture_ok=no：Scout 未返回可用截图",
    ],
}


def builtin_recovery_rules() -> list[dict[str, Any]]:
    return [
        {
            "kind": "recovery",
            "id": "screen_asleep_or_locked",
            "display_name": "锁屏 / 休眠 / 黑屏",
            "description": "设备 asleep、keyguard 显示，或截图全黑/不可读时唤醒并解锁。",
            "enabled": True,
            "lifecycle": "active",
            "platforms_json": ["android"],
            "sort_order": 10,
            "payload_json": dict(_RECOVERY_PAYLOAD_V2),
        },
    ]


def seed_recovery_rules() -> int:
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.catalog import CatalogEntry

    added = 0
    with session_scope() as db:
        for spec in builtin_recovery_rules():
            kind = str(spec["kind"])
            cid = str(spec["id"])
            if db.query(CatalogEntry).filter(CatalogEntry.kind == kind, CatalogEntry.id == cid).first():
                continue
            db.add(
                CatalogEntry(
                    kind=kind,
                    id=cid,
                    display_name=str(spec.get("display_name") or cid),
                    description=str(spec.get("description") or ""),
                    enabled=bool(spec.get("enabled", True)),
                    lifecycle=str(spec.get("lifecycle") or "active"),
                    platforms_json=list(spec.get("platforms_json") or []),
                    payload_json=dict(spec.get("payload_json") or {}),
                    sort_order=int(spec.get("sort_order") or 0),
                )
            )
            added += 1
    if added:
        from mino_nexus.catalog.loader import force_reload

        force_reload()
    return added


def upgrade_recovery_rules() -> int:
    """已有库把 screen_asleep_or_locked 升到 v2 match（黑屏 OR 锁屏）。"""
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.catalog import CatalogEntry

    updated = 0
    with session_scope() as db:
        row = (
            db.query(CatalogEntry)
            .filter(CatalogEntry.kind == "recovery", CatalogEntry.id == "screen_asleep_or_locked")
            .first()
        )
        if not row:
            return 0
        payload = dict(row.payload_json or {})
        if payload.get("match", {}).get("evidence_any"):
            return 0
        row.display_name = "锁屏 / 休眠 / 黑屏"
        row.description = builtin_recovery_rules()[0]["description"]
        row.payload_json = dict(_RECOVERY_PAYLOAD_V2)
        updated = 1
    if updated:
        from mino_nexus.catalog.loader import force_reload

        force_reload()
    return updated
