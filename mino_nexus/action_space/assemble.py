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

    prepare_xy_params_for_execute(out, int(width or 0), int(height or 0))
    scheme = action_scheme(ctx)
    channel = ui_channel_from_ctx(ctx)
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
        if len(hits) == 1 and hits[0].get("node_ref"):
            out["node_ref"] = hits[0]["node_ref"]
            out["snapshot_id"] = hits[0].get("snapshot_id", "")
    out = stamp_scheme_params(cid, out, scheme)
    if cid == "input_text" and scheme != "dom":
        from mino_nexus.action_space.visual import bind_focus_ref

        out = bind_focus_ref(out, ctx)
    return out
