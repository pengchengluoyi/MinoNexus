"""技能 / 基座分界，以及渠道上做不到的步骤。"""
from __future__ import annotations

from typing import Any

from mino_nexus.catalog.exec_classes import BASE_KIND

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
    "system_pkg_clear",
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

# 同目的的第二种写法。对外 id 见 CAP_ALIASES。
CAP_ALIASES = {
    "ui_input_text": "input_text",
    "key_event": "press_key",
    "clipboard_set": "set_clipboard",
    "system_pkg_clear": "clear_app_cache",
    "app_force_stop": "kill_app",
    "app_launch_native": "launch_app",
    "vision_assert": "assert_visual",
}

# 目录里没有、但逻辑块会写的渠道技能。
CHANNEL_PLATFORMS = {
    "clear_app_cache": frozenset({"android"}),
    "confirm_login_state": frozenset({"web"}),
}

# 程序自己派发，规划追加时允许，不要求出现在 case 菜单。
PROGRAM_CAPS = frozenset({
    "lease_account",
    "relogin",
    "get_otp",
    "get_phone",
    "release_account",
    "confirm_login_state",
    "accept_legal_consent",
    "read_web_auth",
    "clear_app_cache",
    "launch_app",
    "open_url",
    "open_app",
    "wake_screen",
    "dismiss_keyguard",
    "fsm_navigate",
})

CASE_GENERIC_IDS = frozenset({
    "lease_account",
    "get_otp",
    "get_phone",
    "release_account",
    "relogin",
    "clear_app_cache",
    "get_app_version",
    "get_foreground_app",
    "kill_app",
    "close_app",
    "launch_app",
    "install_apk",
    "read_device_data",
    "check_run_env",
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
    "accept_legal_consent",
    "request_sms_code",
    "dismiss_ime",
    "assert_visual",
})

SYSTEM_ONLY_IDS = frozenset({
    "read_web_auth",
    "wake_screen",
    "dismiss_keyguard",
    "probe_device_state",
})


def derive_caller(entry_id: str, *, kind: str = "", visible_to: list | None = None) -> str:
    """目录还没写 caller 时，用 kind / visible_to / 程序白名单推出。"""
    eid = canonical_cap(entry_id)
    if str(kind or "") == "base" or eid in BASE_ENTRY_IDS:
        return "base"
    vis = {str(x).strip().lower() for x in (visible_to or []) if str(x).strip()}
    if eid in PROGRAM_CAPS:
        return "program"
    if eid in SYSTEM_ONLY_IDS or vis == {"system"}:
        return "system"
    if "case" in vis:
        return "case"
    return "system"


def derive_phases(entry_id: str, *, kind: str = "", caller: str = "") -> list[str]:
    who = str(caller or "")
    if who in ("base", "system"):
        return []
    eid = canonical_cap(entry_id)
    if str(kind or "") == "check" or eid == "assert_visual":
        return ["check"]
    return ["prep", "do"]


def is_program_caller(cap_id: str) -> bool:
    """程序在看图之前派。目录 caller 优先，没有行时才回落到 PROGRAM_CAPS。"""
    cap = canonical_cap(cap_id)
    if not cap:
        return False
    from mino_nexus.catalog.registry import get_capability

    row = get_capability(cap)
    caller = str(getattr(row, "caller", "") or "") if row is not None else ""
    if caller:
        return caller == "program"
    return cap in PROGRAM_CAPS


def capability_enabled(cap_id: str) -> bool:
    """未启用或已废弃的技能，模型和程序都不派。目录没有这一行时不拦（逻辑块特例仍走自己的函数）。"""
    cap = canonical_cap(cap_id)
    if not cap:
        return False
    from mino_nexus.catalog.registry import get_capability

    row = get_capability(cap)
    if row is None:
        return True
    if getattr(row, "enabled", True) is False:
        return False
    return str(getattr(row, "lifecycle", "") or "active") != "deprecated"


def capability_ui_coverable(cap_id: str) -> bool:
    """当前产品能否把这条事件计入 UI 自动化覆盖。目录没有这一行时不拦。"""
    cap = canonical_cap(cap_id)
    if not cap:
        return False
    from mino_nexus.catalog.registry import get_capability

    row = get_capability(cap)
    if row is None:
        return True
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
    """派发前：不可用或当前不可覆盖都不派。"""
    if str(cap_id or "").startswith(("human_", "signal_", "recover_")):
        return True
    return capability_enabled(cap_id) and capability_ui_coverable(cap_id)


def phase_allows(cap_id: str, phase: str) -> bool:
    """阶段只卡一次：目录 phases 不含本阶段就拒绝。没有 phases 的行不在这里拦。"""
    cap = canonical_cap(cap_id)
    if not cap or cap.startswith(("human_", "signal_", "recover_")):
        return True
    from mino_nexus.catalog.registry import get_capability

    row = get_capability(cap)
    phases = [str(x) for x in (getattr(row, "phases", None) or [])] if row is not None else []
    if not phases:
        return True
    return str(phase or "").strip().lower() in phases


def menu_caller_ok(cap: Any, phase: str) -> bool:
    """模型菜单只收 caller=case，且 phases 含本阶段。"""
    caller = str(getattr(cap, "caller", "") or "")
    if caller != "case":
        return False
    phases = [str(x) for x in (getattr(cap, "phases", None) or [])]
    p = str(phase or "").strip().lower()
    if phases and p in ("prep", "do", "check") and p not in phases:
        return False
    return True


def canonical_cap(cap_id: str) -> str:
    raw = str(cap_id or "").strip()
    return CAP_ALIASES.get(raw, raw)


def normalize_platform(platform: str) -> str:
    p = str(platform or "").strip().lower()
    if p in ("web", "browser", "playwright"):
        return "web"
    if p in ("ios", "ios_wda"):
        return "ios"
    return p


def channel_platforms_for(cap_id: str) -> frozenset[str] | None:
    """有明确渠道限制时返回平台集合。全渠道返回 None。以目录 platforms 为准。"""
    cap = canonical_cap(cap_id)
    from mino_nexus.catalog.registry import get_capability

    row = get_capability(cap)
    plats = [normalize_platform(p) for p in (getattr(row, "platforms", None) or []) if str(p).strip()]
    if plats:
        return frozenset(plats)
    extra = CHANNEL_PLATFORMS.get(cap)
    if extra:
        return extra
    return None


def channel_supports(cap_id: str, platform: str) -> bool:
    allowed = channel_platforms_for(cap_id)
    if allowed is None:
        return True
    got = normalize_platform(platform)
    if not got:
        return True
    return got in allowed


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
    """渠道没有该技能时标 skipped。返回是否改了状态。"""
    if str(row.get("status") or "").strip().lower() in ("pass", "failed", "skipped"):
        return False
    declared = row.get("platforms")
    if isinstance(declared, list) and declared:
        got = normalize_platform(platform)
        allowed_plats = {normalize_platform(str(x or "")) for x in declared if str(x or "").strip()}
        if got and got not in allowed_plats:
            row["status"] = "skipped"
            row["skip_reason"] = "channel_absent"
            row["skipped_by"] = "channel"
            return True
    caps = milestone_cap_ids(row)
    if not caps:
        return False
    if any(channel_supports(cap, platform) for cap in caps):
        return False
    row["status"] = "skipped"
    row["skip_reason"] = "channel_absent"
    row["skipped_by"] = "channel"
    return True


def case_menu_ids(ctx: Any, phase: str) -> set[str]:
    from mino_nexus.runtime.menu import available_menu_brief

    if ctx is None:
        return set()
    extra: list[str] = []
    if getattr(ctx, "allow_model_recovery", False):
        extra.append("fsm_navigate")
    rows = available_menu_brief(
        ctx,
        audience="case",
        phase=str(phase or "do"),
        platform=str(getattr(ctx, "platform", "") or ""),
        extra_ids=extra,
    )
    return {canonical_cap(str(row.get("id") or "")) for row in rows if str(row.get("id") or "").strip()}


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
) -> bool:
    allowed = set(menu_ids)

    def _cap_ok(cap: str) -> bool:
        if cap in allowed:
            return True
        return bool(allow_program and is_program_caller(cap))

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
    if primary and not all(_cap_ok(cap) for cap in primary):
        return False
    # exec_caps 是这一步可选的动作，渠道上有其中一个即可，不要因为 press_key 仅安卓就把整步丢掉。
    if extras and not any(_cap_ok(cap) for cap in extras):
        return False
    return True


def upgrade_skill_channel_layout() -> int:
    """把基座行移出技能菜单，并把常用技能收成通用 / 预期。"""
    from mino_nexus.catalog import registry as catalog
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.catalog import CatalogEntry

    n = 0
    with session_scope() as db:
        rows = db.query(CatalogEntry).all()
        by_key = {(str(r.kind), str(r.id)): r for r in rows}
        for row in rows:
            eid = str(row.id or "")
            kind = str(row.kind or "")
            if eid in BASE_ENTRY_IDS or eid in CAP_ALIASES:
                target = BASE_KIND
                if (target, eid) in by_key and by_key[(target, eid)] is not row:
                    if row.enabled:
                        row.enabled = False
                        row.lifecycle = "deprecated"
                        n += 1
                    continue
                if kind != target:
                    row.kind = target
                    n += 1
                vis = ["system"]
                if list(row.visible_to_json or []) != vis:
                    row.visible_to_json = vis
                    n += 1
                continue
            if eid in SYSTEM_ONLY_IDS:
                if list(row.visible_to_json or []) != ["system"]:
                    row.visible_to_json = ["system"]
                    n += 1
                continue
            if eid in CASE_GENERIC_IDS:
                want_kind = "check" if eid == "assert_visual" else "generic"
                if kind != want_kind and (want_kind, eid) not in by_key:
                    row.kind = want_kind
                    n += 1
                vis = ["case", "system"]
                if list(row.visible_to_json or []) != vis:
                    row.visible_to_json = vis
                    n += 1
                continue
            if eid == "fsm_navigate":
                vis = ["case", "system"]
                if list(row.visible_to_json or []) != vis:
                    row.visible_to_json = vis
                    n += 1
        n += _retire_prep_do_kinds(db)
        n += _rewrite_skill_tool_kinds(db)
        n += _ensure_program_rows(db)
        n += _stamp_dispatch_caller(db)
    catalog.reload()
    return 1 if n else 0


def _ensure_program_rows(db: Any) -> int:
    """程序要派、目录里还没有的能力先补行。没有行时里程碑会被当成未注册。"""
    from mino_nexus.models.catalog import CatalogEntry

    specs = (
        (
            "confirm_login_state",
            "确认登录态",
            "程序判断是否已离开登录表单。不进模型菜单。",
            ["web"],
        ),
        (
            "open_url",
            "打开网址",
            "程序打开给定网址。不进模型菜单。",
            ["web"],
        ),
        (
            "open_app",
            "打开应用",
            "程序打开给定应用。不进模型菜单。",
            ["android", "ios"],
        ),
    )
    n = 0
    for eid, title, desc, platforms in specs:
        if db.query(CatalogEntry).filter(CatalogEntry.id == eid).first():
            continue
        db.add(CatalogEntry(
            kind="generic",
            id=eid,
            display_name=title,
            description=desc,
            enabled=True,
            lifecycle="active",
            platforms_json=list(platforms),
            visible_to_json=["system"],
            payload_json={
                "caller": "program",
                "phases": ["prep", "do"],
                "event_kind": eid,
            },
        ))
        n += 1
    if n:
        db.flush()
    return n


def _stamp_dispatch_caller(db: Any) -> int:
    """把 caller / phases 写进 payload。不改 enabled / lifecycle。"""
    from sqlalchemy.orm.attributes import flag_modified

    from mino_nexus.models.catalog import CatalogEntry

    n = 0
    for row in db.query(CatalogEntry).all():
        payload = dict(row.payload_json or {})
        vis = list(row.visible_to_json or [])
        caller = derive_caller(str(row.id or ""), kind=str(row.kind or ""), visible_to=vis)
        phases = derive_phases(str(row.id or ""), kind=str(row.kind or ""), caller=caller)
        changed = False
        if str(payload.get("caller") or "") != caller:
            payload["caller"] = caller
            changed = True
        if list(payload.get("phases") or []) != phases:
            payload["phases"] = phases
            changed = True
        want_vis = ["case", "system"] if caller == "case" else ["system"]
        if list(row.visible_to_json or []) != want_vis:
            row.visible_to_json = want_vis
            changed = True
        extra_plats = CHANNEL_PLATFORMS.get(canonical_cap(str(row.id or "")))
        if extra_plats and not list(row.platforms_json or []):
            row.platforms_json = sorted(extra_plats)
            changed = True
        if changed:
            row.payload_json = payload
            flag_modified(row, "payload_json")
            n += 1
    return n


def _retire_prep_do_kinds(db: Any) -> int:
    """前置 / 操作分组里剩下的行并入通用，或在已有同 id 时删掉重复行。"""
    from mino_nexus.models.catalog import CatalogEntry

    n = 0
    leftovers = db.query(CatalogEntry).filter(CatalogEntry.kind.in_(("prep", "do"))).all()
    for row in leftovers:
        eid = str(row.id or "")
        survivor = (
            db.query(CatalogEntry)
            .filter(
                CatalogEntry.id == eid,
                CatalogEntry.kind.notin_(("prep", "do")),
                CatalogEntry.pk != row.pk,
            )
            .first()
        )
        if survivor is not None:
            db.delete(row)
            n += 1
            continue
        row.kind = "generic"
        if eid in SYSTEM_ONLY_IDS:
            row.visible_to_json = ["system"]
        n += 1
    return n


def _rewrite_skill_tool_kinds(db: Any) -> int:
    """技能 SOP 里的目录分组名去掉 prep / do，菜单只留通用。"""
    from sqlalchemy.orm.attributes import flag_modified

    from mino_nexus.models.skill import Skill

    n = 0
    for row in db.query(Skill).all():
        sop = dict(row.sop_json or {})
        changed = False
        kinds = sop.get("tool_kinds")
        if isinstance(kinds, list):
            nxt = _without_legacy_kinds(kinds)
            if nxt != kinds:
                sop["tool_kinds"] = nxt
                changed = True
        phases = sop.get("phases")
        if isinstance(phases, list):
            new_phases = []
            for phase in phases:
                if not isinstance(phase, dict):
                    new_phases.append(phase)
                    continue
                phase = dict(phase)
                pkinds = phase.get("tool_kinds")
                if isinstance(pkinds, list):
                    nxt = _without_legacy_kinds(pkinds)
                    if nxt != pkinds:
                        phase["tool_kinds"] = nxt
                        changed = True
                new_phases.append(phase)
            sop["phases"] = new_phases
        if changed:
            row.sop_json = sop
            flag_modified(row, "sop_json")
            n += 1
    return n


def _without_legacy_kinds(kinds: list[Any]) -> list[str]:
    out: list[str] = []
    for item in kinds:
        name = str(item or "").strip()
        if name in ("prep", "do"):
            name = "generic"
        if name and name not in out:
            out.append(name)
    return out
