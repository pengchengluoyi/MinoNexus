"""通用逻辑块 catalog seed（fb.global.*）。"""
from __future__ import annotations

from mino_nexus.models.nav_flow_block_catalog import GLOBAL_APP_ID, NavFlowBlockCatalog

LOGIN_BLOCK_ID = "fb.global.login"
SYSTEM_DIALOG_BLOCK_ID = "fb.global.system_dialog"
_RETIRED_LOGIN_BLOCK_IDS = ("fb.global.login.email_web",)

# 一条登录链。账号和验证码由 lease_account / get_otp 按用例给出，不按邮箱/手机、Web/App 拆块。
_UNIFIED_LOGIN_STEPS = [
    {"id": "login_entry", "kind": "visual_tap", "title": "点击登录入口", "cap": "tap_element"},
    {
        "id": "account_field",
        "kind": "visual_tap",
        "title": "聚焦账号输入框",
        "cap": "tap_element",
    },
    {
        "id": "account_fill",
        "kind": "hook",
        "title": "填写登录账号",
        "hook_cap": "lease_account",
        "cap": "input_text",
    },
    {
        "id": "send_code",
        "kind": "visual_tap",
        "title": "发送验证码",
        "cap": "tap_element",
        "match": ["Send", "Send code", "Get code", "Resend", "发送验证码", "获取验证码", "重新发送"],
        "exclude": [
            "Continue",
            "通过 Google 继续操作",
            "通过 Apple 继续操作",
            "Continue with Google",
            "Continue with Apple",
            "登录",
            "Log in",
            "Login",
            "Sign in",
        ],
    },
    {
        "id": "otp_fetch",
        "kind": "hook",
        "title": "获取验证码",
        "hook_cap": "get_otp",
    },
    {
        "id": "otp_field",
        "kind": "visual_tap",
        "title": "聚焦验证码输入框",
        "cap": "tap_element",
    },
    {
        "id": "otp_fill",
        "kind": "hook",
        "title": "填写验证码",
        "cap": "input_text",
    },
    {
        "id": "legal_consent",
        "kind": "hook",
        "title": "同意协议",
        "hook_cap": "accept_legal_consent",
        "optional": True,
    },
    {
        "id": "submit_login",
        "kind": "visual_tap",
        "title": "提交登录",
        "cap": "tap_element",
        "match": ["登录", "Log in", "Login", "Sign in"],
        "match_when": [{"text": "Continue", "when": "otp_filled"}],
        "exclude": [
            "Send",
            "Send code",
            "Get code",
            "Resend",
            "发送验证码",
            "获取验证码",
            "重新发送",
            "通过 Google 继续操作",
            "通过 Apple 继续操作",
            "Continue with Google",
            "Continue with Apple",
        ],
        "fuse": {"same_target_repeat": 2, "action": "fuse_block"},
    },
    {
        "id": "login_state",
        "kind": "hook",
        "title": "确认登录态",
        "hook_cap": "confirm_login_state",
        "params": {"max_turns": 3},
    },
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
                "description": "登录入口→填账号→发码→取码→填验证码→同意协议→提交→确认登录态。lease_account / get_otp 按用例给凭证。",
                "steps_json": list(_UNIFIED_LOGIN_STEPS),
                "version": "v10",
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
                        version=str(row.get("version") or "v1"),
                        enabled=1,
                    )
                )
                n += 1
            else:
                if not existing.steps_json:
                    existing.steps_json = row["steps_json"]
                    existing.display_name = str(row["display_name"])
                    n += 1
                ver = str(row.get("version") or "")
                if ver in ("v2", "v3", "v4", "v5", "v6", "v7", "v8", "v9", "v10") and str(existing.version or "") != ver:
                    existing.steps_json = row["steps_json"]
                    existing.version = ver
                    existing.display_name = str(row["display_name"])
                    existing.description = str(row.get("description") or existing.description or "")
                    n += 1
        for retired in _RETIRED_LOGIN_BLOCK_IDS:
            gone = (
                db.query(NavFlowBlockCatalog)
                .filter(
                    NavFlowBlockCatalog.app_id == GLOBAL_APP_ID,
                    NavFlowBlockCatalog.block_id == retired,
                )
                .first()
            )
            if gone is not None:
                db.delete(gone)
                n += 1
        db.commit()
    finally:
        db.close()
    return n
