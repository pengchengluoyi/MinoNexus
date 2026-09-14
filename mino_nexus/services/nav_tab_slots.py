"""底栏 Tab 槽位：支持纯图标 / 图文混合，顺序仅按几何从左到右（不用频次排序）。"""
from __future__ import annotations

import hashlib
import re
from typing import Any

_TAB_ICON_RE = re.compile(r"image|icon", re.I)
_LAYOUT_CLASS_RE = re.compile(r"(Layout|View|Widget|Frame|Group)$", re.I)


def _bounds(node: dict[str, Any]) -> tuple[int, int, int, int] | None:
    b = node.get("bounds") or []
    if isinstance(b, (list, tuple)) and len(b) >= 4:
        return int(b[0]), int(b[1]), int(b[2]), int(b[3])
    return None


def _slot_key(center_x: int, kind: str, label: str) -> str:
    raw = f"{kind}|{center_x}|{label}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:10]


def _is_layout_class_label(label: str) -> bool:
    val = str(label or "").strip()
    if not val:
        return True
    if _LAYOUT_CLASS_RE.search(val):
        return True
    if val in ("icon", "slot"):
        return False
    return False


def _node_tab_label(node: dict[str, Any]) -> tuple[str, str]:
    """返回 (label, kind) kind: text|icon|mixed"""
    text = str(node.get("text") or "").strip()
    desc = str(node.get("content_desc") or "").strip()
    rid = str(node.get("resource_id") or "").split("/")[-1].strip()
    cls = str(node.get("class") or "").split(".")[-1]
    is_icon = bool(_TAB_ICON_RE.search(cls) or _TAB_ICON_RE.search(rid))
    if text:
        return text, "mixed" if is_icon else "text"
    if desc:
        return desc, "mixed" if is_icon else "text"
    if is_icon or cls in ("ImageView", "AppCompatImageView"):
        return rid or cls or "icon", "icon"
    if rid:
        return rid, "text"
    return cls or "slot", "icon" if is_icon else "text"


def find_bottom_tab_slots(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """最底水平 Tab 行 → 槽位列表（含无文案图标槽），按 x 从左到右。"""
    from mino_nexus.services.nav_screen_layout import infer_tab_bar_band

    if not nodes:
        return []
    band = infer_tab_bar_band(nodes)
    sh = int(band.get("screen_h") or 0) or 1920
    y0 = max(int(band.get("band_top_px") or 0), int(sh * 0.84))
    y_floor = int(sh * 0.86)
    y_tol = 48

    band_nodes: list[tuple[int, int, int, int, str, str, dict[str, Any]]] = []
    for node in nodes or []:
        if not (node.get("clickable") or node.get("enabled", True)):
            continue
        b = _bounds(node)
        if not b:
            continue
        x1, y1n, x2, y2 = b
        h = y2 - y1n
        if h > 220 or h < 8:
            continue
        if y2 < y_floor:
            continue
        if y2 < y0 - 4:
            continue
        if y1n < y0 - 12:
            continue
        label, kind = _node_tab_label(node)
        if len(label) > 48:
            continue
        if _is_layout_class_label(label) and kind != "icon":
            continue
        if label in ("Button", "View", "slot"):
            continue
        band_nodes.append((x1, y1n, x2, y2, label, kind, node))

    if len(band_nodes) < 2:
        return []

    max_bottom = max(r[3] for r in band_nodes)
    row = [r for r in band_nodes if abs(r[3] - max_bottom) <= y_tol and abs(r[1] - y0) <= y_tol * 2]
    if len(row) < 2:
        row = band_nodes
    row = sorted(row, key=lambda r: (r[0] + r[2]) / 2.0)

    # x 方向聚类为槽位
    slots_raw: list[dict[str, Any]] = []
    for x1, y1n, x2, y2, label, kind, _node in row:
        cx = (x1 + x2) // 2
        merged = False
        for slot in slots_raw:
            if abs(int(slot["center_x"]) - cx) < max(48, (x2 - x1) // 2 + 24):
                slot["x1"] = min(int(slot["x1"]), x1)
                slot["x2"] = max(int(slot["x2"]), x2)
                slot["center_x"] = (int(slot["x1"]) + int(slot["x2"])) // 2
                if label not in slot["labels"]:
                    slot["labels"].append(label)
                slot["kinds"].add(kind)
                merged = True
                break
        if not merged:
            slots_raw.append(
                {
                    "x1": x1,
                    "x2": x2,
                    "y1": y1n,
                    "y2": y2,
                    "center_x": cx,
                    "labels": [label] if label else [],
                    "kinds": {kind},
                }
            )

    slots_raw.sort(key=lambda s: int(s["center_x"]))
    out: list[dict[str, Any]] = []
    for slot in slots_raw:
        labels = [str(l) for l in slot.get("labels") or [] if str(l).strip()]
        kinds = set(slot.get("kinds") or [])
        text_labels = [
            l
            for l in labels
            if l not in ("icon", "ImageView", "AppCompatImageView") and not _is_layout_class_label(l)
        ]
        if text_labels:
            primary = text_labels[0]
            kind = "mixed" if "icon" in kinds or len(labels) > 1 else "text"
        else:
            primary = labels[0] if labels else "icon"
            kind = "icon"
        display = primary
        if kind == "icon" and primary in ("icon", "ImageView", "AppCompatImageView", ""):
            display = "图标 Tab"
        elif len(text_labels) > 1:
            display = " · ".join(text_labels[:3])
        sid = _slot_key(int(slot["center_x"]), kind, primary)
        out.append(
            {
                "slot_id": sid,
                "label": primary,
                "display": display,
                "kind": kind,
                "center_x": int(slot["center_x"]),
                "bounds": [int(slot["x1"]), int(slot["y1"]), int(slot["x2"]), int(slot["y2"])],
                "parts": labels,
            }
        )
    return out


def tab_labels_from_slots(slots: list[dict[str, Any]]) -> list[str]:
    labels: list[str] = []
    seen: set[str] = set()
    for s in slots:
        pref = str(s.get("label") or "").strip()
        disp = str(s.get("display") or pref).strip()
        use = disp if pref in ("icon", "ImageView", "AppCompatImageView", "") else pref
        if use and use not in seen:
            labels.append(use)
            seen.add(use)
    return labels
