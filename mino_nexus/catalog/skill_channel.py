"""技能 / 基座分界，以及渠道上做不到的步骤。"""
from __future__ import annotations

from typing import Any

from mino_nexus.catalog.exec_classes import BASE_KIND

from mino_nexus.contract.business_event_contract import RETIRED as RETIRED_BUSINESS_EVENTS

V5_EXCLUDED_IDS = frozenset({"human_input_text", "persona_subtask", "hitl", "ai_persona", "signal_ask_human"})

# 平台入口和抽象提供面。留在目录里供 Console 查看，不进模型菜单。
BASE_ENTRY_IDS = frozenset({
    "adb",
    "ios_wda",
    "playwright",
    "remote",
    "ui_native_input",
    "ui_input_text",
    "ui_screenshot",
    "ui_stream",
    "system_shell",
    "system_pkg_install",
    "app_force_stop",
    "app_launch_native",
    "key_event",
    "clipboard_set",
    "vision_assert",
    "vision_diff",
    "vision_locate",
    "vision_readiness",
    "network_http",
    "network_mock",
    "read_system_data",
    "ai_persona",
    "vlm",
})

# 程序自己派发，规划追加时允许，不要求出现在 case 菜单。
PROGRAM_CAPS = frozenset({
    "tap_target",
    "input_target",
    "read_web_target_facts",
    "lease_account",
    "get_otp",
    "get_phone",
    "release_account",
    "confirm_login_state",
    "operation.check_box",
    "read_web_auth",
    "clear_app_cache",
    "launch_app",
    "wake_screen",
    "dismiss_keyguard",
    "reload_page",
    "plugin.gmail.fetch_otp",
})

CASE_GENERIC_IDS = frozenset({
    "lease_account",
    "get_otp",
    "get_phone",
    "release_account",
    "clear_app_cache",
    "get_app_version",
    "get_foreground_app",
    "kill_app",
    "close_app",
    "launch_app",
    "install_apk",
    "read_device_data",
    "tap_element",
    "input_text",
    "swipe_direction",
    "swipe_element_to_element",
    "long_press_element",
    "multi_tap",
    "press_key",
    "wait_ms",
    "wait_screen_ready",
    "set_clipboard",
    "operation.check_box",
    "dismiss_ime",
    "assert_visual",
})

SYSTEM_ONLY_IDS = frozenset({
    "read_web_auth",
    "wake_screen",
    "dismiss_keyguard",
    "probe_device_state",
})

# 程序直接派到设备、不进用例菜单的系统动作。启动时按这份声明入库。
SYSTEM_DEVICE_IDS = frozenset({"wake_screen", "dismiss_keyguard", "probe_device_state"})








def system_device_dispatch(cap_id: str) -> bool:
    """唤醒、解锁、取证由程序直接派，不经过用例菜单和绑定菜单。"""
    return canonical_cap(cap_id) in SYSTEM_DEVICE_IDS


def is_program_caller(cap_id: str) -> bool:
    from mino_nexus.catalog.registry import get_capability
    row = get_capability(canonical_cap(cap_id))
    return row is not None and row.caller == "program"


def capability_enabled(cap_id: str) -> bool:
    """未启用或已废弃的技能，模型和程序都不派。目录没有这一行时不拦（逻辑块特例仍走自己的函数）。"""
    cap = canonical_cap(cap_id)
    if not cap or cap.startswith("guard.") or cap in V5_EXCLUDED_IDS or cap in RETIRED_BUSINESS_EVENTS:
        return False
    from mino_nexus.catalog.registry import get_capability

    row = get_capability(cap)
    if row is None:
        return False
    if getattr(row, "enabled", True) is False:
        return False
    return str(getattr(row, "lifecycle", "")) == "active"


def capability_ui_coverable(cap_id: str) -> bool:
    """当前产品能否把这条事件计入 UI 自动化覆盖。目录没有这一行时不拦。"""
    cap = canonical_cap(cap_id)
    if not cap:
        return False
    from mino_nexus.catalog.registry import get_capability

    row = get_capability(cap)
    if row is None:
        return False
    return getattr(row, "ui_coverable", True) is not False


_OBSERVE_VALUES = frozenset({"exec", "visual_each_run", "program"})


def capability_observe(cap_id: str) -> str:
    """编译 observe：目录 payload.observe，否则 program caller → program，其余 exec。"""
    cap = canonical_cap(cap_id)
    if not cap:
        return "exec"
    try:
        from mino_nexus.catalog.registry import get_capability

        row = get_capability(cap)
    except Exception:  # noqa: BLE001
        row = None
    if row is not None:
        obs = str(getattr(row, "observe", "") or "").strip().lower()
        if obs in _OBSERVE_VALUES:
            return obs
    if is_program_caller(cap) or cap in ("wait_ms", "wait_screen_ready"):
        return "program"
    return "exec"


def capability_dispatch_ok(cap_id: str) -> bool:
    """派发前只问：能力是否启用、生命周期是否 active。

    ``ui_coverable`` 是覆盖统计字段，不是权限：把它拿来拦派发会让「手能点、目录也声明了
    实现」的合法事件永远发不出去（规划 §4 / §6）。平台是否支持由 binding / EXECUTE 的
    platform 合同判断，不在这一层重复收窄。
    """
    cap = canonical_cap(cap_id)
    if cap.startswith("guard.") or cap in V5_EXCLUDED_IDS or cap in RETIRED_BUSINESS_EVENTS:
        return False
    if str(cap_id or "").startswith(("human_", "signal_", "recover_")) or str(cap_id or "") == "run_flow_block":
        return True
    return capability_enabled(cap_id)


def phase_allows(cap_id: str, phase: str) -> bool:
    """阶段只缩 prompt，不进派发权限（规划 §3）。

    保留函数是为了兼容旧调用点；``prep`` / ``do`` / ``check`` 不再作为准入门。
    模型菜单仍用 ``menu_caller_ok`` 按阶段裁剪，程序派发只看平台能力与应用声明。
    """
    del cap_id, phase
    return True


def menu_caller_ok(cap: Any, phase: str) -> bool:
    return cap.caller == "case" and phase in cap.phases and cap.lifecycle == "active" and cap.enabled


def canonical_cap(cap_id: str) -> str:
    """IDs are exact; former aliases are not rewritten."""
    return str(cap_id or "").strip()


def normalize_platform(platform: str) -> str:
    from mino_nexus.core.platform_contract import target_platform
    return target_platform(platform)


def channel_platforms_for(cap_id: str) -> frozenset[str]:
    from mino_nexus.catalog.registry import get_capability
    from mino_nexus.core.platform_contract import platform_set
    row = get_capability(canonical_cap(cap_id))
    return frozenset(platform_set(row.platforms)) if row is not None else frozenset()


def channel_supports(cap_id: str, platform: str) -> bool:
    return normalize_platform(platform) in channel_platforms_for(cap_id)


def milestone_cap_ids(row: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for key in ("hook_cap", "device_cap", "cap"):
        val = canonical_cap(str(row.get(key) or ""))
        if val and val not in out:
            out.append(val)
    listed = row.get("exec_caps")
    if isinstance(listed, list):
        for item in listed:
            val = canonical_cap(str(item or ""))
            if val and val not in out:
                out.append(val)
    return out


def apply_channel_skip(row: dict[str, Any], platform: str) -> bool:
    """渠道不支持：可选节点 skipped，必填节点失败并保持诊断，不伪造完成。"""
    if row.get('requirement') == 'conditional':
        return False  # Applicability is observed by the program before capability admission.
    restored = False
    if row.get("status") == "skipped" and row.get("skip_reason") == "channel_absent" and not row.get("optional"):
        row["status"] = "pending"
        row.pop("skip_reason", None)
        row.pop("skipped_by", None)
        restored = True
    if str(row.get("status") or "").strip().lower() in ("pass", "failed", "skipped"):
        return False
    got = normalize_platform(platform)
    declared = row.get("platforms")
    unsupported = bool(isinstance(declared, list) and declared and got
                       and got not in {normalize_platform(str(x)) for x in declared})
    caps = milestone_cap_ids(row)
    if not unsupported and (not caps or any(channel_supports(cap, platform) for cap in caps)):
        return restored
    row["status"] = "skipped" if row.get("optional") else "failed"
    row["skip_reason" if row.get("optional") else "failure_reason"] = "channel_absent"
    row["reason_code"] = "channel_absent"
    row["layer"] = "dispatch"
    row["attribution_class"] = "A"
    row["attribution_label"] = "平台缺口"
    row["evidence"] = "当前渠道缺少必需能力，请在 Workbench 核对目录版本与平台" if not row.get("optional") else "channel_absent"
    row["skipped_by" if row.get("optional") else "failed_by"] = "channel"
    return True



def rewrite_milestone_aliases(raw: dict[str, Any]) -> dict[str, Any]:
    row = dict(raw)
    for key in ("hook_cap", "device_cap", "cap"):
        if row.get(key):
            row[key] = canonical_cap(str(row.get(key) or ""))
    listed = row.get("exec_caps")
    if isinstance(listed, list):
        row["exec_caps"] = [canonical_cap(str(x or "")) for x in listed if str(x or "").strip()]
    return row


def milestone_caps_registered(
    raw: dict[str, Any],
    menu_ids: set[str],
    *,
    allow_program: bool = False,
    platform: str = "",
    phase: str = "",
) -> bool:
    allowed = set(menu_ids)

    def _cap_ok(cap: str) -> bool:
        if cap in allowed:
            return True
        from mino_nexus.catalog.exec_classes import LOCAL_EXECUTOR_IDS
        from mino_nexus.contract.executor_sets import LOCAL_EXECUTORS

        # 本地 executor（vlm/internal/hitl/ai_persona）不经 Scout，目录里本来就不声明
        # implementations。用"有没有 Scout 实现"判它会把人挡住：confirm_login_state
        # 就是这么被永久 blocked 的。
        local = cap in LOCAL_EXECUTORS or cap in LOCAL_EXECUTOR_IDS or cap.startswith(("recover_", "signal_"))
        if local:
            return capability_dispatch_ok(cap) and channel_supports(cap, platform)
        from mino_nexus.catalog.registry import get_capability

        row = get_capability(cap)
        impls = list(getattr(row, "implementations", None) or []) if row is not None else []
        if not impls or not capability_dispatch_ok(cap):
            return False
        if is_program_caller(cap) or system_device_dispatch(cap):
            if not allow_program:
                return False
        # 菜单只缩 prompt。能力目录有实现、平台也声明支持，就不该因为「没进当前阶段
        # 菜单」被判成整案不可执行（规划 §3/§4）。
        return channel_supports(cap, platform)

    primary: list[str] = []
    for key in ("hook_cap", "device_cap", "cap"):
        val = canonical_cap(str(raw.get(key) or ""))
        if val and val not in primary:
            primary.append(val)
    extras = [
        canonical_cap(str(item or ""))
        for item in (raw.get("exec_caps") or [])
        if str(item or "").strip()
    ] if isinstance(raw.get("exec_caps"), list) else []
    # nav_target 是导航策略名，不是目录能力。这一步能不能做，看 exec_caps。
    if "nav_target" in primary and (
        str(raw.get("tier") or "") == "policy" or raw.get("nav_target") or extras
    ):
        primary = [cap for cap in primary if cap != "nav_target"]
        if not extras:
            return False
    if primary and not all(_cap_ok(cap) for cap in primary):
        return False
    # exec_caps 是这一步可选的动作，渠道上有其中一个即可，不要因为 press_key 仅安卓就把整步丢掉。
    if extras and not any(_cap_ok(cap) for cap in extras):
        return False
    return True





def _ensure_core_execution_rows(db: Any) -> int:
    """Register missing built-in primitives used by ordinary compiled plans.

    Existing rows, including disabled rows and custom implementations, remain
    authoritative. Declaring an implementation does not bypass Scout admission.

    这份声明必须覆盖"编译产物里可能成为必填项"的原语：准入按目录判
    `catalog_capability_missing`，漏一个就等于该用例永远跑不了。缺 `input_text`
    会让所有要打字的用例在 72ms 内 `execution_started: no` 结束。
    """
    from mino_nexus.models.catalog import CatalogEntry
    from mino_nexus.catalog.tool_schema import PARAM_DEFAULTS, schema_to_param_rows

    specs = (
        ("tap_element", "点击元素", "case", "adb", "ui_native_input", ["android"]),
        ("press_key", "按键", "case", "adb", "key_event", ["android"]),
        ("launch_app", "启动被测应用", "program", "adb", "app_launch_native", ["android"]),
        ("lease_account", "筛选并租用账号", "program", "internal", "", ["mac", "win", "android", "web"]),
        # 以下原语 Scout adb 已原生实现（按 capability_id 分派，low_level 留空），
        # 只是 Nexus 侧一直没登记，于是编译能过、准入必挂。
        ("input_text", "输入文本", "case", "adb", "ui_input_text", ["android"]),
        ("multi_tap", "连续点击", "case", "adb", "ui_native_input", ["android"]),
        ("long_press_element", "长按元素", "case", "adb", "ui_native_input", ["android"]),
        ("swipe_direction", "按方向滑动", "case", "adb", "ui_native_input", ["android"]),
        ("swipe_element_to_element", "两点滑动", "case", "adb", "ui_native_input", ["android"]),
        ("kill_app", "结束被测应用", "case", "adb", "app_force_stop", ["android"]),
        # 程序派发但从不进模型菜单的观测/管理原语。缺行时 add() 对 optional 静默丢弃，
        # prep 程序照旧派发，最后由阶段门报成"超出阶段"——真正的病是目录缺行。
        ("get_app_version", "读取应用版本", "system", "adb", "read_system_data", ["android"]),
        ("close_app", "关闭被测应用", "system", "adb", "app_force_stop", ["android"]),
    )
    added = 0
    for eid, title, caller, executor, abstract, platforms in specs:
        if db.query(CatalogEntry).filter(CatalogEntry.id == eid).first() is not None:
            continue
        implementation = {
            "id": "builtin.v1:" + eid, "executor": executor,
            "runs_on": "nexus" if executor == "internal" else "scout",
            "platforms": list(platforms), "low_level": {},
            "params_contract": {"version": 1}, "result_contract": {"version": 1},
            "device_hint_template": {"execution_mode": "agent"},
        }
        if abstract:
            implementation["requires_caps"] = [abstract]
        db.add(CatalogEntry(
            kind="generic", id=eid, display_name=title,
            enabled=True, lifecycle="active", platforms_json=list(platforms),
            visible_to_json=["case", "system"] if caller == "case" else ["system"],
            payload_json={"event_kind": eid, "caller": caller, "phases": ["prep", "do"],
                "channel_contract_version": 1, "implementations": [implementation],
                "params": schema_to_param_rows(PARAM_DEFAULTS.get(eid))},
        ))
        added += 1
    if added:
        db.flush()
    return added


def _system_device_implementation(eid: str, platforms: list[str], low_level: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "system.v1:" + eid,
        "executor": "adb",
        "runs_on": "scout",
        "platforms": list(platforms),
        "low_level": dict(low_level),
        "params_contract": {"version": 1},
        "result_contract": {"version": 1},
        "device_hint_template": {"execution_mode": "agent"},
    }


def _ensure_system_device_rows(db: Any) -> int:
    """唤醒、解锁、取证必须有实现。缺行就补上，已有行补上缺失的实现。"""
    from mino_nexus.models.catalog import CatalogEntry

    specs = (
        (
            "wake_screen",
            "唤醒屏幕",
            "程序点亮屏幕。不进模型菜单。",
            ["android"],
            {"kind": "shell", "shell": "input keyevent KEYCODE_WAKEUP", "summary": "唤醒屏幕"},
        ),
        (
            "dismiss_keyguard",
            "解除锁屏",
            "程序收起锁屏界面。不进模型菜单。",
            ["android"],
            {"kind": "shell", "shell": "wm dismiss-keyguard", "summary": "解除锁屏"},
        ),
        (
            "probe_device_state",
            "读取屏幕状态",
            "程序读取电源、锁屏和输入法。不进模型菜单。",
            ["android"],
            {
                "kind": "shell_batch",
                "commands": [
                    {"name": "power", "shell": "dumpsys power", "parse": "keyvalue_lines"},
                    {"name": "keyguard", "shell": "dumpsys window", "parse": "keyvalue_lines"},
                    {"name": "ime", "shell": "dumpsys input_method", "parse": "keyvalue_lines"},
                ],
            },
        ),
        (
            "get_foreground_app",
            "读取前台应用",
            "程序读取当前前台应用，用于恢复与取证。不进模型菜单。",
            ["android", "web", "mac", "win"],
            {"kind": "foreground_app", "summary": "读取前台应用"},
        ),
    )
    n = 0
    for eid, title, desc, platforms, low_level in specs:
        implementation = _system_device_implementation(eid, platforms, low_level)
        fields = {
            "caller": "system",
            "phases": ["prep", "do", "check", "cleanup"], "params": [], "observe": "program",
            "event_kind": eid,
            "channel_contract_version": 1,
            "implementations": [implementation],
        }
        row = db.query(CatalogEntry).filter(CatalogEntry.id == eid).first()
        if row is None:
            db.add(CatalogEntry(
                kind="generic", id=eid, display_name=title, description=desc,
                enabled=True, lifecycle="active", platforms_json=list(platforms),
                visible_to_json=["system"], payload_json=fields,
            ))
            n += 1
            continue
        payload = dict(row.payload_json or {})
        impls = list(payload.get("implementations") or [])
        if not impls:
            payload["implementations"] = [implementation]
        payload.update({k: v for k, v in fields.items() if k != "implementations"})
        if impls:
            payload["implementations"] = impls
        changed = dict(row.payload_json or {}) != payload or list(row.platforms_json or []) != list(platforms)
        if list(row.platforms_json or []) != list(platforms):
            row.platforms_json = list(platforms)
        if list(row.visible_to_json or []) != ["system"]:
            row.visible_to_json = ["system"]
            changed = True
        if changed:
            row.payload_json = payload
            n += 1
    if n:
        db.flush()
    return n


def _ensure_program_rows(db: Any) -> int:
    """程序能力在启动时注册及同步版本合同，不依赖 Workbench 手工升级。"""
    from mino_nexus.models.catalog import CatalogEntry

    specs = (
        (
            "confirm_login_state",
            "确认登录态",
            "程序判断是否已离开登录表单。不进模型菜单。",
            ["mac", "win", "android", "web"],
        ),
        ("operation.check_box", "设置复选框状态", "指定准确目标和 checked 状态；最多点击一次并复核，不含登录业务。", ["mac", "win", "android", "web"]),
    )
    specs += (
        ("clear_app_cache", "重置应用数据（清数据+缓存）", "pm clear 清数据与缓存，等价于退出登录/回到首启初态；执行后登录态、形象、权限授权都必须重新观察。", ["android"]),
        ("reload_page", "刷新当前页面", "程序刷新本次运行的 Web 页面。不进模型菜单。", ["web"]),
        ("plugin.gmail.fetch_otp", "节点 Gmail 取码", "在同一绑定节点读取验证码，不携带邮箱密码。", ["mac", "win", "android", "web"]),
    )
    n = _ensure_core_execution_rows(db)
    n += _ensure_system_device_rows(db)
    for eid, title, desc, platforms in specs:
        row = db.query(CatalogEntry).filter(CatalogEntry.id == eid).first()
        fields = {"caller": "program", "phases": ["prep", "do"], "observe": "program",
                  "params": [], "implementations": [],
                  "event_kind": eid, "channel_contract_version": 3}
        if eid in {"clear_app_cache", "reload_page", "plugin.gmail.fetch_otp"}:
            executor = {"clear_app_cache": "adb", "reload_page": "playwright", "plugin.gmail.fetch_otp": "host_plugin"}[eid]
            implementation = {"id": "program.v1:" + eid, "executor": executor,
                "runs_on": "scout", "platforms": list(platforms), "low_level": {},
                "params_contract": {"version": 1}, "result_contract": {"version": 1},
                "device_hint_template": {"execution_mode": "agent"}}
            if eid == "clear_app_cache":
                fields["channel_contract_version"] = 4
                # 2026-10-22：能力就是 pm clear，清数据与缓存；旧的清缓存别名已删除。
                implementation["low_level"] = {"kind": "shell",
                    "shell": "pm clear --user current {package}",
                    "summary": "清除目标应用数据与缓存，应用回到未登录初态"}
            if eid == "reload_page":
                implementation["requires_caps"] = ["ui_native_input"]
            fields["implementations"] = [implementation]
        if row is None:
            db.add(CatalogEntry(
                kind="generic", id=eid, display_name=title, description=desc,
                enabled=True, lifecycle="active", platforms_json=list(platforms),
                visible_to_json=["system"], payload_json=fields,
            ))
            n += 1
            continue
        payload = {**dict(row.payload_json or {}), **fields}
        changed = False
        if list(row.platforms_json or []) != list(platforms):
            row.platforms_json = list(platforms)
            changed = True
        if dict(row.payload_json or {}) != payload:
            row.payload_json = payload
            changed = True
        if list(row.visible_to_json or []) != ["system"]:
            row.visible_to_json = ["system"]
            changed = True
        if changed:
            n += 1
    if n:
        db.flush()
    return n














def _ensure_web_playwright_rows(db: Any) -> int:
    """为 Web 补齐 tap_element / launch_app 的 Playwright 实现。

    Scout 的 playwright_executor 已经原生支持 tap_element 和 launch_app；但目录长期只
    声明 android/adb，导致 Web 编译产物在准入阶段报 event_platform_unsupported。
    这里只补 Web 平台和 playwright 实现，不改 Android 现有实现，也不降低 Scout 准入。
    """
    from mino_nexus.models.catalog import CatalogEntry

    specs = (
        ("tap_element", "点击元素（Web）", "case", ["prep", "do"]),
        ("launch_app", "打开被测 Web 地址", "program", ["prep"]),
    )
    changed = 0
    for eid, title, caller, phases in specs:
        row = db.query(CatalogEntry).filter(CatalogEntry.id == eid).first()
        if row is None:
            continue
        platforms = list(row.platforms_json or [])
        payload = dict(row.payload_json or {})
        implementations = list(payload.get("implementations") or [])
        impl_id = "web.playwright.v1"
        if not any(isinstance(item, dict) and item.get("id") == impl_id for item in implementations):
            implementations.append({
                "id": impl_id,
                "executor": "playwright",
                "runs_on": "scout",
                "platforms": ["web"],
                "needs_vlm": False,
                "low_level": {},
                "requires_caps": [],
                "params_contract": {"version": 1},
                "result_contract": {"version": 1},
                "device_hint_template": {"execution_mode": "agent"},
            })
            payload["implementations"] = implementations
        if "web" not in platforms:
            platforms.append("web")
        if list(row.platforms_json or []) != platforms or dict(row.payload_json or {}) != payload:
            row.platforms_json = platforms
            row.payload_json = payload
            changed += 1
    return changed


def register_program_capabilities() -> dict[str, Any]:
    """启动同一注册器；仅处理已声明的程序能力，不修改业务目录。"""
    from mino_nexus.catalog import registry as catalog
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.catalog import CatalogEntry
    with session_scope() as db:
        changed = _ensure_program_rows(db)
        changed += _ensure_web_playwright_rows(db)
        registered = [{"id": row.id, "platforms": list(row.platforms_json or []),
                       "channel_contract_version": (row.payload_json or {}).get("channel_contract_version")}
                      for row in db.query(CatalogEntry).filter(CatalogEntry.id.in_(
                          ("confirm_login_state", "operation.check_box", "clear_app_cache", "reload_page", "plugin.gmail.fetch_otp",
                           "tap_element", "press_key", "launch_app", "lease_account",
                           "wake_screen", "dismiss_keyguard", "probe_device_state", "get_foreground_app"))).order_by(CatalogEntry.id).all()]
    catalog.reload()
    return {"changed": changed, "registered": registered}
