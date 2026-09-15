"""tap_element 参数补全：底栏槽位 / 双锚点（无文案控件）。"""
from __future__ import annotations

from typing import Any


def _labeled_anchor(slot: dict[str, Any]) -> str:
    label = str(slot.get("label") or "").strip()
    if label in ("icon", "ImageView", "AppCompatImageView", "图标 Tab", ""):
        disp = str(slot.get("display") or "").strip()
        return disp if disp and disp != "图标 Tab" else ""
    return label


def enrich_tap_params(params: dict[str, Any], nodes: list[dict[str, Any]]) -> dict[str, Any]:
    """在发 EXECUTE 前补全 tap_element 的定位字段（不覆盖已有 selector_text）。"""
    out = dict(params or {})
    if not nodes:
        return out
    from mino_nexus.services.nav_tab_slots import find_bottom_tab_slots

    slots = find_bottom_tab_slots(nodes)
    if not slots:
        return out

    idx_raw = out.get("tab_slot_index")
    if idx_raw is not None and not str(out.get("selector_text") or "").strip():
        try:
            idx = int(idx_raw)
        except (TypeError, ValueError):
            idx = -1
        if 0 <= idx < len(slots):
            slot = slots[idx]
            bounds = slot.get("bounds") or []
            if isinstance(bounds, (list, tuple)) and len(bounds) >= 4:
                cx = (int(bounds[0]) + int(bounds[2])) // 2
                cy = (int(bounds[1]) + int(bounds[3])) // 2
                out.setdefault("fallback_xy", [cx, cy])
            left = _labeled_anchor(slots[idx - 1]) if idx > 0 else ""
            right = _labeled_anchor(slots[idx + 1]) if idx + 1 < len(slots) else ""
            if left and right:
                out.setdefault("anchor_between", [left, right])
            elif left:
                out.setdefault("anchor_between", [left, ""])
            elif right:
                out.setdefault("anchor_between", ["", right])

    anchor = out.get("anchor_between")
    if (
        isinstance(anchor, (list, tuple))
        and len(anchor) >= 2
        and not str(out.get("selector_text") or "").strip()
        and not out.get("fallback_xy")
    ):
        left, right = str(anchor[0] or "").strip(), str(anchor[1] or "").strip()
        for i, slot in enumerate(slots):
            lab = _labeled_anchor(slot)
            if left and lab == left and i + 1 < len(slots):
                mid = slots[i + 1]
                if not right or _labeled_anchor(mid) == right or not _labeled_anchor(mid):
                    bounds = mid.get("bounds") or []
                    if isinstance(bounds, (list, tuple)) and len(bounds) >= 4:
                        cx = (int(bounds[0]) + int(bounds[2])) // 2
                        cy = (int(bounds[1]) + int(bounds[3])) // 2
                        out.setdefault("fallback_xy", [cx, cy])
                        out.setdefault("tab_slot_index", i + 1 if right else i)
                break
    return out
