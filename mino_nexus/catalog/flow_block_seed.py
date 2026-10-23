"""通用逻辑块 catalog seed（fb.global.*）。"""
from __future__ import annotations

from mino_nexus.models.nav_flow_block_catalog import GLOBAL_APP_ID, NavFlowBlockCatalog

LOGIN_BLOCK_ID = "fb.global.login"
SYSTEM_DIALOG_BLOCK_ID = "fb.global.system_dialog"
LOGOUT_BLOCK_ID = "fb.global.logout"
# 首启隐私/用户协议弹窗：同意与拒绝是两条不同的业务后果，用同模板的两个变体表达，
# 由用例编译期选定 block id（不靠模型临场判断）。
PRIVACY_ACCEPT_BLOCK_ID = "fb.global.privacy.accept"
WAKE_BLOCK_ID = "fb.global.device.wake"
FOREGROUND_BLOCK_ID = "fb.global.app.foreground"
RESTART_APP_BLOCK_ID = "fb.global.app.restart"

# L0 恢复规则与逻辑块的对应关系：动作步骤的真源是块，规则只负责匹配与验证。
RECOVERY_RULE_BLOCKS: dict[str, str] = {
    "screen_asleep_or_locked": WAKE_BLOCK_ID,
    "bring_target_app_foreground": FOREGROUND_BLOCK_ID,
    "restart_target_app": RESTART_APP_BLOCK_ID,
}
PRIVACY_DECLINE_BLOCK_ID = "fb.global.privacy.decline"
# Explicit release lineage; unknown versions are never ordered by guessing.
_LOGIN_VERSION_ORDER = (
    "v1", "v2", "v3", "v4", "v5", "v6", "v7",
    "v8", "v9", "v10", "v11", "v12", "v13",
)
_VERSION_ORDER = {
    LOGIN_BLOCK_ID: _LOGIN_VERSION_ORDER,
    LOGOUT_BLOCK_ID: ("v1", "v2"),
    PRIVACY_ACCEPT_BLOCK_ID: ("v1",),
    SYSTEM_DIALOG_BLOCK_ID: ("v1", "v2"),
    WAKE_BLOCK_ID: ("v1",),
    FOREGROUND_BLOCK_ID: ("v1",),
    RESTART_APP_BLOCK_ID: ("v1",),
    SYSTEM_DIALOG_BLOCK_ID + ".deny": ("v1",),
    PRIVACY_DECLINE_BLOCK_ID: ("v1",),
    LOGIN_BLOCK_ID + ".send_code": _LOGIN_VERSION_ORDER,
    LOGIN_BLOCK_ID + ".legal_consent": _LOGIN_VERSION_ORDER,
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

# 退出登录链：所有平台都走应用自己的退出入口（安卓非 root 没有跨应用登录态 API）。
# 入口文案走 App 覆盖，块里只放通用词，不硬编码被测应用文案。
_UNIFIED_LOGOUT_STEPS = [
    {
        "id": "logout_entry",
        "kind": "visual_tap",
        "title": "进入退出登录入口",
        "cap": "tap_element",
        "exec_caps": ["tap_element", "swipe_direction", "press_key", "probe_device_state",
                      "hierarchy", "screenshot"],
        "match": ["退出登录", "退出", "Log out", "Logout", "Sign out", "设置", "Settings"],
        "exclude": ["退出应用", "Quit", "取消", "Cancel"],
    },
    {
        "id": "logout_confirm",
        "kind": "visual_tap",
        "title": "确认退出登录",
        "cap": "tap_element",
        # 有的应用有二次确认弹窗，有的没有：按可选步骤处理，没弹窗就跳过。
        "optional": True,
        "match": ["确认退出", "确认", "确定", "退出", "OK", "Confirm", "Log out"],
    },
    {
        "id": "logout_verified",
        "kind": "hook",
        "title": "核验已退出登录",
        "hook_cap": "confirm_login_state",
        "params": {"max_turns": 3},
    },
]

# 首启协议弹窗：块里只放通用词，App 特有文案走应用覆盖。
# 同意 → 弹窗关闭并进入主界面；拒绝 → 弹窗关闭且不要求进入主界面（有的应用直接退出）。
_PRIVACY_BASE_STEPS = [
    {
        "id": "privacy_choice",
        "kind": "visual_tap",
        "title": "在协议弹窗里选择",
        "cap": "tap_element",
        "exec_caps": ["tap_element", "probe_device_state", "hierarchy", "screenshot"],
        "optional": False,
    },
    {
        "id": "privacy_confirm",
        "kind": "visual_tap",
        "title": "确认二次弹窗（如有）",
        "cap": "tap_element",
        "optional": True,
    },
    {
        "id": "privacy_verified",
        "kind": "hook",
        "title": "核验协议弹窗已关闭（重新观察当前画面）",
        "hook_cap": "screenshot",
        "key_ref": "operation.observe",
    },
]

_PRIVACY_ACCEPT_STEPS = [
    {**_PRIVACY_BASE_STEPS[0],
     "match": ["同意", "同意并继续", "同意并登录", "Agree", "Accept", "Allow"],
     "exclude": ["不同意", "拒绝", "Disagree", "Decline"]},
    {**_PRIVACY_BASE_STEPS[1],
     "match": ["确认", "确定", "同意", "OK", "Confirm"]},
    _PRIVACY_BASE_STEPS[2],
]

_PRIVACY_DECLINE_STEPS = [
    {**_PRIVACY_BASE_STEPS[0],
     "match": ["不同意", "拒绝", "暂不同意", "仅浏览", "Disagree", "Decline", "Not now"],
     "exclude": ["同意并继续", "Agree", "Accept"]},
    {**_PRIVACY_BASE_STEPS[1],
     "match": ["确认", "确定", "仍不同意", "退出", "OK", "Confirm"]},
    _PRIVACY_BASE_STEPS[2],
]

# 系统权限弹窗：允许 / 拒绝两个变体，由用例编译期选定 block id。
# 以前这一步是 {"cap": "recover_system_permission_dialog_unified", "internal": true}：
# 事件在本仓能力目录里根本不存在，块展开后没有槽位，所以永远“未启用”。
_WAKE_STEPS = [
    {"id": "device_wake", "kind": "hook", "title": "唤醒设备屏幕", "cap": "wake_screen",
     "optional": False},
    {"id": "device_unlock", "kind": "hook", "title": "解除锁屏进到桌面", "cap": "dismiss_keyguard",
     "optional": False},
]

_FOREGROUND_STEPS = [
    {"id": "app_foreground", "kind": "hook", "title": "把被测应用拉回前台", "cap": "launch_app",
     "optional": False},
]

_RESTART_APP_STEPS = [
    {"id": "app_restart_close", "kind": "hook", "title": "关闭被测应用进程", "cap": "close_app",
     "optional": False},
    {"id": "app_restart_launch", "kind": "hook", "title": "重新启动被测应用", "cap": "launch_app",
     "optional": False},
]

_SYSTEM_DIALOG_ALLOW_STEPS = [
    {
        "id": "system_dialog_allow",
        "kind": "visual_tap",
        "title": "允许系统权限请求",
        "cap": "tap_element",
        "exec_caps": ["tap_element", "probe_device_state", "hierarchy", "screenshot"],
        "match": ["始终允许", "仅在使用中允许", "使用时允许", "允许", "Allow", "While using the app", "OK"],
        "exclude": ["拒绝", "禁止", "Deny", "Don't allow"],
    },
    {
        "id": "system_dialog_confirm",
        "kind": "visual_tap",
        "title": "确认系统弹窗（如有）",
        "cap": "tap_element",
        "optional": True,
        "match": ["确定", "知道了", "OK", "Got it", "Allow"],
    },
    {
        "id": "system_dialog_verified",
        "kind": "hook",
        "title": "核验系统弹窗已关闭（重新观察当前画面）",
        "hook_cap": "screenshot",
        "key_ref": "operation.observe",
    },
]

_SYSTEM_DIALOG_DENY_STEPS = [
    {
        "id": "system_dialog_deny",
        "kind": "visual_tap",
        "title": "拒绝系统权限请求",
        "cap": "tap_element",
        "exec_caps": ["tap_element", "probe_device_state", "hierarchy", "screenshot"],
        "match": ["拒绝", "禁止", "不允许", "Deny", "Don't allow"],
        "exclude": ["始终允许", "Allow"],
    },
    _SYSTEM_DIALOG_ALLOW_STEPS[1],
    _SYSTEM_DIALOG_ALLOW_STEPS[2],
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
                "block_id": LOGOUT_BLOCK_ID,
                "display_name": "退出登录（通用）",
                "description": "进入退出登录入口→确认弹窗→核验已退出。入口文案由应用覆盖给出。",
                "steps_json": list(_UNIFIED_LOGOUT_STEPS),
                "version": "v2",
            },
            {
                "block_id": WAKE_BLOCK_ID,
                "display_name": "唤醒与解锁（通用）",
                "description": "设备休眠/锁屏时唤醒并解除锁屏，供恢复规则复用。",
                "steps_json": list(_WAKE_STEPS),
                "version": "v1",
            },
            {
                "block_id": FOREGROUND_BLOCK_ID,
                "display_name": "拉回被测应用（通用）",
                "description": "前台不是被测应用时把目标包拉回前台。",
                "steps_json": list(_FOREGROUND_STEPS),
                "version": "v1",
            },
            {
                "block_id": RESTART_APP_BLOCK_ID,
                "display_name": "重启被测应用（通用）",
                "description": "关进程再启动，回到近似首次进入状态。",
                "steps_json": list(_RESTART_APP_STEPS),
                "version": "v1",
            },
            {
                "block_id": PRIVACY_ACCEPT_BLOCK_ID,
                "display_name": "首启协议弹窗：同意（通用）",
                "description": "同意用户协议/隐私政策→确认二次弹窗→核验弹窗关闭。由用例编译期选择。",
                "steps_json": list(_PRIVACY_ACCEPT_STEPS),
            },
            {
                "block_id": PRIVACY_DECLINE_BLOCK_ID,
                "display_name": "首启协议弹窗：拒绝（通用）",
                "description": "拒绝用户协议/隐私政策→确认二次弹窗→核验弹窗关闭（不要求进入主界面）。",
                "steps_json": list(_PRIVACY_DECLINE_STEPS),
            },
            {
                "block_id": SYSTEM_DIALOG_BLOCK_ID,
                "display_name": "系统弹窗：允许（通用）",
                "description": "系统权限挡屏→允许→确认→核验关闭。由用例编译期选择。",
                "steps_json": list(_SYSTEM_DIALOG_ALLOW_STEPS),
                "version": "v2",
            },
            {
                "block_id": SYSTEM_DIALOG_BLOCK_ID + ".deny",
                "display_name": "系统弹窗：拒绝（通用）",
                "description": "系统权限挡屏→拒绝→确认→核验关闭。",
                "steps_json": list(_SYSTEM_DIALOG_DENY_STEPS),
                "version": "v1",
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


# 调度依赖的全局块：缺任何一个都会让对应阶段「找不到钩子而走不通」。
# 启动时自检并大声报错，不做静默降级。
REQUIRED_GLOBAL_BLOCKS: tuple[str, ...] = (
    LOGIN_BLOCK_ID,
    LOGOUT_BLOCK_ID,
    PRIVACY_ACCEPT_BLOCK_ID,
    PRIVACY_DECLINE_BLOCK_ID,
    SYSTEM_DIALOG_BLOCK_ID,
    SYSTEM_DIALOG_BLOCK_ID + ".deny",
    WAKE_BLOCK_ID,
    FOREGROUND_BLOCK_ID,
    RESTART_APP_BLOCK_ID,
)


def verify_required_blocks() -> list[str]:
    """返回缺失的全局块 id；调用方负责报错。"""
    from mino_nexus.core.database import SessionLocal
    from mino_nexus.models.nav_flow_block_catalog import NavFlowBlockCatalog

    db = SessionLocal()
    try:
        rows = {str(r.block_id) for r in db.query(NavFlowBlockCatalog).all()}
    finally:
        db.close()
    return [b for b in REQUIRED_GLOBAL_BLOCKS if b not in rows]
