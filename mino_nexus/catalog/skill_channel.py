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
    """有明确渠道限制时返回平台集合。全渠道返回 None。"""
    cap = canonical_cap(cap_id)
    extra = CHANNEL_PLATFORMS.get(cap)
    if extra:
        return extra
    from mino_nexus.catalog.registry import get_capability

    row = get_capability(cap)
    plats = [normalize_platform(p) for p in (getattr(row, "platforms", None) or []) if str(p).strip()]
    if plats:
        return frozenset(plats)
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
    if allow_program:
        allowed |= PROGRAM_CAPS
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
    if primary and not all(cap in allowed for cap in primary):
        return False
    # exec_caps 是这一步可选的动作，渠道上有其中一个即可，不要因为 press_key 仅安卓就把整步丢掉。
    if extras and not any(cap in allowed for cap in extras):
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
    catalog.reload()
    return 1 if n else 0


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
