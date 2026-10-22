"""任务策略与本轮执行方式分开；旧 dom 任务兼容 DOM 优先。"""
from __future__ import annotations
from typing import Any

DEFAULT_ACTION_SCHEME = "dom_preferred"

def normalize_action_scheme(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw in ("dom", "dom_preferred"):
        return DEFAULT_ACTION_SCHEME
    if raw == "visual":
        return "visual"
    if not raw:
        return DEFAULT_ACTION_SCHEME
    raise ValueError(f"未知执行策略：{raw}")

def action_scheme(ctx: Any) -> str:
    raw = getattr(ctx, "action_scheme", "") if ctx is not None else ""
    case = getattr(ctx, "case", None) if ctx is not None else None
    if not raw and isinstance(case, dict):
        raw = case.get("action_scheme")
    return normalize_action_scheme(raw)

def execution_scheme(ctx: Any) -> str:
    if action_scheme(ctx) == "visual":
        return "visual"
    return "visual" if getattr(ctx, "observation_mode", "") == "visual" else "dom"

def _visual_input_focus_active(ctx: Any, cursor: Any) -> bool:
    """截图聚焦后的同一次输入沿用焦点；下个业务动作重新选择观察方式。"""
    if cursor is None or getattr(ctx, "focus_action_scheme", "") != "visual":
        return False
    from mino_nexus.loop.milestones import read_state
    from mino_nexus.action_space.visual import bind_focus_ref

    row = next((r for r in read_state(cursor).get("milestones", [])
                if isinstance(r, dict) and r.get("status") == "in_progress"), {})
    if str(row.get("device_cap") or row.get("hook_cap") or "") != "input_text":
        return False
    params = row.get("params") if isinstance(row.get("params"), dict) else {}
    bound = bind_focus_ref({"target_mode": "current_focus", "field": params.get("field", "")}, ctx)
    return bool(bound.get("focus_ref")) and not bound.get("_focus_unresolved")


def select_observation(ctx: Any, *, cursor: Any = None, writer: Any = None) -> str:
    nodes = [n for n in (getattr(ctx, "nav_hierarchy_nodes", None) or []) if isinstance(n, dict)]
    from mino_nexus.navigation.geometry.hierarchy_slots import flatten

    usable_nodes = [n for n in nodes if not n.get("cross_origin") and n.get("visible", True)]
    usable = bool(flatten(usable_nodes).strip()) and any(
        n.get("text") or n.get("content_desc") or n.get("editable") or n.get("clickable") or n.get("checkable")
        for n in usable_nodes
    )
    pending = bool(getattr(cursor, "visual_prior", None) or getattr(cursor, "pending_visual_review", None))
    recovering = False
    if cursor is not None:
        from mino_nexus.loop.milestones import read_state

        recovering = bool(getattr(cursor, "interrupt_stack_v1", None) or read_state(cursor).get("screen_obstacle"))
    from mino_nexus.loop.login_evidence_navigation import active_evidence_navigation, restore_execution_reference

    restoring = bool(cursor is not None and restore_execution_reference(cursor, ctx))
    from mino_nexus.loop.prep_program_plan import active_reauthentication_navigation

    reauthenticating = active_reauthentication_navigation(cursor)
    observing_login = active_evidence_navigation(cursor)
    reason = "explicit_visual" if action_scheme(ctx) == "visual" else "pending_action_review" if pending else "screen_obstacle_recovery" if recovering else "login_context_restore" if restoring else "prep_reauthentication_navigation" if reauthenticating else "login_evidence_navigation" if observing_login else "visual_input_focus" if _visual_input_focus_active(ctx, cursor) else "structure_available" if usable else "structure_unavailable"
    ctx.observation_mode = "dom" if reason == "structure_available" else "visual"
    ctx.observation_fallback_reason = "" if ctx.observation_mode == "dom" else reason
    if writer:
        writer.append("observation/strategy", {"strategy": action_scheme(ctx), "mode": ctx.observation_mode,
                      "reason": reason, "nodes": len(nodes), "execution_started": False})
    return ctx.observation_mode

def use_visual_fallback(ctx: Any, *, reason: str, writer: Any = None) -> None:
    ctx.observation_mode = "visual"
    ctx.observation_fallback_reason = reason
    if writer:
        writer.append("observation/fallback", {"strategy": action_scheme(ctx), "reason": reason,
                      "execution_started": False, "same_observation": True})


def structure_target_nodes(capability_id: str, params: Any, ctx: Any) -> list[dict[str, Any]]:
    """只认本轮唯一的结构锚点；input_text.text 是输入值，永远不是定位文案。"""
    p = params if isinstance(params, dict) else {}
    ref = str(p.get("node_ref") or "")
    target = p.get("target") if isinstance(p.get("target"), dict) else {}
    label = str(p.get("selector_text") or (p.get("text") if capability_id != "input_text" else "") or "").strip()
    label = label or str(target.get("text") or target.get("content_desc") or "").strip()
    resource = str(target.get("resource_id") or target.get("id") or p.get("resource_id") or "").strip()
    nodes = [n for n in (getattr(ctx, "nav_hierarchy_nodes", None) or []) if isinstance(n, dict)]
    hits = []
    for node in nodes:
        if node.get("cross_origin") or not node.get("enabled", True):
            continue
        if capability_id == "input_text" and not (node.get("editable") or "edittext" in str(node.get("class") or "").lower()):
            continue
        if ref:
            matched = bool(p.get("snapshot_id")) and node.get("node_ref") == ref and node.get("snapshot_id") == p.get("snapshot_id")
        elif resource:
            matched = resource == str(node.get("resource_id") or "")
        else:
            matched = bool(label) and label in (node.get("text"), node.get("content_desc"))
        if matched:
            hits.append(node)
    return hits


def structure_target_available(capability_id: str, params: Any, ctx: Any) -> bool:
    if capability_id not in {"tap_element", "long_press_element", "multi_tap", "input_text"}:
        return True
    return len(structure_target_nodes(capability_id, params, ctx)) == 1
