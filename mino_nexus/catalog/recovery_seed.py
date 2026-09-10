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
    "需要测试账号或手机号且尚未租到时调用；按用例场景从账号池租号。"
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
            "platforms_json": ["android", "ios"],
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
            "platforms_json": ["android", "ios"],
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
            "platforms_json": ["android", "ios"],
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


def upgrade_recovery_rules() -> int:
    """已有库：screen_asleep v3 match + 补新 L0 规则 + read_device_data 摘要。"""
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.catalog import CatalogEntry

    updated = 0
    with session_scope() as db:
        row = (
            db.query(CatalogEntry)
            .filter(CatalogEntry.kind == "recovery", CatalogEntry.id == "screen_asleep_or_locked")
            .first()
        )
        if row:
            payload = dict(row.payload_json or {})
            if _bare_capture_black_branch(payload) or not payload.get("match", {}).get("evidence_any"):
                row.display_name = "锁屏 / 休眠 / 黑屏"
                row.description = builtin_recovery_rules()[0]["description"]
                row.payload_json = dict(_RECOVERY_PAYLOAD_V3)
                updated += 1

        for spec in builtin_recovery_rules()[1:]:
            cid = str(spec["id"])
            if db.query(CatalogEntry).filter(CatalogEntry.kind == "recovery", CatalogEntry.id == cid).first():
                continue
            db.add(
                CatalogEntry(
                    kind=str(spec["kind"]),
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
            updated += 1

        prep = (
            db.query(CatalogEntry)
            .filter(CatalogEntry.kind == "prep", CatalogEntry.id == "read_device_data")
            .first()
        )
        if prep and str(prep.description or "").strip() != READ_DEVICE_DATA_DESCRIPTION:
            prep.description = READ_DEVICE_DATA_DESCRIPTION
            updated += 1

        secure = (
            db.query(CatalogEntry)
            .filter(CatalogEntry.kind == "recovery", CatalogEntry.id == "screen_secure_or_no_capture")
            .first()
        )
        want_secure = dict(_SCREEN_SECURE_OR_NO_CAPTURE)
        if secure and dict(secure.payload_json or {}) != want_secure:
            secure.payload_json = want_secure
            secure.description = builtin_recovery_rules()[2]["description"]
            updated += 1

        restart = (
            db.query(CatalogEntry)
            .filter(CatalogEntry.kind == "recovery", CatalogEntry.id == "restart_target_app")
            .first()
        )
        want_restart = dict(_RESTART_TARGET_APP)
        if restart and dict(restart.payload_json or {}) != want_restart:
            restart.payload_json = want_restart
            updated += 1

    if updated:
        from mino_nexus.catalog.loader import force_reload

        force_reload()
    return updated


CHECK_RUN_ENV_DESCRIPTION = (
    "确认本任务运行环境（env/platform/otp 通道），摘要形如 env=test; platform=android; otp_via=get_otp。"
    "每任务仅需一次；history 已有 env= 则 signal_done。"
)


def upgrade_check_run_env_capability() -> int:
    """补 prep 能力 check_run_env（Nexus 本地执行）。"""
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.catalog import CatalogEntry

    updated = 0
    with session_scope() as db:
        row = (
            db.query(CatalogEntry)
            .filter(CatalogEntry.kind == "prep", CatalogEntry.id == "check_run_env")
            .first()
        )
        if row:
            if str(row.description or "").strip() != CHECK_RUN_ENV_DESCRIPTION:
                row.description = CHECK_RUN_ENV_DESCRIPTION
                updated = 1
            return updated
        db.add(
            CatalogEntry(
                kind="prep",
                id="check_run_env",
                display_name="确认运行环境",
                description=CHECK_RUN_ENV_DESCRIPTION,
                enabled=True,
                lifecycle="active",
                platforms_json=["android", "ios", "web"],
                payload_json={},
                sort_order=5,
            )
        )
        updated = 1
    if updated:
        from mino_nexus.catalog.loader import force_reload

        force_reload()
    return updated


def upgrade_account_capabilities() -> int:
    """更新 recovery 原子能力 lease_account 的菜单摘要（旧文案写「开跑前」易误导）。"""
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.catalog import CatalogEntry

    updated = 0
    with session_scope() as db:
        row = (
            db.query(CatalogEntry)
            .filter(CatalogEntry.kind == "recovery", CatalogEntry.id == "lease_account")
            .first()
        )
        if not row:
            return 0
        if str(row.description or "").strip() == LEASE_ACCOUNT_DESCRIPTION:
            return 0
        row.description = LEASE_ACCOUNT_DESCRIPTION
        updated = 1
    if updated:
        from mino_nexus.catalog.loader import force_reload

        force_reload()
    return updated
