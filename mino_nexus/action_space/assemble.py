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
        from mino_nexus.contract.coords import lift_selector_target

        typed = out.get("text") if cid == "input_text" else None
        if cid == "input_text":
            out.pop("text", None)
        out = apply_dom_point(out)
        lift_selector_target(out)
        if cid == "input_text" and typed is not None:
            out["text"] = typed
        return out
    from mino_nexus.action_space.visual import apply_focus_input, apply_visual_point

    if cid == "input_text":
        return apply_focus_input(out)
    return apply_visual_point(out, keep_text=False)


def input_combination(cap_id: str, params: dict[str, Any] | None, scheme: str,
                      platform: str = "") -> dict[str, Any]:
    """绑定和派发必须算出同一个输入组合，否则绑定里没有这条 operation。

    Android 当前焦点只实现 append（Scout `input_contract` v2），这里和
    `assemble_execute_params` 用同一条规则：避免绑定按 replace 建、派发按
    append 找，最后报成"阶段不匹配"。
    """
    stamped = stamp_scheme_params(cap_id, params, scheme)
    target = str(stamped.get("target_mode") or stamped.get("point_policy") or "").strip()
    mode = str(stamped.get("mode") or "replace").strip().lower()
    if mode not in ("replace", "append"):
        mode = "replace"
    if str(cap_id or "").strip() == "input_text" and str(platform or "").strip().lower() == "android":
        # Android 只有 current_focus + append 一种输入契约，绑定必须按这个建。
        target, mode = "current_focus", "append"
    return {"contract_version": 2, "target_mode": target, "write_mode": mode}


def visual_point_missing(cap_id: str, params: dict[str, Any] | None) -> bool:
    """看图的点、长按、连点必须带 x,y。输入写当前焦点，不带坐标。方向滑动可以只有 direction。"""
    cid = str(cap_id or "").strip()
    p = params or {}
    if cid == "input_text":
        return False
    if cid in ("tap_element", "long_press_element", "multi_tap"):
        return p.get("x") is None or p.get("y") is None
    if cid == "swipe_element_to_element":
        return any(p.get(k) is None for k in ("from_x", "from_y", "to_x", "to_y"))
    return False


def _node_bounds_for(out: dict[str, Any], nodes: Any) -> list[Any] | None:
    """按文案/资源 id 在当前结构里找唯一节点，返回它的 bounds。"""
    if not nodes:
        return None
    target = out.get("target") if isinstance(out.get("target"), dict) else {}
    labels = [out.get("selector_text"), out.get("text"), target.get("name"),
              target.get("text"), target.get("content_desc")]
    labels += list(out.get("match") or ()) if isinstance(out.get("match"), (list, tuple)) else []
    labels = [str(v).strip() for v in labels if str(v or "").strip()]
    resource = str(target.get("resource_id") or target.get("id") or out.get("resource_id") or "").strip()
    for node in nodes:
        if not isinstance(node, dict) or node.get("cross_origin") or not node.get("visible", True):
            continue
        bounds = node.get("bounds")
        if not isinstance(bounds, (list, tuple)) or len(bounds) != 4:
            continue
        if resource and resource == str(node.get("resource_id") or ""):
            return list(bounds)
        texts = {str(node.get(k) or "").strip() for k in ("text", "content_desc", "label")}
        if any(label in texts for label in labels):
            return list(bounds)
    return None


def xy_from_bounds(out: dict[str, Any], width: int, height: int, nodes: Any = None) -> None:
    """模型给了 hierarchy 节点（bounds）却没给坐标时，用节点中心点补 x/y。

    bounds 是设备像素 [left, top, right, bottom]；x/y 走 0–1000 相对坐标。
    这条路必须能补：没有导航图时模型手里只有节点，再让它「重新给一次坐标」
    只会变成一次无坐标重试 + 一次判失败（cr-cb4bd3f86b7d 的 api-1-211）。
    """
    if out.get("x") is not None and out.get("y") is not None:
        return
    if not width or not height:
        return
    target = out.get("target") if isinstance(out.get("target"), dict) else {}
    bounds = target.get("bounds") or out.get("bounds") or _node_bounds_for(out, nodes)
    if not isinstance(bounds, (list, tuple)) or len(bounds) != 4:
        return
    try:
        left, top, right, bottom = (float(v) for v in bounds)
    except (TypeError, ValueError):
        return
    if right <= left or bottom <= top:
        return
    out["x"] = int(round(((left + right) / 2.0) / width * 1000))
    out["y"] = int(round(((top + bottom) / 2.0) / height * 1000))
    out.setdefault("point_policy", "coordinate")
    out["point_source"] = "bounds"


def assemble_execute_params(
    cap_id: str,
    params: dict[str, Any] | None,
    ctx: Any = None,
    *,
    width: int = 0,
    height: int = 0,
) -> dict[str, Any]:
    from mino_nexus.action_space.visual import apply_focus_input, bind_focus_ref
    out = dict(params or {})
    cid = str(cap_id or "").strip()
    if cid not in _POINT_CAPS and cid != "input_text":
        return out
    from mino_nexus.contract.coords import prepare_xy_params_for_execute
    from mino_nexus.action_space.scheme import execution_scheme as action_scheme
    from mino_nexus.contract.execution.ui import UiChannel, ui_channel_from_ctx

    xy_from_bounds(out, int(width or 0), int(height or 0),
                   getattr(ctx, "nav_hierarchy_nodes", None) if ctx is not None else None)
    prepare_xy_params_for_execute(out, int(width or 0), int(height or 0))
    scheme = action_scheme(ctx)
    channel = ui_channel_from_ctx(ctx)
    if out.get("locator_chain") and cid != "input_text":
        if channel == UiChannel.WEB:
            from mino_nexus.action_space.web import apply_web_point

            return apply_web_point(out)
        from mino_nexus.action_space.dom import apply_dom_point

        return apply_dom_point(out)
    if channel == UiChannel.WEB and cid in _POINT_CAPS and scheme != "dom":
        from mino_nexus.action_space.web import apply_web_point

        out = apply_web_point(out)
    if cid == "input_text" and channel == UiChannel.ANDROID:
        # Android 侧不存在"按节点写文本"的契约：Scout adb 只实现
        # current_focus + append（input_contract v2），dom 的 node 输入在原生端不成立。
        # 定位交给前一次 tap_element，这里只写当前焦点。
        from mino_nexus.action_space.visual import apply_focus_input, bind_focus_ref

        out = apply_focus_input(out)
        out["mode"] = "append"
        return bind_focus_ref(out, ctx)
    if scheme == "dom" and cid in ("tap_element", "input_text"):
        from mino_nexus.action_space.scheme import structure_target_nodes

        hits = structure_target_nodes(cid, out, ctx)
        if len(hits) == 1:
            node = hits[0]
            if node.get("node_ref"):
                out["node_ref"] = node["node_ref"]
                out["snapshot_id"] = node.get("snapshot_id", "")
            if out.get("locator_chain") and not out.get("fallback_xy"):
                from mino_nexus.navigation.geometry.ui_consent import tap_params_for_control

                nodes = getattr(ctx, "nav_hierarchy_nodes", None) if ctx is not None else None
                out.update(tap_params_for_control(node, list(nodes or [])))
    out = stamp_scheme_params(cid, out, scheme)
    if cid == "input_text" and scheme != "dom":
        from mino_nexus.action_space.visual import bind_focus_ref

        out = bind_focus_ref(out, ctx)
    return out
