"""agent-decide v10：VLM hierarchy 与 Scout nodes 合并、landmark 辅信号。"""
from __future__ import annotations

from typing import Any


def _iou(a: list[int], b: list[int]) -> float:
    if len(a) < 4 or len(b) < 4:
        return 0.0
    ax1, ay1, ax2, ay2 = int(a[0]), int(a[1]), int(a[2]), int(a[3])
    bx1, by1, bx2, by2 = int(b[0]), int(b[1]), int(b[2]), int(b[3])
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    if ix2 <= ix1 or iy2 <= iy1:
        return 0.0
    inter = (ix2 - ix1) * (iy2 - iy1)
    area_a = max(1, (ax2 - ax1) * (ay2 - ay1))
    area_b = max(1, (bx2 - bx1) * (by2 - by1))
    return inter / (area_a + area_b - inter)


def merge_vlm_into_nodes(
    scout_nodes: list[dict[str, Any]],
    vlm_nodes: list[dict[str, Any]],
    *,
    iou_min: float = 0.35,
) -> list[dict[str, Any]]:
    """冲突以 Scout 坐标为准，VLM 补 text/desc/clickable。"""
    base = [dict(n) for n in (scout_nodes or []) if isinstance(n, dict)]
    if not vlm_nodes:
        return base
    if not base:
        return [dict(n) for n in vlm_nodes if isinstance(n, dict)]
    out = list(base)
    for vn in vlm_nodes:
        if not isinstance(vn, dict):
            continue
        vb = vn.get("bounds") or []
        best_i = -1
        best_iou = 0.0
        vcls = str(vn.get("class") or "")
        for i, sn in enumerate(out):
            sb = sn.get("bounds") or []
            iou = _iou(list(sb), list(vb))
            if iou < iou_min:
                continue
            scls = str(sn.get("class") or "")
            if vcls and scls and vcls.split(".")[-1] != scls.split(".")[-1]:
                continue
            if iou > best_iou:
                best_iou = iou
                best_i = i
        if best_i >= 0:
            row = dict(out[best_i])
            for key in ("text", "content_desc", "clickable", "resource_id"):
                if not str(row.get(key) or "").strip() and str(vn.get(key) or "").strip():
                    row[key] = vn.get(key)
            out[best_i] = row
        else:
            out.append(dict(vn))
    return out


def vlm_landmark_signals_for_states(
    states: list[dict[str, Any]],
    vlm_hierarchy: dict[str, Any] | None,
) -> dict[str, dict[str, float]]:
    """按 state identify landmarks 对 VLM nodes 打分 → extra_signals。"""
    nodes = (vlm_hierarchy or {}).get("nodes") if isinstance(vlm_hierarchy, dict) else None
    if not isinstance(nodes, list) or not nodes:
        return {}
    from mino_nexus.loop.hierarchy_slots import match_any

    out: dict[str, dict[str, float]] = {}
    for state in states or []:
        sid = str(state.get("id") or "").strip()
        if not sid:
            continue
        identify = state.get("identify") if isinstance(state.get("identify"), dict) else {}
        specs: list[dict[str, Any]] = []
        for bucket in ("required", "optional"):
            for spec in identify.get(bucket) or []:
                if isinstance(spec, dict) and str(spec.get("signal") or "") == "text_landmarks":
                    specs.append(spec)
        if not specs:
            continue
        hit = 0.0
        for spec in specs:
            if match_any(nodes, spec.get("match_any") or []):
                hit = max(hit, 0.75)
        if hit > 0:
            out.setdefault(sid, {})["vlm_landmarks"] = hit
    return out
