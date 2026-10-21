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


def _first_line(*parts: object) -> str:
    """取第一行非空文本。空串 splitlines() 是 []，不能再取 [0]。"""
    for part in parts:
        lines = str(part or "").strip().splitlines()
        line = lines[0].strip() if lines else ""
        if line:
            return line
    return ""


def _cap_summary(cap: Capability) -> str:
    return _first_line(cap.description, getattr(cap, "display_name", ""))[:120]


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
    kinds = phase_kinds(phase, tool_kinds=tool_kinds)
    flags = ctx.connectivity_flags
    available = plugin_registry.filter_capabilities(
        flags,
        kinds=kinds,
        platform=plat,
    )
    if plat in {"desktop", "mac", "win"}:
        from mino_nexus.services.desktop_catalog import CAPABILITY_IDS, capabilities

        available = [cap for cap in available if cap.id not in CAPABILITY_IDS]
        if flags.get("desktop") and "generic" in kinds:
            available.extend(capabilities(ctx.desktop_capabilities, ctx.desktop_os, platform=plat))
    return available


def _bound_menu_ids(ctx: RunContext, phase: str) -> set[str]:
    from mino_nexus.services.execution_binding import CONTRACT

    if getattr(ctx, "execution_contract_version", "") != CONTRACT or not getattr(ctx, "execution_binding", None):
        return set()
    check = getattr(ctx, "execution_binding_menu", None)
    if not callable(check):
        return set()
    return set(check(phase) or ())


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
    allowed = _bound_menu_ids(ctx, phase)
    if not allowed:
        return []
    kind = (kind or "agent").strip().lower()
    plat = str(platform or getattr(ctx, "platform", "") or "").strip()
    from mino_nexus.catalog.skill_channel import menu_caller_ok

    out: list[dict[str, Any]] = []
    available = available_capabilities(ctx, phase=phase, platform=plat, tool_kinds=tool_kinds)
    for cap in available:
        if audience == "case" and not menu_caller_ok(cap, phase):
            continue
        if audience != "case" and not _visible_to(cap, audience):
            continue
        row: dict[str, Any] = {"id": cap.id, "kind": cap.kind}
        summary = _cap_summary(cap)
        if summary:
            row["summary"] = summary
        parameters = getattr(cap, "parameters", None)
        if isinstance(parameters, dict):
            row["parameters"] = parameters
        out.append(row)
    seen = {str(row.get("id") or "") for row in out}
    for eid in extra_ids or []:
        cap_id = str(eid or "").strip()
        if not cap_id or cap_id in seen:
            continue
        cap = plugin_registry.get_capability(cap_id)
        if plat in {"desktop", "mac", "win"}:
            from mino_nexus.services.desktop_catalog import CAPABILITY_IDS, capabilities

            if cap_id in CAPABILITY_IDS:
                native = capabilities(ctx.desktop_capabilities, ctx.desktop_os, platform=plat) if ctx.connectivity_flags.get("desktop") else []
                cap = next((candidate for candidate in native if candidate.id == cap_id), None)
        if cap is None:
            continue
        from mino_nexus.catalog.skill_channel import capability_dispatch_ok

        if not capability_dispatch_ok(cap_id):
            continue
        if audience == "case" and not menu_caller_ok(cap, phase):
            continue
        if audience != "case" and not _visible_to(cap, audience):
            continue
        if plat and not plugin_registry.platform_ok(list(cap.platforms or []), plat):
            continue
        row = {"id": cap.id, "kind": cap.kind}
        summary = _cap_summary(cap)
        if summary:
            row["summary"] = summary
        parameters = getattr(cap, "parameters", None)
        if isinstance(parameters, dict):
            row["parameters"] = parameters
        out.append(row)
        seen.add(cap.id)
    from mino_nexus.services.local_tool_dependencies import evaluate
    # Keep unmet tools visible with a safe reason; never label them not_applicable.
    for row in out:
        facts = evaluate(row["id"], ctx=ctx)
        if facts:
            row["dependencies"] = facts
    return [row for row in out if row['id'] in allowed]


def recovery_menu_brief(ctx: RunContext, *, platform: str = "") -> list[dict[str, Any]]:
    from mino_nexus.loop.observe.session_log import active_writer

    writer = active_writer()
    allowed = _bound_menu_ids(ctx, writer.phase if writer else "prep")
    if not allowed:
        return []
    plat = str(platform or getattr(ctx, "platform", "") or "").strip()
    rows: list[dict[str, Any]] = []
    for rule in plugin_registry.list_recovery_rules(enabled_only=True):
        if not plugin_registry.platform_ok(list(rule.platforms or []), plat):
            continue
        summary = _first_line(rule.when, rule.prompt_snippet, rule.title, rule.id)[:120]
        rows.append({
            "id": f"{RECOVER_PREFIX}{rule.id}",
            "kind": "recovery",
            "summary": summary,
        })
    return [row for row in rows if row['id'] in allowed]


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
