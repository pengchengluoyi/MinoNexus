"""先看 DOM 布局。这一步 DOM 执行不了，再降级到看图。

入口不提供「看图优先」。历史用例里的 `visual` 归一成 `dom_preferred`。
本轮先按层级做；层级不存在，或当前动作已经在 DOM 上执行失败，才改看截图。
"""
from __future__ import annotations
from typing import Any

DEFAULT_ACTION_SCHEME = "dom_preferred"

# 仅用于读取历史数据；不再向外输出。
LEGACY_SCHEMES = frozenset({"visual"})

def normalize_action_scheme(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw in ("dom", "dom_preferred", "", *LEGACY_SCHEMES):
        return DEFAULT_ACTION_SCHEME
    raise ValueError(f"未知执行策略：{raw}")

def action_scheme(ctx: Any) -> str:
    raw = getattr(ctx, "action_scheme", "") if ctx is not None else ""
    case = getattr(ctx, "case", None) if ctx is not None else None
    if not raw and isinstance(case, dict):
        raw = case.get("action_scheme")
    return normalize_action_scheme(raw)

def execution_scheme(ctx: Any) -> str:
    """DOM 能做就走 DOM。已经降级的这一步才看图。"""
    if str(getattr(ctx, "observation_mode", "") or "") == "visual":
        return "visual"
    return "dom"


def _focus_milestone_id(cursor: Any) -> str:
    if cursor is None:
        return ""
    from mino_nexus.runtime.cursor_state import read_state

    for row in read_state(cursor).get("milestones") or []:
        if isinstance(row, dict) and str(row.get("status") or "") in {"in_progress", "pending", "running"}:
            return str(row.get("id") or "")
    return ""

def _visual_input_focus_active(ctx: Any, cursor: Any) -> bool:
    """截图聚焦后的同一次输入沿用焦点；下个业务动作重新选择观察方式。"""
    if cursor is None or getattr(ctx, "focus_action_scheme", "") != "visual":
        return False
    from mino_nexus.runtime.cursor_state import read_state
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
        n.get("text") or n.get("content_desc") or n.get("name") or n.get("title")
        or n.get("identifier") or n.get("automation_id") or n.get("accessibility_id")
        or n.get("editable") or n.get("clickable") or n.get("checkable")
        for n in usable_nodes
    )
    focus_id = _focus_milestone_id(cursor)
    fallback = str(getattr(ctx, "observation_fallback_reason", "") or "")
    same_action = bool(fallback) and focus_id and focus_id == str(getattr(ctx, "observation_fallback_milestone", "") or "")
    keep_visual = (not usable) or same_action or _visual_input_focus_active(ctx, cursor)
    if keep_visual:
        reason = "structure_unavailable" if not usable else ("visual_input_focus" if _visual_input_focus_active(ctx, cursor) else fallback or "dom_cannot_execute")
        ctx.observation_mode = "visual"
        ctx.observation_fallback_reason = reason
    else:
        reason = "structure_available"
        ctx.observation_mode = "dom"
        ctx.observation_fallback_reason = ""
        ctx.observation_fallback_milestone = ""
    if writer:
        writer.append("observation/strategy", {"strategy": action_scheme(ctx), "mode": ctx.observation_mode,
                      "reason": reason, "nodes": len(nodes), "execution_started": False})
    return ctx.observation_mode

def use_visual_fallback(ctx: Any, *, reason: str, writer: Any = None) -> None:
    """当前这一步 DOM 执行不了，改由看图定位。下一步能用 DOM 时再回到 DOM。"""
    ctx.observation_mode = "visual"
    ctx.observation_fallback_reason = reason
    ctx.observation_fallback_milestone = _focus_milestone_id(getattr(ctx, "_step_cursor", None))
    if writer:
        writer.append("observation/fallback", {"strategy": action_scheme(ctx), "reason": reason,
                      "mode": "visual", "execution_started": False, "same_observation": True})


def structure_target_nodes(capability_id: str, params: Any, ctx: Any) -> list[dict[str, Any]]:
    """只认本轮唯一的结构锚点；input_text.text 是输入值，永远不是定位文案。"""
    from mino_nexus.action_space.locator_chain import build_chain, platform_of, resolve

    p = params if isinstance(params, dict) else {}
    chain = build_chain(capability_id=str(capability_id or ""), params=p, platform=platform_of(ctx))
    result = resolve(chain, getattr(ctx, "nav_hierarchy_nodes", None) or [])
    if result.status == "hit" and result.node is not None:
        return [result.node]
    return []


def structure_target_available(capability_id: str, params: Any, ctx: Any) -> bool:
    if capability_id not in {"tap_element", "long_press_element", "multi_tap", "input_text"}:
        return True
    return len(structure_target_nodes(capability_id, params, ctx)) == 1
