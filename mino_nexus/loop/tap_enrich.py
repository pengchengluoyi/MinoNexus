"""tap_element 参数补全：底栏槽位 / 双锚点 / 步骤引号文案（无文案控件也能落到 x/y）。"""
from __future__ import annotations

import re
from typing import Any

_QUOTE_RE = re.compile(r"[「『\"“]([^」』\"”]{1,16})[」』\"”]")
_TAB_HINT_RE = re.compile(r"tab|底栏|底部", re.I)


def _labeled_anchor(slot: dict[str, Any]) -> str:
    label = str(slot.get("label") or "").strip()
    if label in ("icon", "ImageView", "AppCompatImageView", "图标 Tab", ""):
        disp = str(slot.get("display") or "").strip()
        return disp if disp and disp != "图标 Tab" else ""
    return label


def _center_xy(bounds: Any) -> list[int] | None:
    if not isinstance(bounds, (list, tuple)) or len(bounds) < 4:
        return None
    from mino_nexus.loop.hierarchy_slots import int_list

    bb = int_list(bounds, 4)
    if bb[2] <= bb[0] or bb[3] <= bb[1]:
        return None
    return [(bb[0] + bb[2]) // 2, (bb[1] + bb[3]) // 2]


def _quoted_tokens(text: str) -> list[str]:
    out: list[str] = []
    for match in _QUOTE_RE.finditer(str(text or "")):
        token = str(match.group(1) or "").strip()
        if 1 <= len(token) <= 16 and token not in out:
            out.append(token)
    return out


def _hint_labels(params: dict[str, Any], hint: str) -> list[str]:
    labels: list[str] = []
    for key in ("selector_text", "text", "description", "label"):
        val = str(params.get(key) or "").strip().strip("「」\"'")
        if val and val not in labels:
            labels.append(val)
    for token in _quoted_tokens(hint):
        if token not in labels:
            labels.append(token)
    return labels[:8]


def _screen_wh(nodes: list[dict[str, Any]]) -> tuple[int, int]:
    from mino_nexus.services.nav_screen_layout import screen_size

    return screen_size(nodes)


def _milli(px: int, dim: int) -> int:
    if dim <= 0:
        return max(0, int(px))
    return max(0, min(1000, int(round(px / dim * 1000.0))))


def _apply_point(out: dict[str, Any], xy_px: list[int], *, screen_w: int, screen_h: int) -> None:
    if not xy_px or len(xy_px) < 2:
        return
    cx, cy = int(xy_px[0]), int(xy_px[1])
    out.setdefault("fallback_xy", [cx, cy])
    out.setdefault("x", _milli(cx, screen_w))
    out.setdefault("y", _milli(cy, screen_h))


def _promote_fallback_xy(out: dict[str, Any], *, screen_w: int, screen_h: int) -> None:
    raw = out.get("fallback_xy")
    if not isinstance(raw, (list, tuple)) or len(raw) < 2:
        return
    try:
        cx, cy = int(raw[0]), int(raw[1])
    except (TypeError, ValueError):
        return
    if cx > 1000 or cy > 1000:
        out.setdefault("x", _milli(cx, screen_w))
        out.setdefault("y", _milli(cy, screen_h))
    else:
        out.setdefault("x", max(0, min(1000, cx)))
        out.setdefault("y", max(0, min(1000, cy)))


def _node_score(node: dict[str, Any], label: str, *, prefer_bottom: bool, sw: int, sh: int) -> tuple[int, int] | None:
    from mino_nexus.loop.hierarchy_slots import int_list

    text = str(node.get("text") or "").strip()
    desc = str(node.get("content_desc") or "").strip()
    rid = str(node.get("resource_id") or "").split("/")[-1]
    exact = text == label or desc == label
    contains = (label in text) or (label in desc) or (label in rid)
    if not exact and not contains:
        return None
    b = int_list(node.get("bounds"), 4)
    if b[2] <= b[0] or b[3] <= b[1]:
        return None
    area = max(1, (b[2] - b[0]) * (b[3] - b[1]))
    if sw > 0 and sh > 0 and area > int(sw * sh * 0.45):
        return None
    cy = (b[1] + b[3]) / 2.0
    score = 0
    if exact:
        score += 100
    elif text.startswith(label) or desc.startswith(label):
        score += 40
    if node.get("clickable"):
        score += 20
    if prefer_bottom and sh > 0 and cy >= sh * 0.80:
        score += 50
    elif prefer_bottom and sh > 0 and cy < sh * 0.70:
        score -= 20
    return score, -area


def _match_node(nodes: list[dict[str, Any]], label: str, *, hint: str) -> dict[str, Any] | None:
    if not label or not nodes:
        return None
    sw, sh = _screen_wh(nodes)
    prefer_bottom = bool(_TAB_HINT_RE.search(hint or ""))
    ranked: list[tuple[int, int, dict[str, Any]]] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        scored = _node_score(node, label, prefer_bottom=prefer_bottom, sw=sw, sh=sh)
        if scored is None:
            continue
        ranked.append((scored[0], scored[1], node))
    if not ranked:
        return None
    ranked.sort(key=lambda row: (row[0], row[1]), reverse=True)
    return ranked[0][2]


def _match_slot(slots: list[dict[str, Any]], label: str) -> dict[str, Any] | None:
    if not label:
        return None
    for slot in slots or []:
        parts = [str(slot.get("label") or ""), str(slot.get("display") or "")]
        parts.extend(str(p) for p in (slot.get("parts") or []) if p)
        if any(label == str(p).strip() or label in str(p) for p in parts if str(p).strip()):
            return slot
    return None


def enrich_tap_params(
    params: dict[str, Any],
    nodes: list[dict[str, Any]],
    *,
    hint: str = "",
    extra_nodes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """发 EXECUTE 前补 selector / target / x,y。已有坐标不覆盖。"""
    out = dict(params or {})
    pool: list[dict[str, Any]] = [n for n in (nodes or []) if isinstance(n, dict)]
    for node in extra_nodes or []:
        if isinstance(node, dict):
            pool.append(node)
    sw, sh = _screen_wh(pool) if pool else (0, 0)
    from mino_nexus.services.nav_tab_slots import find_bottom_tab_slots

    slots = find_bottom_tab_slots(pool) if pool else []
    labels = _hint_labels(out, hint)

    idx_raw = out.get("tab_slot_index")
    if idx_raw is not None and not str(out.get("selector_text") or "").strip():
        try:
            idx = int(idx_raw)
        except (TypeError, ValueError):
            idx = -1
        if 0 <= idx < len(slots):
            slot = slots[idx]
            xy = _center_xy(slot.get("bounds") or [])
            if xy:
                _apply_point(out, xy, screen_w=sw, screen_h=sh)
            left = _labeled_anchor(slots[idx - 1]) if idx > 0 else ""
            right = _labeled_anchor(slots[idx + 1]) if idx + 1 < len(slots) else ""
            if left and right:
                out.setdefault("anchor_between", [left, right])
            elif left:
                out.setdefault("anchor_between", [left, ""])
            elif right:
                out.setdefault("anchor_between", ["", right])
            lab = _labeled_anchor(slot)
            if lab:
                out.setdefault("selector_text", lab)

    anchor = out.get("anchor_between")
    if (
        isinstance(anchor, (list, tuple))
        and len(anchor) >= 2
        and not str(out.get("selector_text") or "").strip()
        and out.get("x") is None
    ):
        left, right = str(anchor[0] or "").strip(), str(anchor[1] or "").strip()
        for i, slot in enumerate(slots):
            lab = _labeled_anchor(slot)
            if left and lab == left and i + 1 < len(slots):
                mid = slots[i + 1]
                if not right or _labeled_anchor(mid) == right or not _labeled_anchor(mid):
                    xy = _center_xy(mid.get("bounds") or [])
                    if xy:
                        _apply_point(out, xy, screen_w=sw, screen_h=sh)
                        out.setdefault("tab_slot_index", i + 1 if right else i)
                break

    if out.get("x") is None or out.get("y") is None:
        for label in labels:
            slot = _match_slot(slots, label)
            if slot is not None:
                xy = _center_xy(slot.get("bounds") or [])
                if xy:
                    _apply_point(out, xy, screen_w=sw, screen_h=sh)
                    out.setdefault("selector_text", label)
                    break
            hit = _match_node(pool, label, hint=hint)
            if hit is None:
                continue
            xy = _center_xy(hit.get("bounds") or [])
            if xy:
                _apply_point(out, xy, screen_w=sw, screen_h=sh)
                out.setdefault("selector_text", label)
                break

    _promote_fallback_xy(out, screen_w=sw, screen_h=sh)
    return out
