"""一次点击只走一种模式。方案由任务上的 action_scheme 决定。"""
from __future__ import annotations

from typing import Any

_POINT_CAPS = frozenset({
    "tap_element",
    "long_press_element",
    "multi_tap",
    "swipe_direction",
    "swipe_element_to_element",
})


def stamp_scheme_params(cap_id: str, params: dict[str, Any] | None, scheme: str) -> dict[str, Any]:
    """按任务方案盖章。visual 去掉锚点；dom 保留锚点，并补上 target。"""
    out = dict(params or {})
    cid = str(cap_id or "").strip()
    if cid not in _POINT_CAPS and cid != "input_text":
        return out
    mode = "node" if str(scheme or "").strip().lower() == "dom" else "coordinate"
    if mode == "node":
        from mino_nexus.action_space.dom import apply_dom_point
        from mino_nexus.ai.coords import lift_selector_target

        typed = out.get("text") if cid == "input_text" else None
        if cid == "input_text":
            out.pop("text", None)
        out = apply_dom_point(out)
        lift_selector_target(out)
        if cid == "input_text" and typed is not None:
            out["text"] = typed
        return out
    from mino_nexus.action_space.visual import apply_visual_point

    return apply_visual_point(out, keep_text=(cid == "input_text"))


def visual_point_missing(cap_id: str, params: dict[str, Any] | None) -> bool:
    """看图的点、长按、连点、输入必须带 x,y。方向滑动可以只有 direction。"""
    cid = str(cap_id or "").strip()
    p = params or {}
    if cid in ("tap_element", "long_press_element", "multi_tap", "input_text"):
        return p.get("x") is None or p.get("y") is None
    if cid == "swipe_element_to_element":
        return any(p.get(k) is None for k in ("from_x", "from_y", "to_x", "to_y"))
    return False


def assemble_execute_params(
    cap_id: str,
    params: dict[str, Any] | None,
    ctx: Any = None,
    *,
    width: int = 0,
    height: int = 0,
) -> dict[str, Any]:
    out = dict(params or {})
    cid = str(cap_id or "").strip()
    if cid not in _POINT_CAPS and cid != "input_text":
        return out
    from mino_nexus.ai.coords import prepare_xy_params_for_execute
    from mino_nexus.action_space.scheme import action_scheme
    from mino_nexus.loop.ui_channel import UiChannel, ui_channel_from_ctx

    prepare_xy_params_for_execute(out, int(width or 0), int(height or 0))
    scheme = action_scheme(ctx)
    if ui_channel_from_ctx(ctx) == UiChannel.WEB and cid in _POINT_CAPS and scheme != "dom":
        from mino_nexus.action_space.web import apply_web_point

        out = apply_web_point(out)
    return stamp_scheme_params(cid, out, scheme)
