"""从 Nav 边 execute 载荷解析目标屏（Tab/入口文案 → edge.to），不硬编码 App 文案。"""
from __future__ import annotations

from typing import Any

from mino_nexus.services import nav_fsm as F
from mino_nexus.services.nav_execute import execute_target_page
from mino_nexus.services.nav_state_resolve import ResolveOutcome, _similarity


def _edge_tap_labels(edge: dict[str, Any]) -> list[str]:
    exe = edge.get("execute") if isinstance(edge.get("execute"), dict) else {}
    out: list[str] = []
    for key in ("target_page", "target_tab", "selector_text", "text"):
        val = str(exe.get(key) or "").strip()
        if val:
            out.append(val)
    page = str(execute_target_page(exe) or "").strip()
    if page and page not in out:
        out.append(page)
    meta = edge.get("meta") if isinstance(edge.get("meta"), dict) else {}
    al = str(meta.get("action_label") or "").strip()
    if al and "·" in al:
        part = al.split("·", -1)[-1].strip()
        if part:
            out.append(part)
    dedup: list[str] = []
    seen: set[str] = set()
    for val in out:
        key = val.casefold()
        if key not in seen:
            seen.add(key)
            dedup.append(val)
    return dedup


def resolve_state_via_nav_edges(
    fsm: dict[str, Any],
    ref: str,
    *,
    role: str = "to",
) -> ResolveOutcome:
    """ref 与某条 nav 边的点击文案/ target_page 对齐时，返回该边的 to（或 from）state_id。"""
    raw = str(ref or "").strip()
    if not raw or not fsm:
        return ResolveOutcome("", method="edge_empty")
    if role not in ("to", "from"):
        role = "to"

    best_sid = ""
    best_name = 0.0
    best_method = ""

    for edge in fsm.get("edges") or []:
        if not isinstance(edge, dict):
            continue
        kind = str(edge.get("kind") or "nav").strip()
        if kind and kind not in ("nav", "recover"):
            continue
        labels = _edge_tap_labels(edge)
        if not labels:
            continue
        name = max(_similarity(raw, lab) for lab in labels)
        if name < 0.55:
            continue
        if role == "to":
            sid = str(edge.get("to") or edge.get("to_state") or "").strip()
            method = "nav_edge_to"
        else:
            sid = str(edge.get("from") or edge.get("from_state") or "").strip()
            method = "nav_edge_from"
        if not sid or not F.state_by_id(fsm, sid):
            continue
        if name > best_name:
            best_name = name
            best_sid = sid
            best_method = method

    if not best_sid:
        return ResolveOutcome("", name_score=best_name, method="edge_no_match")
    return ResolveOutcome(best_sid, name_score=best_name, screen_score=0.0, method=best_method)


def enrich_state_aliases_from_nav_edges(doc: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """把边上可编译的 target_page/selector 写入目标 state.meta.aliases（图内真源，非白名单）。"""
    if not doc:
        return doc, 0
    states = [dict(st) for st in (doc.get("states") or []) if isinstance(st, dict)]
    if not states:
        return doc, 0
    by_id = {str(st.get("id") or ""): st for st in states if str(st.get("id") or "")}
    touched: set[str] = set()
    for edge in doc.get("edges") or []:
        if not isinstance(edge, dict):
            continue
        kind = str(edge.get("kind") or "nav").strip()
        if kind and kind not in ("nav", "recover"):
            continue
        sid = str(edge.get("to") or edge.get("to_state") or "").strip()
        st = by_id.get(sid)
        if not st:
            continue
        meta = dict(st.get("meta") or {})
        aliases = [str(a or "").strip() for a in (meta.get("aliases") or []) if str(a or "").strip()]
        alias_set = {a.casefold() for a in aliases}
        for lab in _edge_tap_labels(edge):
            if lab.casefold() not in alias_set:
                aliases.append(lab)
                alias_set.add(lab.casefold())
                touched.add(sid)
        if sid in touched:
            meta["aliases"] = aliases[:24]
            st["meta"] = meta
    if not touched:
        return doc, 0
    out = dict(doc)
    out["states"] = list(by_id.values())
    return out, len(touched)
