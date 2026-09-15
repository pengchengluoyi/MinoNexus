"""NavFSM 屏态引用解析：state_id、展示名、别名与自然语言（相似度 + 屏态印证）。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

from mino_nexus.services import nav_fsm as F
from mino_nexus.services.nav_route import resolve_state_ref


def _norm(text: str) -> str:
    val = str(text or "").strip().lower()
    val = re.sub(r"[\s_·\-]+", "", val)
    val = val.replace("页面", "").replace("页", "")
    return val


def _similarity(a: str, b: str) -> float:
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    if na in nb or nb in na:
        return 0.92
    return float(SequenceMatcher(None, na, nb).ratio())


def _state_labels(fsm: dict[str, Any], state: dict[str, Any]) -> list[str]:
    sid = str(state.get("id") or "").strip()
    if not sid:
        return []
    out: list[str] = []
    meta = state.get("meta") if isinstance(state.get("meta"), dict) else {}
    for key in ("display_name", "page_title"):
        val = str(meta.get(key) or "").strip()
        if val:
            out.append(val)
    aliases = meta.get("aliases")
    if isinstance(aliases, list):
        for a in aliases:
            val = str(a or "").strip()
            if val:
                out.append(val)
    tab_meta = fsm.get("meta") if isinstance(fsm.get("meta"), dict) else {}
    tab_bar = tab_meta.get("tab_bar") if isinstance(tab_meta.get("tab_bar"), dict) else {}
    labels = tab_bar.get("labels") if isinstance(tab_bar.get("labels"), dict) else {}
    if sid in labels:
        val = str(labels[sid] or "").strip()
        if val:
            out.append(val)
    identify = state.get("identify") or {}
    required = identify.get("required")
    blocks = required if isinstance(required, list) else ([required] if required else [])
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("signal") == "tab_bar":
            tab = str((block.get("match") or {}).get("selected") or "").strip()
            if tab:
                out.append(tab)
    short = sid.replace("page.", "").replace("tab_", "").replace(".", " ")
    if short:
        out.append(short)
    dedup: list[str] = []
    seen: set[str] = set()
    for val in out:
        key = _norm(val)
        if key and key not in seen:
            seen.add(key)
            dedup.append(val)
    return dedup


def _screen_score(localized: dict[str, Any] | None, state_id: str) -> float:
    if not localized or not state_id:
        return 0.0
    chosen = str(localized.get("chosen") or "").strip()
    if chosen == state_id:
        return float(localized.get("confidence") or 0.0)
    for row in localized.get("candidates") or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("state_id") or "").strip() == state_id:
            return float(row.get("confidence") or 0.0)
    return 0.0


@dataclass(frozen=True)
class ResolveOutcome:
    state_id: str
    name_score: float = 0.0
    screen_score: float = 0.0
    method: str = ""


def resolve_state_fuzzy(
    fsm: dict[str, Any],
    ref: str,
    *,
    localized: dict[str, Any] | None = None,
    role: str = "to",
) -> ResolveOutcome:
    """把模型/文案解析为图中的 state_id。role=from 时对自然语言要求屏态印证。"""
    raw = str(ref or "").strip()
    if not raw or not fsm:
        return ResolveOutcome("", method="empty")

    if F.state_by_id(fsm, raw):
        return ResolveOutcome(
            raw,
            name_score=1.0,
            screen_score=_screen_score(localized, raw),
            method="state_id",
        )

    legacy = resolve_state_ref(fsm, raw)
    if legacy and F.state_by_id(fsm, legacy) and legacy != raw:
        return ResolveOutcome(
            legacy,
            name_score=0.95,
            screen_score=_screen_score(localized, legacy),
            method="legacy_ref",
        )

    best_sid = ""
    best_name = 0.0
    best_screen = 0.0
    for st in fsm.get("states") or []:
        if not isinstance(st, dict):
            continue
        sid = str(st.get("id") or "").strip()
        if not sid:
            continue
        labels = _state_labels(fsm, st)
        if not labels:
            continue
        name = max(_similarity(raw, lab) for lab in labels)
        if name > best_name:
            best_name = name
            best_sid = sid
            best_screen = _screen_score(localized, sid)

    looks_like_id = raw.startswith("page.") or raw.startswith("tab_")
    min_name = 0.55 if role == "to" else 0.5
    if best_name < min_name:
        return ResolveOutcome("", name_score=best_name, screen_score=best_screen, method="no_match")

    if role == "from" and not looks_like_id:
        chosen = str((localized or {}).get("chosen") or "").strip()
        if chosen and chosen == best_sid and best_screen >= 0.08:
            return ResolveOutcome(best_sid, name_score=best_name, screen_score=best_screen, method="fuzzy_from")
        if best_screen >= 0.2 and best_name >= 0.6:
            return ResolveOutcome(best_sid, name_score=best_name, screen_score=best_screen, method="fuzzy_from")
        if best_name >= 0.82 and best_screen >= 0.05:
            return ResolveOutcome(best_sid, name_score=best_name, screen_score=best_screen, method="fuzzy_from")
        return ResolveOutcome(
            "",
            name_score=best_name,
            screen_score=best_screen,
            method="from_unconfirmed",
        )

    return ResolveOutcome(best_sid, name_score=best_name, screen_score=best_screen, method="fuzzy_to")


def plan_route_resolved(
    fsm: dict[str, Any],
    *,
    from_ref: str,
    to_ref: str,
    localized: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """plan_route + 模糊解析；附带 resolve 元数据。"""
    from mino_nexus.services.nav_route import plan_route

    from_out = resolve_state_fuzzy(fsm, from_ref, localized=localized, role="from")
    to_out = resolve_state_fuzzy(fsm, to_ref, localized=localized, role="to")
    meta = {
        "from_ref": from_ref,
        "to_ref": to_ref,
        "resolved_from": from_out.state_id,
        "resolved_to": to_out.state_id,
        "from_name_score": from_out.name_score,
        "from_screen_score": from_out.screen_score,
        "to_name_score": to_out.name_score,
        "to_screen_score": to_out.screen_score,
        "from_method": from_out.method,
        "to_method": to_out.method,
    }
    if not to_out.state_id:
        return {
            "ok": False,
            "error": f"无法解析目标屏「{to_ref}」（name={to_out.name_score:.2f}）",
            "resolve": meta,
            "steps": [],
            "edge_ids": [],
        }
    if from_ref and not from_out.state_id:
        return {
            "ok": False,
            "error": (
                f"无法确认当前屏「{from_ref}」对应架构节点"
                f"（name={from_out.name_score:.2f} screen={from_out.screen_score:.2f}）"
            ),
            "resolve": meta,
            "steps": [],
            "edge_ids": [],
        }
    src = from_out.state_id or ""
    if not src:
        return {
            "ok": False,
            "error": "需要 from_state 或可由屏态解析的当前页描述",
            "resolve": meta,
            "steps": [],
            "edge_ids": [],
        }
    out = plan_route(fsm, from_state=src, to_state=to_out.state_id)
    out["resolve"] = meta
    return out
