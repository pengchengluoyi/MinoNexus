"""通用逻辑块 catalog seed（fb.global.*）。"""
from __future__ import annotations

from mino_nexus.models.nav_flow_block_catalog import GLOBAL_APP_ID, NavFlowBlockCatalog

LOGIN_BLOCK_ID = "fb.global.login"
SYSTEM_DIALOG_BLOCK_ID = "fb.global.system_dialog"
# Explicit release lineage; unknown versions are never ordered by guessing.
_LOGIN_VERSION_ORDER = (
    "v1", "v2", "v3", "v4", "v5", "v6", "v7",
    "v8", "v9", "v10", "v11", "v12", "v13",
)
_VERSION_ORDER = {
    LOGIN_BLOCK_ID: _LOGIN_VERSION_ORDER,
    LOGIN_BLOCK_ID + ".send_code": _LOGIN_VERSION_ORDER,
    LOGIN_BLOCK_ID + ".legal_consent": _LOGIN_VERSION_ORDER,
    SYSTEM_DIALOG_BLOCK_ID: ("v1",),
}


def _is_seed_upgrade(block_id: str, current: str, target: str) -> bool:
    order = _VERSION_ORDER.get(block_id, ())
    return current in order and target in order and order.index(current) < order.index(target)


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
        "exec_caps": ["press_key", "probe_device_state", "hierarchy", "screenshot"],
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
        "id": "legal_consent",
        "kind": "hook",
        "title": "同意协议",
        "hook_cap": "operation.check_box",
        "params": {"checked": True},
        "exec_caps": ["tap_element", "press_key", "probe_device_state", "hierarchy", "screenshot"],
        "optional": False,
        "requirement": "conditional",
        "condition": {"kind": "login_consent", "max_observations": 3},
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
                "description": "登录入口→填账号→发码→取码→核验协议→聚焦并填验证码→提交→确认登录态。lease_account / get_otp 按用例给凭证。",
                "steps_json": list(_UNIFIED_LOGIN_STEPS),
                "version": "v13",
            },
            {
                "block_id": SYSTEM_DIALOG_BLOCK_ID,
                "display_name": "系统弹窗（通用）",
                "description": "系统权限挡屏 unified recovery。",
                "steps_json": list(_SYSTEM_DIALOG_STEPS),
            },
        ]
        for step_id in ('send_code', 'legal_consent'):
            rows.append({'block_id': LOGIN_BLOCK_ID + '.' + step_id,
                'display_name': '登录步骤：' + step_id, 'description': '登录块的独立业务步骤；不是事件别名。',
                'steps_json': [{**{k:v for k,v in step.items() if k not in {'condition', 'requirement'}}, 'optional': False} for step in _UNIFIED_LOGIN_STEPS if step['id'] == step_id],
                'version': 'v13'})
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
            elif (existing.block_origin == "global_catalog"
                  and _is_seed_upgrade(bid, str(existing.version or ""),
                                       str(row.get("version") or "v1"))):
                existing.steps_json = row["steps_json"]
                existing.version = str(row.get("version") or "v1")
                existing.display_name = str(row["display_name"])
                existing.description = str(row.get("description") or "")
                n += 1
        # Legacy IDs are no longer seeded, but startup must not delete them:
        # a newer release or an existing plan may still reference those rows.
        db.commit()
    finally:
        db.close()
    return n
