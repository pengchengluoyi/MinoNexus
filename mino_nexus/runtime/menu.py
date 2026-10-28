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
    """prep / do / check 使用同一份事件能力，不再按阶段裁剪。"""
    del phase, tool_kinds, phase_cfg
    from mino_nexus.catalog.exec_classes import MENU_ATOMIC_KINDS

    return list(MENU_ATOMIC_KINDS)


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
        from mino_nexus.knowledge.desktop_catalog import CAPABILITY_IDS, capabilities

        available = [cap for cap in available if cap.id not in CAPABILITY_IDS]
        if flags.get("desktop") and "generic" in kinds:
            available.extend(capabilities(ctx.desktop_capabilities, ctx.desktop_os, platform=plat))
    return available


def _bound_menu_ids(ctx: RunContext, phase: str) -> set[str]:
    from mino_nexus.contract.binding import CONTRACT

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
    """id + summary。不把 platforms / implementations / low_level 塞进 prompt。

    ``allowed`` 是绑定投影出的建议集合；没有绑定或投影失败时不再返回空菜单，
    否则规划会直接 give_up，里程碑也会被误判成能力未注册（规划 §4）。
    """
    allowed = _bound_menu_ids(ctx, phase)
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
            from mino_nexus.knowledge.desktop_catalog import CAPABILITY_IDS, capabilities

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
    # 逻辑块是意图宏，不再只藏在恢复菜单里：prep/do/check 都能按块 id 调度。
    # 这样「意图识别 → 边展开 → 同一解释器」才有真正的执行入口（规划 §3）。
    if (kind == "agent" and str(phase or "do") in ("prep", "do", "check")
            and "run_flow_block" not in seen):
        try:
            from mino_nexus.loop.block_dispatch import dispatchable_block_ids

            block_ids = dispatchable_block_ids(app_id=str(getattr(ctx, "app_id", "") or ""))
        except Exception:  # noqa: BLE001 - 菜单降级不能拖垮整个决策
            block_ids = []
        if block_ids:
            out.append({
                "id": "run_flow_block", "kind": "recovery",
                "summary": "执行意图/逻辑块（登录、协议、私有分支、恢复）；可用块：" + " / ".join(block_ids[:30]),
            })
            seen.add("run_flow_block")
    from mino_nexus.resources.local_tool_dependencies import evaluate
    # Keep unmet tools visible with a safe reason; never label them not_applicable.
    for row in out:
        facts = evaluate(row["id"], ctx=ctx)
        if facts:
            row["dependencies"] = facts
    return [row for row in out
            if not allowed or row['id'] in allowed or row['id'] == "run_flow_block"]


def recovery_menu_brief(ctx: RunContext, *, platform: str = "") -> list[dict[str, Any]]:
    from mino_nexus.records.eventlog.writer import active_writer

    writer = active_writer()
    allowed = _bound_menu_ids(ctx, writer.phase if writer else "prep")
    plat = str(platform or getattr(ctx, "platform", "") or "").strip()
    # 恢复不再按规则暴露虚拟能力：模型只有一个入口 run_flow_block(block_id)，
    # 具体做哪些步骤由块目录声明。
    rows: list[dict[str, Any]] = []
    if any(plugin_registry.platform_ok(list(rule.platforms or []), plat)
           for rule in plugin_registry.list_recovery_rules(enabled_only=True)):
        from mino_nexus.loop.block_dispatch import dispatchable_block_ids

        ids = dispatchable_block_ids(app_id=str(getattr(ctx, "app_id", "") or ""))
        if ids:
            rows.append({
                "id": "run_flow_block",
                "kind": "recovery",
                "summary": "执行逻辑块（" + " / ".join(ids[:20]) + "）",
            })
    return [row for row in rows if not allowed or row['id'] in allowed]


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
