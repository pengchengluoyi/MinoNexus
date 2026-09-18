"""通用逻辑块 catalog seed（fb.global.*）。"""
from __future__ import annotations

from mino_nexus.models.nav_flow_block_catalog import GLOBAL_APP_ID, NavFlowBlockCatalog

LOGIN_BLOCK_ID = "fb.global.login"
SYSTEM_DIALOG_BLOCK_ID = "fb.global.system_dialog"

_DEFAULT_LOGIN_STEPS = [
    {"id": "sms_send", "cap": "request_sms_code"},
    {"id": "legal_consent", "cap": "accept_legal_consent"},
    {"id": "otp_fill", "cap": "get_otp", "optional": True},
]

_SYSTEM_DIALOG_STEPS = [
    {"id": "unified_permission", "cap": "recover_system_permission_dialog_unified", "internal": True},
]


def seed_global_flow_blocks() -> int:
    from mino_nexus.core.database import SessionLocal

    db = SessionLocal()
    n = 0
    try:
        rows = [
            {
                "block_id": LOGIN_BLOCK_ID,
                "display_name": "登录流（通用）",
                "description": "发码、协议勾选、取码；应用可 override。",
                "steps_json": list(_DEFAULT_LOGIN_STEPS),
            },
            {
                "block_id": SYSTEM_DIALOG_BLOCK_ID,
                "display_name": "系统弹窗（通用）",
                "description": "系统权限挡屏 unified recovery。",
                "steps_json": list(_SYSTEM_DIALOG_STEPS),
            },
        ]
        for row in rows:
            bid = str(row["block_id"])
            existing = (
                db.query(NavFlowBlockCatalog)
                .filter(
                    NavFlowBlockCatalog.app_id == GLOBAL_APP_ID,
                    NavFlowBlockCatalog.block_id == bid,
                )
                .first()
            )
            if existing is None:
                db.add(
                    NavFlowBlockCatalog(
                        app_id=GLOBAL_APP_ID,
                        block_id=bid,
                        block_origin="global_catalog",
                        display_name=str(row["display_name"]),
                        description=str(row["description"]),
                        steps_json=row["steps_json"],
                        version="v1",
                        enabled=1,
                    )
                )
                n += 1
            else:
                if not existing.steps_json:
                    existing.steps_json = row["steps_json"]
                    existing.display_name = str(row["display_name"])
                    n += 1
        db.commit()
    finally:
        db.close()
    return n
