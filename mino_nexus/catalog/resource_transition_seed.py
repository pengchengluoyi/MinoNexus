"""内置 resource_transition_rules（空库灌种一次，之后以库为准）。"""
from __future__ import annotations

from datetime import datetime, timezone

_BUILTIN: list[dict] = [
    {
        "rule_id": "clear_app_cache:any",
        "trigger_id": "clear_app_cache",
        "platform": "any",
        "label": "清缓存 → 机态未登录",
        "effects_json": [{"action": "device_logout", "stale_reason": "clear_app_cache"}],
    },
    {
        "rule_id": "system_pkg_clear:any",
        "trigger_id": "system_pkg_clear",
        "platform": "any",
        "label": "系统清包 → 机态未登录",
        "effects_json": [{"action": "device_logout", "stale_reason": "system_pkg_clear"}],
    },
    {
        "rule_id": "nav_logout:any",
        "trigger_id": "nav_logout",
        "platform": "any",
        "label": "路线图 logout",
        "effects_json": [{"action": "device_logout", "source": "nav_logout"}],
    },
    {
        "rule_id": "inspect_session:logged_in",
        "trigger_id": "inspect_session_logged_in",
        "platform": "any",
        "label": "inspect 已登录",
        "effects_json": [{"action": "device_login", "source": "inspect_session"}],
    },
    {
        "rule_id": "inspect_session:logged_out",
        "trigger_id": "inspect_session_logged_out",
        "platform": "any",
        "label": "inspect 未登录",
        "effects_json": [{"action": "device_logout", "source": "inspect_session"}],
    },
    {
        "rule_id": "flow_login:any",
        "trigger_id": "login_flow_complete",
        "platform": "any",
        "label": "登录流块完成",
        "effects_json": [{"action": "device_login", "source": "flow_block"}],
    },
    {
        "rule_id": "cap_logout:any",
        "trigger_id": "cap_logout",
        "platform": "any",
        "label": "登出类 capability",
        "effects_json": [{"action": "device_logout", "source": "capability"}],
    },
]


def seed_resource_transition_rules() -> int:
    from mino_nexus.core.database import SessionLocal
    from mino_nexus.models.resource_ops import ResourceTransitionRule

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    db = SessionLocal()
    n = 0
    try:
        for row in _BUILTIN:
            rid = str(row["rule_id"])
            if db.query(ResourceTransitionRule).filter(ResourceTransitionRule.rule_id == rid).first():
                continue
            db.add(
                ResourceTransitionRule(
                    rule_id=rid,
                    trigger_id=str(row["trigger_id"]),
                    platform=str(row.get("platform") or "any"),
                    label=str(row.get("label") or ""),
                    effects_json=list(row.get("effects_json") or []),
                    enabled=1,
                    updated_at=now,
                )
            )
            n += 1
        db.commit()
    finally:
        db.close()
    return n


def list_builtin_rules() -> list[dict]:
    return list(_BUILTIN)
