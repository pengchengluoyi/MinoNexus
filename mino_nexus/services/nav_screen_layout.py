"""屏面边界与线框布局：hierarchy 与 VLM 并行产出，按比例坐标。"""
from __future__ import annotations

import re
from typing import Any

_TOP_CHROME_RE = re.compile(
    r"status_?bar|navigationbar|action_bar|titlebar|toolbar|decor_content",
    re.I,
)
_BOTTOM_CHROME_RE = re.compile(
    r"tab_?bar|bottom_?nav|navigation_?bar|nav_bar|bottombar",
    re.I,
)


def _bounds(node: dict[str, Any]) -> tuple[int, int, int, int] | None:
    b = node.get("bounds") or []
    if isinstance(b, (list, tuple)) and len(b) >= 4:
        return int(b[0]), int(b[1]), int(b[2]), int(b[3])
    return None


def screen_size(nodes: list[dict[str, Any]]) -> tuple[int, int]:
    w = h = 0
    for node in nodes or []:
        b = _bounds(node)
        if b:
            w = max(w, b[2])
            h = max(h, b[3])
    return w or 1080, h or 1920


def sanitize_chrome(chrome: dict[str, Any] | None) -> dict[str, float]:
    if not isinstance(chrome, dict):
        return {"top": 0.06, "bottom": 0.88}
    top = float(chrome.get("top") or 0.06)
    bottom = float(chrome.get("bottom") or 0.88)
    if top > 0.22 or bottom < 0.55 or top >= bottom:
        if top > 0.5 and bottom > 0.85:
            top, bottom = 0.06, 0.88
        elif top >= bottom:
            top, bottom = 0.06, 0.88
    top = max(0.0, min(0.22, top))
    bottom = max(0.55, min(1.0, bottom))
    bottom = max(top + 0.1, bottom)
    return {"top": round(top, 4), "bottom": round(bottom, 4)}


def sanitize_wireframe(wf: dict[str, Any] | None) -> dict[str, Any]:
    out = dict(wf or {})
    out["chrome"] = sanitize_chrome(out.get("chrome"))
    regions = out.get("regions")
    out["regions"] = list(regions) if isinstance(regions, list) else []
    if "screen" not in out:
        out["screen"] = {"w": 1080, "h": 1920}
    return out


def infer_content_bands(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    screen_w, screen_h = screen_size(nodes)
    top_candidates: list[int] = []
    bottom_candidates: list[int] = []

    for node in nodes or []:
        b = _bounds(node)
        if not b:
            continue
        x1, y1, x2, y2 = b
        rid = str(node.get("resource_id") or "")
        cls = str(node.get("class") or "")
        hint = f"{rid} {cls}"
        if _TOP_CHROME_RE.search(hint):
            top_candidates.append(y2)
        if _BOTTOM_CHROME_RE.search(hint):
            bottom_candidates.append(y1)

    content_top_px = max(top_candidates) if top_candidates else int(screen_h * 0.06)
    content_bottom_px = min(bottom_candidates) if bottom_candidates else int(screen_h * 0.88)
    content_top_px = max(0, min(content_top_px, screen_h - 1))
    content_bottom_px = max(content_top_px + 1, min(content_bottom_px, screen_h))

    return {
        "screen_w": screen_w,
        "screen_h": screen_h,
        "content_top": round(content_top_px / screen_h, 4),
        "content_bottom": round(content_bottom_px / screen_h, 4),
        "content_top_px": content_top_px,
        "content_bottom_px": content_bottom_px,
        "source": "hierarchy",
    }


def _x_overlap(x1a: int, x2a: int, x1b: int, x2b: int) -> int:
    return max(0, min(x2a, x2b) - max(x1a, x1b))


def _is_clickable_button(node: dict[str, Any]) -> bool:
    if node.get("clickable"):
        return True
    cls = str(node.get("class") or "").lower()
    return "button" in cls


def _button_items(
    nodes: list[dict[str, Any]],
    *,
    max_text_len: int = 16,
) -> list[tuple[int, int, int, int, str]]:
    """(x1, y1, x2, y2, text) 可点击短文案控件。"""
    out: list[tuple[int, int, int, int, str]] = []
    for node in nodes or []:
        if not _is_clickable_button(node):
            continue
        text = str(node.get("text") or node.get("content_desc") or "").strip()
        if not text or len(text) > max_text_len:
            continue
        b = _bounds(node)
        if not b:
            continue
        out.append((b[0], b[1], b[2], b[3], text))
    return out


def _y_cluster_tolerance(heights: list[int]) -> int:
    if not heights:
        return 32
    ordered = sorted(heights)
    mid = ordered[len(ordered) // 2]
    return max(24, mid // 2)


def _is_horizontal_row(items: list[tuple[int, int, int, int, str]]) -> bool:
    if len(items) < 2:
        return False
    bottoms = [row[3] for row in items]
    heights = [row[3] - row[1] for row in items]
    tol = _y_cluster_tolerance(heights)
    if max(bottoms) - min(bottoms) > tol:
        return False
    widths = [row[2] - row[0] for row in items]
    xs = [(row[0] + row[2]) / 2.0 for row in items]
    if max(xs) - min(xs) <= max(widths):
        return False
    return not _is_vertical_stack(items)


def _is_vertical_stack(items: list[tuple[int, int, int, int, str]]) -> bool:
    if len(items) < 2:
        return False
    ordered = sorted(items, key=lambda row: row[1])
    for prev, cur in zip(ordered, ordered[1:]):
        w_prev = prev[2] - prev[0]
        w_cur = cur[2] - cur[0]
        min_w = max(1, min(w_prev, w_cur))
        if _x_overlap(prev[0], prev[2], cur[0], cur[2]) < min_w // 2:
            return False
        gap = cur[1] - prev[3]
        if gap > max(prev[3] - prev[1], cur[3] - cur[1]) * 2:
            return False
    return True


def _cluster_by_bottom_edge(
    items: list[tuple[int, int, int, int, str]],
) -> list[list[tuple[int, int, int, int, str]]]:
    if not items:
        return []
    heights = [row[3] - row[1] for row in items]
    tol = _y_cluster_tolerance(heights)
    ordered = sorted(items, key=lambda row: row[3])
    clusters: list[list[tuple[int, int, int, int, str]]] = []
    cur: list[tuple[int, int, int, int, str]] = [ordered[0]]
    ref = ordered[0][3]
    for row in ordered[1:]:
        if abs(row[3] - ref) <= tol:
            cur.append(row)
        else:
            clusters.append(cur)
            cur = [row]
            ref = row[3]
    clusters.append(cur)
    return clusters


def find_bottom_horizontal_tab_row(
    nodes: list[dict[str, Any]],
    *,
    max_text_len: int = 16,
) -> list[str]:
    """最底部水平 Tab 行：同 y 带 + 左右分布，不用屏幕百分比。"""
    items = _button_items(nodes, max_text_len=max_text_len)
    if len(items) < 2:
        return []
    bands = infer_content_bands(nodes)
    content_bottom = int(bands.get("content_bottom_px") or 0)
    horizontal_rows: list[list[tuple[int, int, int, int, str]]] = []
    for cluster in _cluster_by_bottom_edge(items):
        if _is_horizontal_row(cluster):
            horizontal_rows.append(cluster)
    if not horizontal_rows:
        return []
    # 优先 content_bottom 以下的行；否则取最底水平行
    below = [row for row in horizontal_rows if row and row[0][1] >= content_bottom]
    chosen = max(below or horizontal_rows, key=lambda row: max(r[3] for r in row))
    return [row[4] for row in sorted(chosen, key=lambda r: r[0])]


def is_modal_button_stack(nodes: list[dict[str, Any]]) -> bool:
    """竖排模态按钮（权限弹窗等）：同列叠放，且不是最底水平 Tab 行。"""
    items = _button_items(nodes)
    if len(items) < 2:
        return False
    tab_row_labels = set(find_bottom_horizontal_tab_row(nodes))
    tab_items = [row for row in items if row[4] in tab_row_labels] if tab_row_labels else []
    tab_bottom = max((row[3] for row in tab_items), default=0)
    for cluster in _cluster_by_bottom_edge(items):
        if len(cluster) < 2 or not _is_vertical_stack(cluster):
            continue
        if tab_bottom and min(row[1] for row in cluster) >= tab_bottom:
            continue
        widths = [row[2] - row[0] for row in cluster]
        sw, _ = screen_size(nodes)
        if sw:
            margin_left = min(row[0] for row in cluster)
            margin_right = sw - max(row[2] for row in cluster)
            inner = max(1, sw - margin_left - margin_right)
            if max(widths) >= inner:
                return True
        if not tab_row_labels:
            return True
    return False


def infer_tab_bar_band(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    """底栏 Tab 带像素范围：hierarchy chrome rid 或最底水平行。"""
    bands = infer_content_bands(nodes)
    sw = int(bands.get("screen_w") or 0)
    sh = int(bands.get("screen_h") or 0)
    labels = find_bottom_horizontal_tab_row(nodes)
    items = _button_items(nodes)
    row_items = [row for row in items if row[4] in set(labels)] if labels else []
    if row_items:
        band_top = min(row[1] for row in row_items)
        band_bottom = max(row[3] for row in row_items)
        source = "horizontal_row"
    else:
        band_top = int(bands.get("content_bottom_px") or 0)
        band_bottom = sh
        source = "content_bottom_px"
    return {
        "screen_w": sw,
        "screen_h": sh,
        "band_top_px": max(0, band_top),
        "band_bottom_px": max(band_top + 1, band_bottom or sh),
        "tab_labels": labels,
        "source": source,
        **bands,
    }


def _norm_rect(x1: int, y1: int, x2: int, y2: int, sw: int, sh: int) -> dict[str, float]:
    return {
        "x": round(x1 / sw, 4),
        "y": round(y1 / sh, 4),
        "w": round(max(0, x2 - x1) / sw, 4),
        "h": round(max(0, y2 - y1) / sh, 4),
    }


def _class_short(node: dict[str, Any]) -> str:
    return str(node.get("class") or "").split(".")[-1]


def _is_image_node(node: dict[str, Any]) -> bool:
    cls = _class_short(node)
    return "Image" in cls or "Icon" in cls


def _node_label(node: dict[str, Any], *, index: int) -> str:
    text = str(node.get("text") or "").strip()
    if text:
        return text
    desc = str(node.get("content_desc") or "").strip()
    if desc:
        return desc
    rid = str(node.get("resource_id") or "").split("/")[-1].strip()
    if rid:
        return rid
    cls = _class_short(node)
    if cls:
        return cls
    return f"#{index}"


def wireframe_from_hierarchy(
    nodes: list[dict[str, Any]],
    *,
    bands: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """hierarchy 每个节点单独一块，不合并、不截断、不按尺寸过滤。"""
    b = bands or infer_content_bands(nodes)
    sw = int(b.get("screen_w") or 1080)
    sh = int(b.get("screen_h") or 1920)

    regions: list[dict[str, Any]] = []
    for i, node in enumerate(nodes or []):
        box = _bounds(node)
        if not box:
            continue
        x1, y1, x2, y2 = box
        w, h = x2 - x1, y2 - y1
        if w <= 0 or h <= 0:
            continue
        cls = _class_short(node)
        is_image = _is_image_node(node)
        regions.append(
            {
                "id": f"n{i}",
                "source": "hierarchy",
                "label": _node_label(node, index=i),
                "clickable": bool(node.get("clickable")),
                "is_image": is_image,
                "class_name": cls,
                "rect": _norm_rect(x1, y1, x2, y2, sw, sh),
                "bounds_px": [x1, y1, x2, y2],
            }
        )

    chrome = sanitize_chrome({"top": b.get("content_top"), "bottom": b.get("content_bottom")})
    return {
        "chrome": chrome,
        "screen": {"w": sw, "h": sh},
        "regions": regions,
        "source": "hierarchy",
    }


def normalize_vision_layout(raw: dict[str, Any], *, screen_w: int, screen_h: int) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    chrome = raw.get("chrome") if isinstance(raw.get("chrome"), dict) else {}
    top = float(chrome.get("top") or raw.get("content_top") or 0.06)
    bottom = float(chrome.get("bottom") or raw.get("content_bottom") or 0.88)
    top = max(0.0, min(1.0, top))
    bottom = max(top + 0.01, min(1.0, bottom))

    regions: list[dict[str, Any]] = []
    for i, item in enumerate(raw.get("regions") or []):
        if not isinstance(item, dict):
            continue
        rect = item.get("rect") if isinstance(item.get("rect"), dict) else item
        try:
            x = float(rect.get("x") or 0)
            y = float(rect.get("y") or 0)
            w = float(rect.get("w") or rect.get("width") or 0)
            h = float(rect.get("h") or rect.get("height") or 0)
        except (TypeError, ValueError):
            continue
        if w <= 0 or h <= 0:
            continue
        regions.append(
            {
                "id": str(item.get("id") or f"v{i}"),
                "source": "vision",
                "label": str(item.get("label") or item.get("role") or item.get("id") or f"v{i}"),
                "clickable": bool(item.get("clickable")),
                "is_image": bool(item.get("is_image")),
                "rect": {
                    "x": round(max(0, min(1, x)), 4),
                    "y": round(max(0, min(1, y)), 4),
                    "w": round(max(0, min(1, w)), 4),
                    "h": round(max(0, min(1, h)), 4),
                },
            }
        )

    chrome = sanitize_chrome({"top": top, "bottom": bottom})
    return {
        "chrome": chrome,
        "screen": {"w": screen_w, "h": screen_h},
        "regions": regions,
        "source": "vision",
    }


def stack_layout_regions(
    hierarchy: dict[str, Any] | None,
    vision: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """hierarchy + vision 原样拼接，不去重、不合并。"""
    out: list[dict[str, Any]] = []
    out.extend(list((hierarchy or {}).get("regions") or []))
    out.extend(list((vision or {}).get("regions") or []))
    return out


def merge_layout_views(
    hierarchy: dict[str, Any] | None,
    vision: dict[str, Any] | None,
) -> dict[str, Any]:
    h = hierarchy or {}
    v = vision or {}
    h_chrome = h.get("chrome") or {}
    v_chrome = v.get("chrome") or {}
    disagree = False
    if h_chrome and v_chrome:
        disagree = abs(float(h_chrome.get("top") or 0) - float(v_chrome.get("top") or 0)) > 0.04
        disagree = disagree or abs(float(h_chrome.get("bottom") or 1) - float(v_chrome.get("bottom") or 1)) > 0.04

    chrome = h_chrome or v_chrome
    if h_chrome and v_chrome and not disagree:
        chrome = {
            "top": round((float(h_chrome.get("top") or 0) + float(v_chrome.get("top") or 0)) / 2, 4),
            "bottom": round((float(h_chrome.get("bottom") or 1) + float(v_chrome.get("bottom") or 1)) / 2, 4),
        }
    chrome = sanitize_chrome(chrome)

    regions = stack_layout_regions(h, v)
    screen = h.get("screen") or v.get("screen") or {}
    return {
        "chrome": chrome,
        "screen": screen,
        "regions": regions,
        "sources": {
            "hierarchy": bool(h.get("regions")),
            "vision": bool(v.get("regions")),
            "fused": False,
            "chrome_disagree": disagree,
        },
    }
