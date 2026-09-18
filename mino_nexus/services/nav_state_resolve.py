"""NavFSM 屏态引用解析：state_id、展示名、别名与自然语言（相似度 + 屏态印证）。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

from mino_nexus.services import nav_fsm as F
from mino_nexus.services.nav_route import resolve_state_ref


_STATE_ID_SHAPE_RE = re.compile(r"^(?:page|dialog|state)\.|^tab_|^sk[0-9a-f]{6,}", re.I)
_OPAQUE_SHORT_ID_RE = re.compile(r"^sk[0-9a-f]{6,}", re.I)


def looks_like_state_id(ref: str) -> bool:
    """ref 是 state_id 形态（`page.*` / `tab_*` / 裸骨骼 id）还是口语页名。"""
    return bool(_STATE_ID_SHAPE_RE.match(str(ref or "").strip()))


def _norm(text: str) -> str:
    val = str(text or "").strip().lower()
    val = re.sub(r"[\s_·\-]+", "", val)
    # 只剥尾缀「页面/页」，不要全局删「页」：否则「开始造物拍照页」和按钮文案「开始造物」
    # 被当成同一串的包含关系，入口页会把拍照页吃掉。
    val = re.sub(r"(?:页面|页)$", "", val)
    return val


def _similarity(a: str, b: str) -> float:
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    if na in nb or nb in na:
        short, long = (na, nb) if len(na) <= len(nb) else (nb, na)
        # 短别名（按钮文案）包含在长口语里不得顶满 0.92，否则两页共享同一入口文案时
        # states 顺序先到先得，真正的目标页永远轮不到。
        return 0.55 + 0.40 * (len(short) / max(len(long), 1))
    return float(SequenceMatcher(None, na, nb).ratio())


def _leftover_after_label(raw: str, label: str) -> str:
    nq, nl = _norm(raw), _norm(label)
    if not nq or not nl or nq == nl or nl not in nq:
        return ""
    return nq.replace(nl, "", 1)


def _state_name_score(raw: str, labels: list[str]) -> float:
    """单页对口语的分数：最佳标签相似度 + 剩余字被同页其它标签覆盖的加分。"""
    if not labels:
        return 0.0
    best = 0.0
    leftover = ""
    for lab in labels:
        score = _similarity(raw, lab)
        if score > best:
            best = score
            leftover = _leftover_after_label(raw, lab)
    if leftover and len(leftover) >= 2:
        for lab in labels:
            nl = _norm(lab)
            if leftover in nl or (nl and nl in leftover):
                return min(1.0, best + 0.12)
    return best


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
    # 骨骼 id 不当标签：`sk3f5a31a92f9fs4` 与 `sk3f5a31a92f9fs0` 字面相似度 0.94，
    # 拿它参与模糊匹配等于让相邻两屏互相冒充。
    if short and not _OPAQUE_SHORT_ID_RE.match(short):
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
            conf = float(row.get("confidence") or 0.0)
            sig = row.get("signals") if isinstance(row.get("signals"), dict) else {}
            sk = float(sig.get("skeleton_wireframe") or 0.0)
            return max(conf, sk)
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
    exclude_ids: set[str] | None = None,
) -> ResolveOutcome:
    """把模型/文案解析为图中的 state_id。role=from 时对自然语言要求屏态印证。"""
    raw = str(ref or "").strip()
    skip = {str(x).strip() for x in (exclude_ids or set()) if str(x).strip()}
    if not raw or not fsm:
        return ResolveOutcome("", method="empty")

    if F.state_by_id(fsm, raw) and raw not in skip:
        return ResolveOutcome(
            raw,
            name_score=1.0,
            screen_score=_screen_score(localized, raw),
            method="state_id",
        )

    legacy = resolve_state_ref(fsm, raw)
    if legacy and F.state_by_id(fsm, legacy) and legacy != raw and legacy not in skip:
        return ResolveOutcome(
            legacy,
            name_score=0.95,
            screen_score=_screen_score(localized, legacy),
            method="legacy_ref",
        )

    if looks_like_state_id(raw):
        # id 形态只认精确存在。骨骼 id 会随重新聚类换代（`page.skd568c2708674s0` →
        # `page.skd568c2708674`），模糊匹配会把一个已失效的 id 静默解析成隔壁那一屏，
        # 规划照样"成功"，走到的却是别的页。宁可报不认识，让 localize 来兜。
        return ResolveOutcome("", method="unknown_state_id")

    scored: list[tuple[float, bool, str]] = []
    for st in fsm.get("states") or []:
        if not isinstance(st, dict):
            continue
        sid = str(st.get("id") or "").strip()
        if not sid or sid in skip:
            continue
        labels = _state_labels(fsm, st)
        if not labels:
            continue
        name = _state_name_score(raw, labels)
        base = max((_similarity(raw, lab) for lab in labels), default=0.0)
        scored.append((name, name > base + 0.001, sid))
    scored.sort(key=lambda row: (row[0], row[1]), reverse=True)

    best_name = scored[0][0] if scored else 0.0
    best_sid = scored[0][2] if scored else ""
    best_screen = _screen_score(localized, best_sid) if best_sid else 0.0
    min_name = 0.55 if role == "to" else 0.5
    if best_name < min_name:
        from mino_nexus.services.nav_edge_resolve import resolve_state_via_nav_edges

        edge_out = resolve_state_via_nav_edges(fsm, raw, role=role)
        if edge_out.state_id and edge_out.state_id not in skip:
            return ResolveOutcome(
                edge_out.state_id,
                name_score=edge_out.name_score,
                screen_score=_screen_score(localized, edge_out.state_id),
                method=edge_out.method,
            )
        return ResolveOutcome("", name_score=best_name, screen_score=best_screen, method="no_match")

    top = [row for row in scored if row[0] >= best_name - 0.04]
    if len(top) > 1:
        leftover_top = [row for row in top if row[1]]
        if len(leftover_top) == 1:
            best_sid = leftover_top[0][2]
        elif role == "to":
            from mino_nexus.services.nav_edge_resolve import resolve_state_via_nav_edges

            edge_out = resolve_state_via_nav_edges(fsm, raw, role="to")
            top_ids = {row[2] for row in top}
            if (
                edge_out.state_id
                and edge_out.state_id in top_ids
                and edge_out.state_id not in skip
                and edge_out.name_score >= 0.9
            ):
                return ResolveOutcome(
                    edge_out.state_id,
                    name_score=max(best_name, edge_out.name_score),
                    screen_score=_screen_score(localized, edge_out.state_id),
                    method="nav_edge_to",
                )
            chosen = str((localized or {}).get("chosen") or "").strip()
            others = [row for row in top if row[2] != chosen]
            if chosen and others:
                best_sid = others[0][2]
    best_screen = _screen_score(localized, best_sid)

    if role == "from":
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


def _same_utterance(a: str, b: str) -> bool:
    na, nb = _norm(a), _norm(b)
    return bool(na and na == nb)


def _disambiguate_to_away_from_here(
    fsm: dict[str, Any],
    *,
    from_ref: str,
    to_ref: str,
    from_out: ResolveOutcome,
    to_out: ResolveOutcome,
    localized: dict[str, Any] | None,
) -> ResolveOutcome:
    """from/to 口语不同却解析到同一节点：几乎一定是入口按钮别名撞车，再找一次。"""
    src = str(from_out.state_id or "").strip()
    if not src or to_out.state_id != src:
        return to_out
    if _same_utterance(from_ref, to_ref):
        return to_out
    alt = resolve_state_fuzzy(
        fsm,
        to_ref,
        localized=localized,
        role="to",
        exclude_ids={src},
    )
    if alt.state_id and alt.name_score >= 0.55:
        return ResolveOutcome(
            alt.state_id,
            name_score=alt.name_score,
            screen_score=alt.screen_score,
            method=alt.method or "to_exclude_from",
        )
    from mino_nexus.services.nav_edge_resolve import resolve_state_via_nav_edges

    edge_out = resolve_state_via_nav_edges(fsm, to_ref, role="to")
    if edge_out.state_id and edge_out.state_id != src:
        return ResolveOutcome(
            edge_out.state_id,
            name_score=edge_out.name_score,
            screen_score=_screen_score(localized, edge_out.state_id),
            method=edge_out.method,
        )
    return to_out


def plan_route_resolved(
    fsm: dict[str, Any],
    *,
    from_ref: str,
    to_ref: str,
    localized: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """plan_route + 模糊解析；附带 resolve 元数据。"""
    from mino_nexus.services.nav_route import coerce_oral_nav_ref, plan_route

    from_ref = coerce_oral_nav_ref(fsm, from_ref)
    to_ref = coerce_oral_nav_ref(fsm, to_ref)

    from_out = resolve_state_fuzzy(fsm, from_ref, localized=localized, role="from")
    chosen = str((localized or {}).get("chosen") or "").strip()
    ch_conf = float((localized or {}).get("confidence") or 0.0)
    if from_ref and not from_out.state_id and chosen and ch_conf >= 0.35 and F.state_by_id(fsm, chosen):
        from_out = ResolveOutcome(
            chosen,
            name_score=from_out.name_score,
            screen_score=ch_conf,
            method="localized_chosen",
        )
    to_out = resolve_state_fuzzy(fsm, to_ref, localized=localized, role="to")
    to_out = _disambiguate_to_away_from_here(
        fsm,
        from_ref=from_ref,
        to_ref=to_ref,
        from_out=from_out,
        to_out=to_out,
        localized=localized,
    )
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
        if to_out.method == "unknown_state_id":
            err = f"架构图里没有 state_id「{to_ref}」，可能已随重新聚类换代；请用页面展示名"
        else:
            err = f"无法解析目标屏「{to_ref}」（name={to_out.name_score:.2f}）"
        return {
            "ok": False,
            "error": err,
            "resolve": meta,
            "steps": [],
            "edge_ids": [],
        }
    if from_ref and not from_out.state_id:
        return {
            "ok": False,
            "error": (
                f"架构图里没有 state_id「{from_ref}」，可能已随重新聚类换代"
                if from_out.method == "unknown_state_id"
                else f"无法确认当前屏「{from_ref}」对应架构节点"
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
