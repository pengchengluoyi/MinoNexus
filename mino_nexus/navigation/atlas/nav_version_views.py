"""应用版本 → nav_view；待验证层过滤；state 多版本骨骼 facet。"""
from __future__ import annotations

import copy
import re
from typing import Any

DEFAULT_NAV_VIEW = "av:default"
VERIFIED = "verified"
PENDING = "pending"


def _norm_version(v: str) -> str:
    return str(v or "").strip()


def version_matches(app_version: str, version_range: str) -> bool:
    av = _norm_version(app_version)
    pattern = _norm_version(version_range)
    if not pattern or pattern == "*":
        return True
    if not av:
        return pattern == DEFAULT_NAV_VIEW or pattern == "*"
    if pattern.endswith(".*"):
        prefix = pattern[:-2]
        return av == prefix or av.startswith(prefix + ".")
    if pattern.endswith("*"):
        prefix = pattern[:-1]
        return av.startswith(prefix)
    return av == pattern


def nav_view_id_for_version(app_version: str) -> str:
    av = _norm_version(app_version)
    if not av:
        return DEFAULT_NAV_VIEW
    safe = re.sub(r"[^0-9A-Za-z._-]+", "_", av)[:48]
    return f"av:{safe}"


def collect_nav_views_from_turns(turns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """从采集样本汇总 nav_views（verified 层）。"""
    views: list[dict[str, Any]] = [
        {
            "nav_view_id": DEFAULT_NAV_VIEW,
            "version_range": "*",
            "label": "默认（未标版本）",
            "verification_status": VERIFIED,
        }
    ]
    seen: set[str] = set()
    for turn in turns or []:
        av = _norm_version(str(turn.get("app_version") or ""))
        if not av or av in seen:
            continue
        seen.add(av)
        parts = av.split(".")
        range_pat = "*"
        if len(parts) >= 2:
            range_pat = f"{parts[0]}.{parts[1]}.*"
        elif len(parts) == 1:
            range_pat = f"{parts[0]}.*"
        views.append(
            {
                "nav_view_id": nav_view_id_for_version(av),
                "version_range": range_pat,
                "label": av,
                "verification_status": VERIFIED,
                "sample_app_version": av,
            }
        )
    return views


def resolve_nav_view(app_version: str, nav_views: list[dict[str, Any]] | None) -> str:
    av = _norm_version(app_version)
    views = nav_views if isinstance(nav_views, list) else []
    if not av:
        return DEFAULT_NAV_VIEW
    best_id = DEFAULT_NAV_VIEW
    best_specificity = -1
    for row in views:
        if str(row.get("verification_status") or VERIFIED) == PENDING:
            continue
        rng = str(row.get("version_range") or "*")
        if not version_matches(av, rng):
            continue
        spec = len(rng.replace("*", ""))
        if spec > best_specificity:
            best_specificity = spec
            best_id = str(row.get("nav_view_id") or DEFAULT_NAV_VIEW)
    if best_specificity >= 0:
        return best_id
    return nav_view_id_for_version(av)


def _edge_status(edge: dict[str, Any]) -> str:
    meta = edge.get("meta") if isinstance(edge.get("meta"), dict) else {}
    tr = meta.get("transition") if isinstance(meta.get("transition"), dict) else {}
    for key in ("verification_status",):
        val = str(meta.get(key) or tr.get(key) or "").strip()
        if val:
            return val
    ex = edge.get("execute") if isinstance(edge.get("execute"), dict) else {}
    by_view = ex.get("by_view") if isinstance(ex.get("by_view"), dict) else {}
    if by_view:
        return VERIFIED
    return VERIFIED


def _edge_applies_to_view(edge: dict[str, Any], nav_view_id: str) -> bool:
    meta = edge.get("meta") if isinstance(edge.get("meta"), dict) else {}
    want = str(meta.get("nav_view_id") or "").strip()
    if not want or want == DEFAULT_NAV_VIEW:
        return True
    return want == nav_view_id or nav_view_id == DEFAULT_NAV_VIEW


def _execute_for_view(edge: dict[str, Any], nav_view_id: str) -> dict[str, Any]:
    ex = dict(edge.get("execute") or {})
    by_view = ex.pop("by_view", None)
    if isinstance(by_view, dict):
        picked = by_view.get(nav_view_id) or by_view.get(DEFAULT_NAV_VIEW)
        if isinstance(picked, dict):
            ex = {**ex, **picked}
    return ex


def _apply_state_facets(states: list[dict[str, Any]], nav_view_id: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for st in states:
        row = copy.deepcopy(st)
        meta = dict(row.get("meta") or {})
        facets = meta.get("version_facets") if isinstance(meta.get("version_facets"), list) else []
        picked: dict[str, Any] | None = None
        fallback: dict[str, Any] | None = None
        for facet in facets:
            if not isinstance(facet, dict):
                continue
            fid = str(facet.get("nav_view_id") or "")
            status = str(facet.get("verification_status") or VERIFIED)
            if status == PENDING:
                continue
            if fid == nav_view_id:
                picked = facet
                break
            if fid == DEFAULT_NAV_VIEW:
                fallback = facet
        use = picked or fallback
        if use:
            wf_ref = use.get("wireframe_ref")
            if wf_ref:
                meta["effective_wireframe_ref"] = wf_ref
            meta["active_nav_view_id"] = nav_view_id
            if use.get("identify_patch") and isinstance(use.get("identify_patch"), dict):
                meta["facet_identify_patch"] = use["identify_patch"]
        row["meta"] = meta
        out.append(row)
    return out


def materialize_fsm_for_runtime(
    doc: dict[str, Any] | None,
    *,
    app_version: str = "",
    nav_view_id: str = "",
    include_pending: bool = False,
) -> dict[str, Any] | None:
    """按当前 app 版本裁剪边与骨骼 facet；不修改原 doc。"""
    if not doc:
        return None
    out = copy.deepcopy(doc)
    meta = dict(out.get("meta") or {})
    views = meta.get("nav_views") if isinstance(meta.get("nav_views"), list) else []
    resolved = nav_view_id or resolve_nav_view(app_version, views)
    meta["resolved_nav_view_id"] = resolved
    meta["resolved_app_version"] = _norm_version(app_version)
    out["meta"] = meta

    edges_in = out.get("edges") if isinstance(out.get("edges"), list) else []
    edges_out: list[dict[str, Any]] = []
    for ed in edges_in:
        if str(ed.get("kind") or "nav") != "nav":
            edges_out.append(ed)
            continue
        status = _edge_status(ed)
        if status == PENDING and not include_pending:
            continue
        if not _edge_applies_to_view(ed, resolved):
            continue
        row = copy.deepcopy(ed)
        row["execute"] = _execute_for_view(row, resolved)
        edges_out.append(row)
    out["edges"] = edges_out
    states = out.get("states") if isinstance(out.get("states"), list) else []
    out["states"] = _apply_state_facets(states, resolved)
    return out


def pending_summary(doc: dict[str, Any] | None) -> dict[str, Any]:
    if not doc:
        return {"pending_edges": 0, "pending_facets": 0, "pending_blocks": 0}
    pending_edges = 0
    for ed in doc.get("edges") or []:
        if _edge_status(ed) == PENDING:
            pending_edges += 1
    pending_facets = 0
    for st in doc.get("states") or []:
        meta = st.get("meta") if isinstance(st.get("meta"), dict) else {}
        for facet in meta.get("version_facets") or []:
            if isinstance(facet, dict) and str(facet.get("verification_status") or "") == PENDING:
                pending_facets += 1
    pending_blocks = 0
    meta = doc.get("meta") if isinstance(doc.get("meta"), dict) else {}
    for block in meta.get("flow_blocks") or []:
        if isinstance(block, dict) and str(block.get("verification_status") or "") == PENDING:
            pending_blocks += 1
    return {
        "pending_edges": pending_edges,
        "pending_facets": pending_facets,
        "pending_blocks": pending_blocks,
    }


def capture_report_by_app_version(turns: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for turn in turns or []:
        key = _norm_version(str(turn.get("app_version") or "")) or "(unknown)"
        counts[key] = counts.get(key, 0) + 1
    return counts
