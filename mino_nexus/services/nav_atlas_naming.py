"""Screen Atlas 页面展示名：侧边栏选中态 + 内容顶栏，不依赖 Tab 分桶。"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any

_SIDE_ORDER = ("bottom", "top", "left", "right")


def _compact(label: str) -> str:
    from mino_nexus.services.nav_synthesis import _is_bad_tab_slot_label

    raw = str(label or "").strip()
    if _is_bad_tab_slot_label(raw):
        return "页面"
    val = re.sub(r"\d+", "", raw)
    val = re.sub(r"\s+", "", val)
    if not val or "%" in val or len(val) <= 1:
        return "页面"
    return val[:24]


def _screen_h(nodes: list[dict[str, Any]]) -> int:
    mx = 1920
    for n in nodes or []:
        b = n.get("bounds") or []
        if isinstance(b, (list, tuple)) and len(b) >= 4:
            mx = max(mx, int(b[3]))
    return mx


def _band_selected_label(
    turn: dict[str, Any],
    *,
    band: str,
    y_tab: int,
    known_labels: list[str],
) -> str:
    """底/顶/左/右栏：有选中态则返回该槽文案，否则空。"""
    nodes = turn.get("nodes") or []
    if band == "bottom":
        from mino_nexus.services.nav_synthesis import _bottom_tab_node_states, _tab_bar_band

        band_top, _ = _tab_bar_band([turn])
        states = _bottom_tab_node_states(turn, known_labels, band_top=band_top)
        for label in known_labels:
            row = states.get(label) or ()
            if not row:
                continue
            sel, chk = bool(row[0]), bool(row[1])
            rid = str(row[3] if len(row) > 3 else "").lower()
            if sel or chk or any(x in rid for x in ("selected", "checked", "active")):
                return label
        return ""
    sh = _screen_h(nodes)
    if band == "top":
        y_max = max(72, int(sh * 0.14))
    elif band == "left":
        y_max = sh
    elif band == "right":
        y_max = sh
    else:
        return ""

    candidates: list[tuple[int, str]] = []
    for node in nodes:
        b = node.get("bounds") or []
        if not isinstance(b, (list, tuple)) or len(b) < 4:
            continue
        x1, y1, x2, y2 = int(b[0]), int(b[1]), int(b[2]), int(b[3])
        text = str(node.get("text") or node.get("content_desc") or "").strip()
        if not text or len(text) > 24:
            continue
        rid = str(node.get("resource_id") or "").lower()
        selected = any(x in rid for x in ("selected", "checked", "active", "current"))
        if band == "top" and y2 > y_max:
            continue
        if band == "left" and x1 > int(sh * 0.22):
            continue
        if band == "right" and x2 < int(sh * 0.78):
            continue
        if selected:
            candidates.append((y1, text))
    if not candidates:
        return ""
    candidates.sort(key=lambda t: t[0])
    return candidates[0][1]


def sidebar_selections(
    turn: dict[str, Any],
    *,
    y_tab: int,
    known_labels: list[str],
) -> dict[str, str]:
    out: dict[str, str] = {}
    for side in _SIDE_ORDER:
        val = _band_selected_label(turn, band=side, y_tab=y_tab, known_labels=known_labels)
        if val:
            out[side] = val
    return out


def topmost_skeleton_title(
    turn: dict[str, Any],
    wireframe: dict[str, Any] | None,
    *,
    y_tab: int,
    skip: set[str],
) -> str:
    """无侧栏选中时：骨架线框中最靠上（y 最小）的可读文案。"""
    from mino_nexus.services.nav_layout import is_status_bar_chrome_text, is_volatile_text

    best_y = 2.0
    best_lbl = ""
    if isinstance(wireframe, dict):
        for r in wireframe.get("regions") or []:
            if not isinstance(r, dict):
                continue
            lbl = str(r.get("label") or "").strip()
            if not lbl or lbl in skip or lbl in ("返回", "Back"):
                continue
            if is_volatile_text(lbl) or is_status_bar_chrome_text(lbl):
                continue
            if lbl.endswith("View") and lbl[0].isupper() and " " not in lbl:
                continue
            if lbl in ("ImageView", "FrameLayout", "View", "icon", "slot"):
                continue
            if len(lbl) < 2 or len(lbl) > 32:
                continue
            y = float((r.get("rect") or {}).get("y") or 1.0)
            if y < best_y:
                best_y = y
                best_lbl = lbl
    if best_lbl:
        return best_lbl
    return _top_content_title(turn, y_tab=y_tab, skip=skip)


def _top_content_title(
    turn: dict[str, Any],
    *,
    y_tab: int,
    skip: set[str],
) -> str:
    from mino_nexus.services.nav_layout import (
        filter_app_chrome_texts,
        is_status_bar_chrome_text,
        is_volatile_text,
        stable_chrome_texts,
    )

    chrome = filter_app_chrome_texts(
        stable_chrome_texts(turn, y_tab_max=y_tab or 9999, exclude=skip)
    )
    for text in chrome:
        val = str(text or "").strip()
        if not val or val in skip or is_volatile_text(val) or is_status_bar_chrome_text(val):
            continue
        if val in ("返回", "Back"):
            continue
        if 2 <= len(val) <= 32:
            return val
    nodes = turn.get("nodes") or []
    sh = _screen_h(nodes)
    top_limit = max(120, int(sh * 0.12))
    rows: list[tuple[int, str]] = []
    for node in nodes:
        b = node.get("bounds") or []
        if not isinstance(b, (list, tuple)) or len(b) < 4:
            continue
        y1, y2 = int(b[1]), int(b[3])
        if y1 > top_limit:
            continue
        text = str(node.get("text") or node.get("content_desc") or "").strip()
        if not text or text in skip or len(text) > 32:
            continue
        if is_volatile_text(text) or is_status_bar_chrome_text(text):
            continue
        rows.append((y1, text))
    if not rows:
        return ""
    rows.sort(key=lambda t: t[0])
    return rows[0][1]


def _compose_sidebar_title(side: dict[str, str]) -> str:
    bottom = str(side.get("bottom") or "").strip()
    top = str(side.get("top") or "").strip()
    left = str(side.get("left") or "").strip()
    right = str(side.get("right") or "").strip()
    if bottom and top:
        return f"{_compact(bottom)}-{_compact(top)}"
    parts = [side[k] for k in _SIDE_ORDER if side.get(k)]
    if len(parts) >= 2:
        return "-".join(_compact(p) for p in parts[:3])
    if parts:
        return _compact(parts[0])
    return ""


def _hint_from_turn(turn: dict[str, Any]) -> tuple[str, str, str]:
    """(doc, case, llm) 弱信号，来自采集 meta。"""
    doc = str(turn.get("page_doc_name") or turn.get("doc_page_name") or "").strip()
    case = str(turn.get("case_page_name") or turn.get("nav_goal_label") or "").strip()
    llm = str(turn.get("llm_page_name") or turn.get("page_name") or "").strip()
    cid = str(turn.get("case_id") or "")
    if not case and cid:
        case = cid
    return doc, case, llm


def page_label_from_turn(
    turn: dict[str, Any],
    *,
    y_tab: int,
    known_labels: list[str],
    user_display_name: str = "",
    wireframe: dict[str, Any] | None = None,
) -> str:
    """单帧推导展示名（3.1–3.4）。"""
    if str(user_display_name or "").strip():
        return _compact(user_display_name)
    skip = {str(x).strip() for x in known_labels if str(x).strip()} | {"返回", "Back"}
    side = sidebar_selections(turn, y_tab=y_tab, known_labels=known_labels)
    composed = _compose_sidebar_title(side)
    if composed:
        return composed
    top = topmost_skeleton_title(turn, wireframe, y_tab=y_tab, skip=skip)
    if top:
        return _compact(top)
    doc, case, llm = _hint_from_turn(turn)
    for cand in (doc, case, llm):
        if cand:
            return _compact(cand)
    return ""


def merge_cluster_display_name(
    labels: list[str],
    *,
    user_display_name: str = "",
    min_ratio: float = 0.35,
) -> str:
    """多帧投票更新展示名；人工编辑优先。"""
    if str(user_display_name or "").strip():
        return _compact(user_display_name)
    clean = [_compact(x) for x in labels if str(x or "").strip()]
    if not clean:
        return ""
    counts = Counter(clean)
    best, n = counts.most_common(1)[0]
    if n >= max(1, int(len(clean) * min_ratio)):
        return best
    return clean[-1]
