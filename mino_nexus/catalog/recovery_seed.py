"""L0 恢复规则种子（insert-only）+ 已有规则 payload 升级。"""
from __future__ import annotations

from typing import Any

_RECOVERY_PAYLOAD_V3: dict[str, Any] = {
    "priority": 100,
    "mode": "execute",
    "max_attempts": 2,
    "match": {
        "evidence_any": [
            {"screen_blocked": "yes"},
            {"capture_ok": "no"},
            {"capture_black": "yes", "screen_blocked": "yes"},
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
        "设备锁屏、休眠，或截图不可读且 probe 报告 blocked 时，先唤醒再解除锁屏。"
        "单独截图全黑但设备未锁屏 → 用 recover_screen_secure_or_no_capture，不要反复 wake。"
    ),
    "evidence_notes": [
        "screen_blocked=yes：probe 报告 asleep 或 keyguard",
        "capture_black=yes 且 screen_blocked=yes：锁屏黑图",
        "capture_ok=no：Scout 未返回可用截图",
    ],
}

_BRING_TARGET_APP_FOREGROUND: dict[str, Any] = {
    "priority": 90,
    "mode": "execute",
    "max_attempts": 2,
    "match": {
        "evidence": {
            "app_foreground": "no",
            "screen_blocked": "no",
        },
    },
    "actions": [
        {"capability": "launch_app", "params": {}},
    ],
    "verify": {
        "evidence": {
            "app_foreground": "yes",
        },
    },
    "prompt_snippet": (
        "当前前台不是被测 App（如 OAuth 后仍停在微信）。"
        "若本步要在被测 App 内操作，先 recover_bring_target_app_foreground 或 launch_app(目标包)。"
    ),
    "evidence_notes": [
        "app_foreground=no：probe 前台包 ≠ 目标包",
        "screen_blocked=no：排除锁屏误判",
    ],
}

_SCREEN_SECURE_OR_NO_CAPTURE: dict[str, Any] = {
    "priority": 110,
    "mode": "advise",
    "max_attempts": 1,
    "match": {
        "evidence": {
            "capture_black": "yes",
            "screen_blocked": "no",
        },
    },
    "actions": [],
    "prompt_snippet": (
        "截图全黑但设备未锁屏：可能是 FLAG_SECURE、安全键盘或密码界面，唤醒无效。"
        "先 press_key BACK 收起键盘或 wait_ms 后再 observe；不要重复 recover_screen_asleep_or_locked。"
        "仍黑则 hierarchy / signal_ask_human。实验机可在 Scout 侧切 ADB Keyboard 减挡截图。"
    ),
    "evidence_notes": [
        "capture_black=yes 且 screen_blocked=no：非锁屏黑图",
        "ime_shown=yes 时优先关键盘",
        "与 screen_asleep_or_locked 区分",
    ],
}

_RESTART_TARGET_APP: dict[str, Any] = {
    "priority": 50,
    "mode": "execute",
    "max_attempts": 1,
    "match": {
        "evidence": {
            "agent_invoke": "yes",
        },
    },
    "actions": [
        {"capability": "close_app", "params": {}},
        {"capability": "wait_ms", "params": {"duration_ms": 1500}},
        {"capability": "launch_app", "params": {}},
    ],
    "verify": {
        "evidence": {
            "app_foreground": "yes",
            "capture_ok": "yes",
        },
    },
    "prompt_snippet": (
        "迷路、页面栈太深、找不到登录入口或起点时：recover_restart_target_app。"
        "等价于关进程再冷启动目标包。登录模块 case 开环也可能已做过一次；仍迷路可再调。"
        "重启后先 observe 再操作，勿 launch_app/BACK 死循环。"
    ),
    "evidence_notes": [
        "主要由 agent 主动调用，不靠自动 match",
        "close_app → wait → launch_app",
    ],
}

READ_DEVICE_DATA_DESCRIPTION = (
    "只读 SIM/设备属性（getprop/dumpsys），不唤醒、不解锁。"
    "锁屏/黑屏请用 recover_screen_asleep_or_locked。"
    "Remote 不可用时该 plugin 自动消失。"
)

LEASE_ACCOUNT_DESCRIPTION = (
    "需要测试账号或手机号且尚未租到时调用；按 Requirement DSL / 用例前置从号池租号。"
    "只产出账号句柄，不代替登录步骤。"
)


def _bare_capture_black_branch(payload: dict[str, Any]) -> bool:
    branches = list((payload.get("match") or {}).get("evidence_any") or [])
    return {"capture_black": "yes"} in branches


def builtin_recovery_rules() -> list[dict[str, Any]]:
    return [
        {
            "kind": "recovery",
            "id": "screen_asleep_or_locked",
            "display_name": "锁屏 / 休眠 / 黑屏",
            "description": "设备 asleep、keyguard 显示，或截图不可读且 blocked 时唤醒并解锁。",
            "enabled": True,
            "lifecycle": "active",
            "platforms_json": ["android"],
            "sort_order": 10,
            "payload_json": dict(_RECOVERY_PAYLOAD_V3),
        },
        {
            "kind": "recovery",
            "id": "bring_target_app_foreground",
            "display_name": "拉回被测 App 前台",
            "description": "前台不是被测 App 时 launch 目标包拉回前台。",
            "enabled": True,
            "lifecycle": "active",
            "platforms_json": ["android"],
            "sort_order": 20,
            "payload_json": dict(_BRING_TARGET_APP_FOREGROUND),
        },
        {
            "kind": "recovery",
            "id": "screen_secure_or_no_capture",
            "display_name": "禁止截屏 / 非锁屏黑图",
            "description": "设备已 awake 且未锁屏，但截图全黑（FLAG_SECURE 等）。",
            "enabled": True,
            "lifecycle": "active",
            "platforms_json": ["android"],
            "sort_order": 15,
            "payload_json": dict(_SCREEN_SECURE_OR_NO_CAPTURE),
        },
        {
            "kind": "recovery",
            "id": "restart_target_app",
            "display_name": "冷启动被测 App",
            "description": "关进程再 launch，回到近似首次进入状态。迷路时由 agent 主动调用。",
            "enabled": True,
            "lifecycle": "active",
            "platforms_json": ["android"],
            "sort_order": 25,
            "payload_json": dict(_RESTART_TARGET_APP),
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


_SYSTEM_PERMISSION_WHILE_USING_DETERMINISTIC: dict[str, Any] = {
    "when": "顶层窗口是系统权限控制器，屏上出现「仅在使用该应用时允许 / 仅在使用中允许 / While using the app」",
    "mode": "deterministic",
    "match": {
        "evidence": {"app_foreground": "no"},
        "screen_text_any": [
            "允许",
            "仅在使用该应用时允许",
            "仅在使用中允许",
            "使用时允许",
            "使用应用时",
            "While using the app",
            "Allow while using the app",
        ],
        "top_window_pkg_prefix": [
            "com.android.permissioncontroller",
            "com.android.packageinstaller",
            "com.miui.securitycenter",
            "com.lbe.security.miui",
        ],
    },
    "actions": [
        {"capability": "tap_element", "params": {}, "target": {"text": "仅在使用中允许"}, "fallback_xy": []},
        {"capability": "wait_ms", "params": {"ms": 350}, "target": {}, "fallback_xy": []},
        {"capability": "tap_element", "params": {}, "target": {"text": "仅在使用该应用时允许"}, "fallback_xy": []},
        {"capability": "wait_ms", "params": {"ms": 350}, "target": {}, "fallback_xy": []},
        {"capability": "tap_element", "params": {}, "target": {"text": "使用时允许"}, "fallback_xy": []},
        {"capability": "wait_ms", "params": {"ms": 350}, "target": {}, "fallback_xy": []},
        {"capability": "tap_element", "params": {}, "target": {"text": "使用应用时"}, "fallback_xy": []},
        {"capability": "wait_ms", "params": {"ms": 350}, "target": {}, "fallback_xy": []},
        {"capability": "tap_element", "params": {}, "target": {"text": "允许"}, "fallback_xy": []},
        {"capability": "wait_ms", "params": {"ms": 350}, "target": {}, "fallback_xy": []},
        {"capability": "tap_element", "params": {}, "target": {"text": "While using the app"}, "fallback_xy": []},
        {"capability": "wait_ms", "params": {"ms": 400}, "target": {}, "fallback_xy": []},
        {"capability": "tap_element", "params": {}, "target": {"text": "Allow while using the app"}, "fallback_xy": []},
        {"capability": "wait_ms", "params": {"ms": 600}, "target": {}, "fallback_xy": []},
    ],
    "verify": {
        "evidence": {"app_foreground": "yes"},
        "screen_text_any": [],
        "top_window_pkg_prefix": [],
    },
    "forbid": {"text_any": ["拒绝", "不允许", "禁止", "Deny"]},
    "prompt_snippet": "",
    "priority": 65,
    "max_attempts": 3,
    "evidence_notes": [
        "MIUI/HyperOS 常见文案「仅在使用中允许」与 AOSP「仅在使用该应用时允许」并存，actions 须都覆盖。",
        "apply_rule 入口优先 system_dialog_recovery.try_unified_system_permission_recovery（hierarchy 点 allow/grant 文案），本 actions 为 fallback；禁止点拒绝/不允许。",
        "连续失败后 agent 应改用 tap_element 直点或 signal_give_up（limit_recovery_retry 守卫）。",
    ],
}

_SYSTEM_MEDIA_PICKER_DISMISS: dict[str, Any] = {
    "when": "系统相册/文件选择器挡在前台，需返回被测 App",
    "mode": "deterministic",
    "match": {
        "evidence": {"app_foreground": "no", "screen_blocked": "no"},
        "top_window_pkg_prefix": [
            "com.google.android.apps.photos",
            "com.android.documentsui",
            "com.android.providers.media",
            "com.miui.gallery",
            "com.coloros.gallery3d",
            "com.huawei.photos",
        ],
    },
    "actions": [
        {"capability": "press_key", "params": {"key": "BACK"}, "target": {}, "fallback_xy": []},
        {"capability": "wait_ms", "params": {"ms": 400}, "target": {}, "fallback_xy": []},
        {"capability": "tap_element", "params": {}, "target": {"text": "取消"}, "fallback_xy": []},
        {"capability": "tap_element", "params": {}, "target": {"text": "关闭"}, "fallback_xy": []},
        {"capability": "tap_element", "params": {}, "target": {"text": "Cancel"}, "fallback_xy": []},
        {"capability": "press_key", "params": {"key": "BACK"}, "target": {}, "fallback_xy": []},
        {"capability": "wait_ms", "params": {"ms": 500}, "target": {}, "fallback_xy": []},
    ],
    "verify": {"evidence": {"app_foreground": "yes"}},
    "forbid": {},
    "prompt_snippet": "",
    "priority": 60,
    "max_attempts": 3,
    "evidence_notes": [
        "系统相册/文档选择器不进 App 导航图；优先 BACK，再点取消/关闭。",
    ],
}











CHECK_RUN_ENV_DESCRIPTION = (
    "确认批次 env/platform/otp；前置不要调用，环境由 env_profile 确定。"
)





FSM_NAVIGATE_DESCRIPTION = (
    "按已发布 NavFSM 路线图规划最短路：传入当前屏与目标屏（state_id 或 Tab 文案如「首页」），"
    "返回本步应走的 nav 边序列。跨 Tab 迷路时先恢复设备态，再逐步执行返回的边。"
)

_FSM_NAVIGATE_PAYLOAD: dict[str, Any] = {
    "caller": "nexus_fsm_navigate",
    "mode": "advise",
}





GET_OTP_DESCRIPTION = (
    "从已租账号 otp 或项目环境 otp.fixed 取验证码，不点设备。"
    "取到后用 input_text 填入。未配置则失败，勿盲填知识库固定码。"
)

_GET_OTP_PAYLOAD: dict[str, Any] = {
    "caller": "nexus_get_otp",
}

ACCEPT_LEGAL_CONSENT_DESCRIPTION = (
    "勾选长文案左侧的小可勾选控件。"
    "输入框聚焦时先 BACK 再点控件。不要点长文案本身。"
)

DISMISS_IME_DESCRIPTION = (
    "有聚焦输入框时 BACK 收起输入法。不要在登录弹层上盲目 BACK。"
)

REQUEST_SMS_CODE_DESCRIPTION = (
    "手机号输入框已填 11 位后，点同行右侧可点的短文案控件以请求短信码。"
    "聚焦输入框时先 BACK。然后再 get_otp / input_text 填验证码。"
)

_STRUCTURAL_RECOVERY_PAYLOAD: dict[str, Any] = {
    "caller": "nexus_local",
    "mode": "advise",
}











_TARGET_APP_CAPS = (
    "launch_app",
    "close_app",
    "kill_app",
    "get_app_version",
    "clear_app_cache",
    "system_pkg_clear",
    "open_url",
    "open_app",
)








def register_current_local_capabilities() -> None:
    """Synchronize current local declarations, retaining disabled state and audit rows."""
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.catalog import CatalogEntry
    from mino_nexus.catalog.tool_schema import PARAM_DEFAULTS, schema_to_param_rows
    from mino_nexus.catalog.system_registration import sync_system_capability
    specs = [("get_otp", "generic", "取验证码", GET_OTP_DESCRIPTION, ["mac", "win", "android", "web"]),
             ("dismiss_ime", "recovery", "收起输入法", DISMISS_IME_DESCRIPTION, ["android"])]
    with session_scope() as db:
        for cid, kind, title, description, platforms in specs:
            sync_system_capability(db, cid=cid, kind=kind, title=title,
                description=description, platforms=platforms,
                fields={"event_kind": cid, "caller": "program", "phases": ["prep", "do"],
                        "observe": "program", "implementations": [],
                        "channel_contract_version": 1,
                        "params": schema_to_param_rows(PARAM_DEFAULTS[cid])})
        for cid, title, payload in [
            ("system_permission_dialog_while_using_deterministic", "系统权限弹窗", _SYSTEM_PERMISSION_WHILE_USING_DETERMINISTIC),
            ("system_media_picker_dismiss", "关闭系统相册/选择器", _SYSTEM_MEDIA_PICKER_DISMISS)]:
            if db.query(CatalogEntry).filter(CatalogEntry.id==cid).first(): continue
            db.add(CatalogEntry(id=cid,kind="recovery",display_name=title,enabled=True,lifecycle="active",
                               platforms_json=["android"],payload_json=dict(payload)))
