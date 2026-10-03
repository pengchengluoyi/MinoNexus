"""按阶段 + 平台 + 连通性组装喂给模型的能力菜单。"""
from __future__ import annotations

from typing import Any, Optional

from mino_nexus.catalog import registry as plugin_registry
from mino_nexus.catalog.models import Capability
from mino_nexus.runtime.run_context import RunContext

RECOVER_PREFIX = "recover_"


def _visible_to(cap: Capability, audience: str) -> bool:
    if audience == "all":
        return True
    allowed = [str(x).strip().lower() for x in (getattr(cap, "visible_to", None) or [])]
    if "both" in allowed:
        return True
    if not allowed:
        return audience == "system"
    return audience in allowed


def _cap_summary(cap: Capability) -> str:
    text = (cap.description or "").strip().splitlines()[0].strip()
    return text[:120] if text else ""


def phase_kinds(phase: str, *, tool_kinds: Optional[list[str]] = None, phase_cfg: Optional[dict] = None) -> list[str]:
    """前置和操作只注入通用。校验只注入预期。恢复不进平时菜单。"""
    del phase_cfg
    p = str(phase or "do").strip().lower()
    if p == "check":
        kinds = ["check"]
    elif p in ("prep", "do"):
        kinds = ["generic"]
    else:
        kinds = ["generic", "check"]
    if tool_kinds:
        allow = {str(x) for x in tool_kinds}
        if allow & {"prep", "do", "generic"} and "generic" not in allow:
            allow.add("generic")
        kinds = [k for k in kinds if k in allow]
    return kinds


def available_capabilities(
    ctx: RunContext,
    *,
    phase: str = "do",
    platform: str = "",
    tool_kinds: Optional[list[str]] = None,
) -> list[Capability]:
    plat = str(platform or getattr(ctx, "platform", "") or "").strip()
    return plugin_registry.filter_capabilities(
        ctx.connectivity_flags,
        kinds=phase_kinds(phase, tool_kinds=tool_kinds),
        platform=plat,
    )


def available_menu_brief(
    ctx: RunContext,
    *,
    audience: str = "case",
    kind: str = "agent",
    phase: str = "do",
    platform: str = "",
    tool_kinds: Optional[list[str]] = None,
    extra_ids: Optional[list[str]] = None,
) -> list[dict[str, Any]]:
    """id + summary。不把 platforms / implementations / low_level 塞进 prompt。"""
    kind = (kind or "agent").strip().lower()
    plat = str(platform or getattr(ctx, "platform", "") or "").strip()
    from mino_nexus.catalog.skill_channel import menu_caller_ok

    out: list[dict[str, Any]] = []
    for cap in available_capabilities(ctx, phase=phase, platform=plat, tool_kinds=tool_kinds):
        if audience == "case" and not menu_caller_ok(cap, phase):
            continue
        if not _visible_to(cap, audience):
            continue
        row: dict[str, Any] = {"id": cap.id, "kind": cap.kind}
        summary = _cap_summary(cap)
        if summary:
            row["summary"] = summary
        out.append(row)
    seen = {str(row.get("id") or "") for row in out}
    for eid in extra_ids or []:
        cap_id = str(eid or "").strip()
        if not cap_id or cap_id in seen:
            continue
        cap = plugin_registry.get_capability(cap_id)
        if cap is None:
            continue
        from mino_nexus.catalog.skill_channel import capability_dispatch_ok

        if not capability_dispatch_ok(cap_id):
            continue
        if plat and not plugin_registry.platform_ok(list(cap.platforms or []), plat):
            continue
        row = {"id": cap.id, "kind": cap.kind}
        summary = _cap_summary(cap)
        if summary:
            row["summary"] = summary
        out.append(row)
        seen.add(cap.id)
    return out


def recovery_menu_brief(ctx: RunContext, *, platform: str = "") -> list[dict[str, Any]]:
    plat = str(platform or getattr(ctx, "platform", "") or "").strip()
    rows: list[dict[str, Any]] = []
    for rule in plugin_registry.list_recovery_rules(enabled_only=True):
        if not plugin_registry.platform_ok(list(rule.platforms or []), plat):
            continue
        summary = (rule.when or rule.prompt_snippet or rule.title or rule.id).strip().splitlines()[0][:120]
        rows.append({
            "id": f"{RECOVER_PREFIX}{rule.id}",
            "kind": "recovery",
            "summary": summary,
        })
    return rows


def capability_menu_diagnostics(ctx: RunContext) -> dict[str, Any]:
    flags = ctx.connectivity_flags
    available_ids = {c.id for c in available_capabilities(ctx, phase="do")}
    dropped: list[dict[str, Any]] = []
    for cap in plugin_registry.list_capabilities():
        if cap.id in available_ids:
            continue
        dropped.append({
            "id": cap.id,
            "kind": cap.kind,
            "platforms": list(cap.platforms or []),
            "reasons": ["filtered by platform or connectivity"],
        })
    return {
        "flags": flags,
        "available_count": len(available_ids),
        "dropped": dropped,
    }
