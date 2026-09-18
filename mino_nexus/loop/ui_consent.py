"""协议勾选：只认几何/控件结构，不猜 App 文案。

长文案左侧的小可勾选控件 = 同意框。点文案本身通常不会勾上。
"""
from __future__ import annotations

from typing import Any, Optional

from mino_nexus.loop.hierarchy_slots import int_list, node_flag

LONG_LABEL_MIN_CHARS = 8
SMALL_MAX_PX = 96
SMALL_EDGE_RATIO = 0.12
_ROW_OVERLAP = 0.70
_LEFT_SLACK_PX = 24


def _bounds(node: dict[str, Any]) -> list[int]:
    return int_list(node.get("bounds"), 4)


def _size(bounds: list[int]) -> tuple[int, int]:
    return max(0, bounds[2] - bounds[0]), max(0, bounds[3] - bounds[1])


def _center(bounds: list[int]) -> tuple[float, float]:
    return (bounds[0] + bounds[2]) / 2.0, (bounds[1] + bounds[3]) / 2.0


def _screen_wh(nodes: list[dict[str, Any]]) -> tuple[int, int]:
    max_x = 0
    max_y = 0
    for node in nodes or []:
        b = _bounds(node)
        max_x = max(max_x, b[2])
        max_y = max(max_y, b[3])
    return max_x, max_y


def _label_text(node: dict[str, Any]) -> str:
    text = str(node.get("text") or "").strip()
    if text:
        return text
    return str(node.get("content_desc") or "").strip()


def is_long_label(node: dict[str, Any]) -> bool:
    text = _label_text(node)
    if len(text) < LONG_LABEL_MIN_CHARS:
        return False
    b = _bounds(node)
    w, h = _size(b)
    if w <= 0 or h <= 0:
        return False
    return w >= h * 2


def is_small_control(node: dict[str, Any], *, sw: int, sh: int) -> bool:
    b = _bounds(node)
    w, h = _size(b)
    if w <= 0 or h <= 0:
        return False
    max_edge = SMALL_MAX_PX
    if sw > 0 and sh > 0:
        max_edge = min(SMALL_MAX_PX, max(24, int(min(sw, sh) * SMALL_EDGE_RATIO)))
    if max(w, h) > int(max_edge * 1.6):
        return False
    cls = str(node.get("class") or "").lower()
    if node_flag(node, "checkable") or "checkbox" in cls or "switch" in cls or ".check" in cls:
        return True
    if node.get("clickable") and max(w, h) <= max_edge:
        aspect = max(w, h) / max(1, min(w, h))
        return aspect <= 1.8
    return False


def _same_row(a: dict[str, Any], b: dict[str, Any]) -> bool:
    ba, bb = _bounds(a), _bounds(b)
    _, cay = _center(ba)
    _, cby = _center(bb)
    ha = max(1, ba[3] - ba[1])
    hb = max(1, bb[3] - bb[1])
    return abs(cay - cby) <= max(ha, hb, 24) * _ROW_OVERLAP


def _control_left_of_label(ctrl: dict[str, Any], label: dict[str, Any]) -> bool:
    bc, bl = _bounds(ctrl), _bounds(label)
    cw, _ = _size(bc)
    lw, _ = _size(bl)
    if bc[2] <= bl[0] + _LEFT_SLACK_PX:
        return True
    return bc[0] < bl[0] and cw < lw / 2


def find_consent_control(nodes: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """长文案左侧的小方框。已勾选则仍返回该控件（调用方决定是否跳过）。"""
    pool = [n for n in (nodes or []) if isinstance(n, dict)]
    if not pool:
        return None
    sw, sh = _screen_wh(pool)
    labels = [n for n in pool if is_long_label(n)]
    controls = [n for n in pool if is_small_control(n, sw=sw, sh=sh)]
    best: tuple[int, dict[str, Any]] | None = None
    for label in labels:
        for ctrl in controls:
            if ctrl is label:
                continue
            if not _same_row(ctrl, label):
                continue
            if not _control_left_of_label(ctrl, label):
                continue
            score = 0
            if node_flag(ctrl, "checkable"):
                score += 40
            if ctrl.get("clickable"):
                score += 10
            if node_flag(ctrl, "checked"):
                score += 5
            if best is None or score > best[0]:
                best = (score, ctrl)
    return None if best is None else best[1]


def any_focused_input(nodes: list[dict[str, Any]]) -> bool:
    for node in nodes or []:
        if not isinstance(node, dict):
            continue
        if node_flag(node, "focused"):
            return True
        cls = str(node.get("class") or "").lower()
        if "edittext" in cls and node_flag(node, "focused"):
            return True
    return False


def _node_center_xy(node: dict[str, Any]) -> list[int] | None:
    center = int_list(node.get("center"), 2)
    if center[0] > 0 or center[1] > 0:
        return [center[0], center[1]]
    b = _bounds(node)
    w, h = _size(b)
    if w <= 0 or h <= 0:
        return None
    return [(b[0] + b[2]) // 2, (b[1] + b[3]) // 2]


def _hit_node(params: dict[str, Any], nodes: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    labels: list[str] = []
    for key in ("selector_text", "text", "description", "label"):
        val = str(params.get(key) or "").strip()
        if val and val not in labels:
            labels.append(val)
    for label in labels:
        for node in nodes or []:
            if not isinstance(node, dict):
                continue
            blob = f"{node.get('text') or ''} {node.get('content_desc') or ''}"
            if label and label in blob:
                return node
    try:
        x = int(params.get("x"))
        y = int(params.get("y"))
    except (TypeError, ValueError):
        raw = params.get("fallback_xy")
        if isinstance(raw, (list, tuple)) and len(raw) >= 2:
            try:
                x, y = int(raw[0]), int(raw[1])
            except (TypeError, ValueError):
                return None
        else:
            return None
    # 千分比坐标不在这里换算；仅像素点落在 bounds 内才认。
    if x <= 1000 and y <= 1000:
        return None
    for node in nodes or []:
        if not isinstance(node, dict):
            continue
        b = _bounds(node)
        if b[0] <= x <= b[2] and b[1] <= y <= b[3]:
            return node
    return None


def retarget_tap_to_consent_control(
    params: dict[str, Any],
    nodes: list[dict[str, Any]],
) -> dict[str, Any]:
    """若点击目标是长文案标签，改落到左侧小勾选框。已有精确小控件则不改。"""
    out = dict(params or {})
    pool = [n for n in (nodes or []) if isinstance(n, dict)]
    if not pool:
        return out
    sw, sh = _screen_wh(pool)
    hit = _hit_node(out, pool)
    if hit is None:
        return out
    if is_small_control(hit, sw=sw, sh=sh):
        return out
    if not is_long_label(hit):
        return out
    ctrl = find_consent_control(pool)
    if ctrl is None:
        return out
    xy = _node_center_xy(ctrl)
    if not xy:
        return out
    out["fallback_xy"] = xy
    if sw > 0 and sh > 0:
        out["x"] = max(0, min(1000, int(round(xy[0] / sw * 1000.0))))
        out["y"] = max(0, min(1000, int(round(xy[1] / sh * 1000.0))))
    else:
        out["x"] = xy[0]
        out["y"] = xy[1]
    if not str(out.get("selector_text") or "").strip():
        rid = str(ctrl.get("resource_id") or "").split("/")[-1]
        if rid:
            out["selector_text"] = rid
    out["consent_retarget"] = True
    return out


def tap_params_for_control(node: dict[str, Any], nodes: list[dict[str, Any]]) -> dict[str, Any]:
    xy = _node_center_xy(node) or [0, 0]
    sw, sh = _screen_wh(nodes)
    out: dict[str, Any] = {"fallback_xy": xy}
    if sw > 0 and sh > 0:
        out["x"] = max(0, min(1000, int(round(xy[0] / sw * 1000.0))))
        out["y"] = max(0, min(1000, int(round(xy[1] / sh * 1000.0))))
    else:
        out["x"], out["y"] = xy[0], xy[1]
    rid = str(node.get("resource_id") or "").split("/")[-1]
    text = _label_text(node)
    if rid:
        out["selector_text"] = rid
    elif text:
        out["selector_text"] = text[:24]
    return out
