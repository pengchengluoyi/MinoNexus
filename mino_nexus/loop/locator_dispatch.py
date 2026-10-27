"""把定位链解析成可下发的 AgentDecision。miss 只记账，不判失败。"""
from __future__ import annotations

from typing import Any, Optional

from mino_nexus.ai.schemas import AgentAction, AgentDecision
from mino_nexus.action_space.locator_chain import (
    ChainResult,
    attach_to_row,
    build_chain,
    locator_chain_enabled,
    match_phrases_of,
    needs_locator,
    platform_of,
    resolve,
)
from mino_nexus.action_space.locator_platforms import for_platform
from mino_nexus.loop.binding_cache import intent_key, lookup
from mino_nexus.loop.milestones import read_state


def _nodes(ctx: Any) -> list[dict[str, Any]]:
    return [n for n in (getattr(ctx, "nav_hierarchy_nodes", None) or []) if isinstance(n, dict)]


def _focus(cursor: Any, focus: dict[str, Any] | None) -> dict[str, Any] | None:
    if isinstance(focus, dict):
        return focus
    for row in read_state(cursor).get("milestones") or []:
        if isinstance(row, dict) and str(row.get("status") or "") == "in_progress":
            return row
    return None


def _cap(row: dict[str, Any]) -> str:
    kind = str(row.get("kind") or "").strip().lower()
    cap = str(row.get("hook_cap") or row.get("device_cap") or row.get("cap") or "").strip()
    if not cap and kind in {"visual_tap", "visual_action", "nav"}:
        return "tap_element"
    return cap


def _log(writer: Any, result: ChainResult, row: dict[str, Any], cap: str) -> None:
    if writer is None:
        return
    payload = result.to_event()
    payload["milestone_id"] = str(row.get("id") or "")
    payload["capability_id"] = cap
    writer.append(f"locator_chain/{result.status}", payload)


def _remember_hit(ctx: Any, row: dict[str, Any], result: ChainResult) -> None:
    if result.status != "hit" or ctx is None:
        return
    if str(result.used_key or "") == "node_ref":
        return
    cid = str(row.get("control_id") or "").strip()
    params = row.get("params") if isinstance(row.get("params"), dict) else {}
    cid = cid or str(params.get("control_id") or "").strip()
    project_id = str(getattr(ctx, "project_id", "") or "").strip()
    platform = platform_of(ctx, row)
    value = str(result.used_value or "").strip()
    if not value or not project_id or not platform:
        return
    try:
        from mino_nexus.knowledge.control_registry import (
            ControlRegistryReadOnlyError,
            fill_locator,
            fill_locator_spec_draft,
            find_control_by_label,
            runtime_readonly,
        )

        if runtime_readonly():
            return
        if not cid:
            found = find_control_by_label(project_id, value)
            cid = str((found or {}).get("control_id") or "")
            if cid:
                row["control_id"] = cid
        if not cid:
            return
        fill_locator(project_id, cid, scheme="dom", platform=platform, value=value, source="locator_hit")
        strat = for_platform(platform)
        spec = strat.spec_from_hit(result.node or {}, result.used_key, value)
        if spec:
            fill_locator_spec_draft(project_id, cid, spec)
    except (ControlRegistryReadOnlyError, ValueError, KeyError, TypeError):
        return


def try_locator_chain_decision(
    cursor: Any,
    ctx: Any,
    writer: Any = None,
    *,
    focus: dict[str, Any] | None = None,
) -> Optional[AgentDecision]:
    if not locator_chain_enabled(ctx):
        return None
    from mino_nexus.loop.fuse.interrupt_stack import stack_depth

    if stack_depth(cursor) > 0:
        return None
    row = _focus(cursor, focus)
    if not isinstance(row, dict):
        return None
    if str(row.get("observe") or "exec").strip().lower() == "visual_each_run":
        return None
    cap = _cap(row)
    if not needs_locator(cap):
        return None

    plat = platform_of(ctx, row)
    strat = for_platform(plat)
    snapshot = str((getattr(ctx, "observation_contract", None) or {}).get("snapshot_id") or "")
    cache_key = (str(row.get("id") or ""), snapshot, cap, plat)
    if getattr(cursor, "_locator_chain_key", None) == cache_key:
        return getattr(cursor, "_locator_chain_decision", None)

    if not strat.structural:
        result = ChainResult(status="skip", reason="visual_platform", platform=plat or strat.name)
        _log(writer, result, row, cap)
        cursor._locator_chain_key = cache_key
        cursor._locator_chain_decision = None
        return None

    from mino_nexus.loop.intent_tap import dom_checkbox_tap_decision

    before_id = str(row.get("id") or "")
    applicable, checkbox = dom_checkbox_tap_decision(cursor, ctx, writer)
    if checkbox is not None:
        cursor._locator_chain_key = cache_key
        cursor._locator_chain_decision = checkbox
        return checkbox
    after = _focus(cursor, None)
    if str((after or {}).get("id") or "") != before_id:
        cursor._locator_chain_key = cache_key
        cursor._locator_chain_decision = None
        return None
    if applicable:
        cursor._locator_chain_key = cache_key
        cursor._locator_chain_decision = None
        return None

    attach_to_row(
        row,
        platform=plat,
        project_id=str(getattr(ctx, "project_id", "") or ""),
        app_id=str(getattr(ctx, "app_id", "") or ""),
        ctx=ctx,
    )
    params = row.get("params") if isinstance(row.get("params"), dict) else {}
    compiled = row.get("locator_chain") if isinstance(row.get("locator_chain"), dict) else {}
    fp = lookup(cursor, ctx, intent_key(cursor, row))
    control = None
    cid = str(row.get("control_id") or params.get("control_id") or "").strip()
    if cid and getattr(ctx, "project_id", ""):
        try:
            from mino_nexus.knowledge.control_registry import get_control

            control = get_control(str(ctx.project_id), cid)
        except (ValueError, KeyError, TypeError):
            control = None
    chain = build_chain(
        capability_id=cap,
        params=params,
        compiled=compiled,
        fingerprints=fp,
        match_phrases=match_phrases_of(row),
        snapshot_id=snapshot,
        platform=plat,
        control=control,
    )
    result = resolve(chain, _nodes(ctx))
    _log(writer, result, row, cap)
    decision: AgentDecision | None = None
    if result.status == "hit" and result.node is not None:
        setattr(cursor, "binding_last_node", dict(result.node))
        _remember_hit(ctx, row, result)
        if cap != "input_text":
            out = strat.assemble_hit(params, result.node, result, _nodes(ctx))
            thought = f"定位链唯一命中 {result.used_key}={result.used_value[:40]}"
            decision = AgentDecision(
                status="continue",
                thought=thought[:240],
                action=AgentAction(capability_id=cap, params=out),
                confidence=0.9,
            )
    cursor._locator_chain_key = cache_key
    cursor._locator_chain_decision = decision
    return decision
