"""Screen Atlas：从采集聚类屏面，Tab+结构关系图，供架构图与探索进度使用。"""
from __future__ import annotations

import hashlib
import re
import time
from collections import Counter
from typing import Any

from mino_nexus.services import nav_capture_store as capture
from mino_nexus.services.nav_capture_store import is_system_screen


def _short_hash(text: str, n: int = 6) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[: max(4, n)]


def fingerprint_turn(
    turn: dict[str, Any],
    *,
    y_tab: int = 0,
    exclude: set[str] | None = None,
) -> str:
    """细粒度证据指纹（探索进度等）；架构图聚类用 synthesis 同款 role/chrome_key。"""
    from mino_nexus.services.nav_layout import (
        detect_layout_framework,
        framework_fingerprint,
        stable_chrome_texts,
    )
    from mino_nexus.services.nav_screen_layout import infer_content_bands

    nodes = turn.get("nodes") or []
    exclude = exclude or set()
    if not nodes:
        return "screen.empty"
    if y_tab <= 0:
        bands = infer_content_bands(nodes)
        y_tab = int(bands.get("content_bottom_px") or 0) or 9999
    fw = detect_layout_framework(turn, y_tab_max=y_tab, exclude=exclude)
    chrome = stable_chrome_texts(turn, y_tab_max=y_tab, exclude=exclude)
    fp, chrome_key = framework_fingerprint(fw, chrome)
    ck = chrome_key if isinstance(chrome_key, (list, tuple)) else (chrome_key,)
    raw = f"{fp}|{','.join(str(x) for x in ck if x)}"
    return f"screen.{_short_hash(raw)}"


def fingerprint_nodes(
    nodes: list[dict[str, Any]],
    *,
    exclude: set[str] | None = None,
) -> str:
    return fingerprint_turn({"nodes": nodes}, exclude=exclude or set())


def _atlas_layout_cluster_key(
    row: dict[str, Any],
    wf: dict[str, Any] | None,
    role: str,
    *,
    tab: str = "",
    y_tab: int = 0,
    exclude_tabs: set[str] | None = None,
    tab_labels: list[str] | None = None,
) -> str:
    """Atlas 聚类键：主 Feed 合并 + 子页顶栏区分（不含 role 启发式分桶）。"""
    from mino_nexus.services.nav_app_skeleton import skeleton_fp_from_turn

    turn = row.get("turn") if isinstance(row.get("turn"), dict) else {"nodes": []}
    fw = dict(row.get("framework") or {})
    return skeleton_fp_from_turn(
        turn,
        wf,
        fw,
        tab=str(tab or row.get("tab") or ""),
        tab_labels=tab_labels,
        y_tab_max=y_tab,
        exclude_tabs=exclude_tabs,
    )


def _wireframe_layout_signature(wf: dict[str, Any] | None) -> str:
    """线框布局签名：同结构屏面合并（不看流式文案/商品标题）。 """
    if not isinstance(wf, dict):
        return "empty"
    regions = wf.get("regions") or []
    if not regions:
        return "empty"
    bits: list[str] = []
    for r in sorted(
        regions,
        key=lambda x: (
            round(float((x.get("rect") or {}).get("y") or 0), 3),
            round(float((x.get("rect") or {}).get("x") or 0), 3),
        ),
    ):
        label = str(r.get("label") or "").strip()
        cls = str(r.get("class_name") or "").strip()
        if label == "FrameLayout" or cls == "FrameLayout":
            continue
        rect = r.get("rect") or {}
        bits.append(
            f"{r.get('source') or ''}:{round(float(rect.get('w') or 0), 3)}:"
            f"{round(float(rect.get('h') or 0), 3)}"
        )
    return _short_hash("|".join(bits[:32]), 10)


def _atlas_state_page_lane(state_id: str) -> str:
    """page.sk{hash}sN → page.sk{hash}，同车道视口并页用。"""
    sid = str(state_id or "").strip()
    if not sid.startswith("page.sk") or ".sk" not in sid:
        return sid
    body = sid.split(".", 1)[-1]
    if body.startswith("sk") and "s" in body[2:]:
        head, _, suffix = body.rpartition("s")
        if suffix.isdigit() and head:
            return f"page.{head}"
    return sid


def _merge_wireframe_duplicate_clusters(
    screen_rows: list[dict[str, Any]],
    cluster_meta: dict[str, dict[str, Any]],
    turn_cluster_ids: list[str],
    *,
    timeline: list[dict[str, Any]] | None = None,
    app_id: str = "",
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """同 skeleton_fp 的重复 state（极少）才合并；不因线框相似或 feed 标题并页。"""
    nav_sigs = (
        _build_state_click_nav_signatures(timeline or [], turn_cluster_ids, app_id=app_id)
        if timeline
        else {}
    )

    groups: dict[tuple[str, str], list[str]] = {}
    for row in screen_rows:
        if row.get("is_entry"):
            continue
        sid = str(row.get("id") or "")
        tab = str(row.get("tab") or "")
        cm = cluster_meta.get(sid) or {}
        sk = str(cm.get("skeleton_fp") or row.get("skeleton_fp") or "").strip()
        if sk:
            groups.setdefault((tab, f"sk:{sk}"), []).append(sid)

    remap: dict[str, str] = {}

    def _absorb_into_canonical(
        canonical: str,
        dup_id: str,
        *,
        viewport_transform: bool = False,
    ) -> None:
        if dup_id == canonical or dup_id in remap:
            return
        sa = nav_sigs.get(canonical, frozenset())
        sb = nav_sigs.get(dup_id, frozenset())
        dup_vis = int((cluster_meta.get(dup_id) or {}).get("visit_count") or 0)
        if not viewport_transform and not _nav_click_signatures_compatible(sa, sb):
            return
        if viewport_transform and not _nav_click_signatures_compatible(sa, sb):
            click_a = {x for x in sa if x.startswith(("tap:", "tab:"))}
            click_b = {x for x in sb if x.startswith(("tap:", "tab:"))}
            if click_a and click_b and click_a != click_b and dup_vis > 5:
                return
        remap[dup_id] = canonical
        can_meta = cluster_meta.get(canonical) or {}
        dup = cluster_meta.get(dup_id) or {}
        can_meta["visit_count"] = int(can_meta.get("visit_count") or 0) + int(
            dup.get("visit_count") or 0
        )
        can_turns = list(can_meta.get("turn_wireframes") or [])
        can_turns.extend(list(dup.get("turn_wireframes") or []))
        can_meta["turn_wireframes"] = can_turns
        morphs = list(can_meta.get("morphs") or []) + list(dup.get("morphs") or [])
        if morphs:
            can_meta["morphs"] = morphs[:24]
        can_meta["morph_count"] = max(
            int(can_meta.get("morph_count") or 0),
            int(dup.get("morph_count") or 0),
            max(0, len(can_meta.get("morphs") or []) - 1),
            max(0, len(can_turns) - 1),
        )
        dn = str(dup.get("display_name") or "").strip()
        names = list(can_meta.get("_merged_display_names") or [])
        if dn and dn not in names:
            names.append(dn)
        can_meta["_merged_display_names"] = names
        cluster_meta[canonical] = can_meta

    for (_tab, _sig), sids in groups.items():
        if len(sids) < 2:
            continue
        canonical = max(sids, key=lambda s: int((cluster_meta.get(s) or {}).get("visit_count") or 0))
        can_meta = cluster_meta.get(canonical) or {}
        names: list[str] = []
        for s in sids:
            if s == canonical:
                continue
            _absorb_into_canonical(canonical, s)
            dup = cluster_meta.get(s) or {}
            dn = str(dup.get("display_name") or "").strip()
            if dn and dn not in names:
                names.append(dn)
        if names:
            base = str(can_meta.get("display_name") or "").strip()
            merged_title = base
            if base and names:
                merged_title = base if base in names else f"{base}（等同布局）"
            elif names:
                merged_title = names[0]
            can_meta["display_name"] = merged_title
        cluster_meta[canonical] = can_meta

    from mino_nexus.services.nav_app_skeleton import (
        representative_wireframe_from_meta,
        wireframe_aligned_transform_jaccard,
        wireframes_same_page_under_viewport_transform,
    )

    def _resolve_sid(sid: str) -> str:
        while sid in remap:
            sid = remap[sid]
        return sid

    page_ids = [
        str(r.get("id") or "")
        for r in screen_rows
        if not r.get("is_entry") and str(r.get("id") or "")
    ]
    page_ids.sort(
        key=lambda s: int((cluster_meta.get(s) or {}).get("visit_count") or 0),
        reverse=True,
    )
    for canon_raw in page_ids:
        canon = _resolve_sid(canon_raw)
        can_meta = cluster_meta.get(canon) or {}
        wf_c = representative_wireframe_from_meta(can_meta)
        vc = int(can_meta.get("visit_count") or 0)
        for dup_raw in page_ids:
            if dup_raw == canon_raw:
                continue
            dup = _resolve_sid(dup_raw)
            if dup == canon or dup_raw in remap:
                continue
            dup_meta = cluster_meta.get(dup) or {}
            wf_d = representative_wireframe_from_meta(dup_meta)
            if not wireframes_same_page_under_viewport_transform(wf_c, wf_d):
                continue
            import re

            sk_a = str(can_meta.get("skeleton_fp") or "")
            sk_b = str(dup_meta.get("skeleton_fp") or "")
            base_a = re.sub(r"-s\d+$", "", sk_a)
            base_b = re.sub(r"-s\d+$", "", sk_b)
            lane_ok = _atlas_state_page_lane(canon) == _atlas_state_page_lane(dup)
            if base_a and base_b and base_a != base_b and not lane_ok:
                continue
            if not lane_ok and (not base_a or not base_b or base_a != base_b):
                continue
            aligned = wireframe_aligned_transform_jaccard(wf_c, wf_d)
            vd = int(dup_meta.get("visit_count") or 0)
            if vd > max(6, int(vc * 0.25)) and aligned < 0.85:
                continue
            _absorb_into_canonical(canon, dup, viewport_transform=True)

    lane_groups: dict[str, list[str]] = {}
    for row in screen_rows:
        if row.get("is_entry"):
            continue
        sid = str(row.get("id") or "")
        if sid:
            lane_groups.setdefault(_atlas_state_page_lane(sid), []).append(sid)
    for _lane, sids in lane_groups.items():
        if len(sids) < 2:
            continue
        canonical = max(
            sids, key=lambda s: int((cluster_meta.get(s) or {}).get("visit_count") or 0)
        )
        wf_can = representative_wireframe_from_meta(cluster_meta.get(canonical) or {})
        for dup in sids:
            if dup == canonical or dup in remap:
                continue
            wf_dup = representative_wireframe_from_meta(cluster_meta.get(dup) or {})
            if not wireframes_same_page_under_viewport_transform(wf_can, wf_dup):
                aligned = wireframe_aligned_transform_jaccard(wf_can, wf_dup)
                if aligned < 0.72:
                    continue
            vd = int((cluster_meta.get(dup) or {}).get("visit_count") or 0)
            if vd > 8:
                continue
            _absorb_into_canonical(canonical, dup, viewport_transform=True)

    if not remap:
        return screen_rows, remap

    keep_ids = {str(r.get("id") or "") for r in screen_rows} - set(remap.keys())
    screen_rows = [r for r in screen_rows if str(r.get("id") or "") in keep_ids]
    for i, cid in enumerate(turn_cluster_ids):
        if cid in remap:
            turn_cluster_ids[i] = remap[cid]
    return screen_rows, remap


def _slug_atlas_page(
    skeleton_fp: str,
    used: set[str],
) -> str:
    """屏面 state id：page.sk{骨骼哈希}（无 Tab 前缀、sk 后无下划线）。"""
    lk = re.sub(r"[^\w]", "", str(skeleton_fp or "page"))[:16]
    sid = f"page.sk{lk}"
    n = 2
    while sid in used:
        sid = f"page.sk{lk}{n}"
        n += 1
    used.add(sid)
    return sid


def _logical_page_title(tab: str, role: str, fw: dict[str, Any]) -> str:
    """页面逻辑名：Tab + 布局角色/框架，不用通知栏/商品标题。"""
    kind = str(fw.get("kind") or "").strip()
    if kind in ("unknown", "tab_shell", ""):
        kind = str(role or "page")
    kind_label = kind.replace("_", " ")
    widgets = [str(w) for w in (fw.get("widgets") or []) if w][:3]
    detail = ""
    if widgets and kind in ("feed_grid", "feed_list", "content_page", "profile_page"):
        detail = " · ".join(widgets)
    body = f"{kind_label}{(' · ' + detail) if detail else ''}"
    tab = str(tab or "").strip()
    return f"{tab} · {body}" if tab else body


def _tab_label_allowed(label: str, tab_labels: list[str]) -> bool:
    val = str(label or "").strip()
    if not val:
        return False
    if val in tab_labels:
        return True
    return False


def _compact_page_title(label: str) -> str:
    """短标题：≤10 字、无数字、不含布局结构名。"""
    from mino_nexus.services.nav_synthesis import _is_bad_tab_slot_label

    raw = str(label or "").strip()
    if _is_bad_tab_slot_label(raw):
        return "页面"
    val = re.sub(r"\d+", "", raw)
    val = re.sub(r"\s+", "", val)
    if not val or "%" in val or len(val) <= 1:
        return "页面"
    return val[:10]


def _title_from_chrome(
    chrome: list[str],
    *,
    tab: str,
    tab_labels: list[str],
) -> str:
    """子页标题：顶栏第一个非 Tab、非噪声短文案。"""
    from mino_nexus.services.nav_layout import is_status_bar_chrome_text, is_volatile_text

    tabs = {str(t or "").strip() for t in tab_labels if str(t or "").strip()}
    tab_s = str(tab or "").strip()
    skip = tabs | {tab_s, "返回"}
    for text in chrome:
        val = str(text or "").strip()
        if not val or val in skip or is_volatile_text(val) or is_status_bar_chrome_text(val):
            continue
        if 2 <= len(val) <= 24:
            return val
    return ""


def _atlas_state_title(
    turn: dict[str, Any],
    *,
    tab_labels: list[str],
    assigned_tab: str,
    role: str,
    fw: dict[str, Any],
    is_entry: bool = False,
    chrome: list[str] | None = None,
) -> str:
    """屏面标题：底栏 Tab 短名；同逻辑页靠聚类合并，不靠改标题区分。"""
    from mino_nexus.services.nav_synthesis import infer_selected_tab_label

    if not is_entry and chrome:
        chrome_title = _title_from_chrome(chrome, tab=assigned_tab, tab_labels=tab_labels)
        if chrome_title:
            return _compact_page_title(chrome_title)

    selected = infer_selected_tab_label(turn, tab_labels, fallback="", prev_tab="")
    name_tab = selected if _tab_label_allowed(selected, tab_labels) else ""
    if not name_tab and _tab_label_allowed(assigned_tab, tab_labels):
        name_tab = assigned_tab
    if name_tab and is_entry:
        return _compact_page_title(name_tab)
    if name_tab and not chrome:
        return _compact_page_title(name_tab)
    return _logical_page_title(
        str(assigned_tab or ""),
        str(role or ""),
        dict(fw or {}),
    )[:24]


def _resolve_atlas_display_name(
    meta: dict[str, Any],
    *,
    tab: str,
    tab_labels: list[str],
    is_entry: bool,
    fw: dict[str, Any],
    role: str,
) -> str:
    """展示名：用户已编辑 display_name > 顶栏 chrome > Tab 短名 > 逻辑 role。"""
    user_dn = str(meta.get("user_display_name") or "").strip()
    if user_dn:
        return _compact_page_title(user_dn)
    header = str(meta.get("header_title") or "").strip()
    if header and not is_entry:
        return _compact_page_title(header)
    chromes = meta.get("chrome_texts") or meta.get("chrome") or []
    chrome_title = _title_from_chrome(
        list(chromes), tab=tab, tab_labels=tab_labels
    )
    if chrome_title and not is_entry:
        return _compact_page_title(chrome_title)
    if is_entry and _tab_label_allowed(tab, tab_labels):
        return _compact_page_title(tab)
    return _logical_page_title(str(tab or ""), str(role or ""), dict(fw or {}))[:24]


def _tab_switch_evidence(
    prev_turn: dict[str, Any],
    target_tab: str,
    tab_labels: list[str],
) -> bool:
    """上一屏底栏可见且含目标 Tab，且线框里能点到该 Tab。"""
    from mino_nexus.services.nav_synthesis import _horizontal_tab_row_labels

    want = str(target_tab or "").strip()
    if not _tab_label_allowed(want, tab_labels):
        return False
    row = _horizontal_tab_row_labels(prev_turn)
    if len(row) < 2:
        return False
    if not any(want == x or want in x or x in want for x in row):
        return False
    return bool(_pick_tab_hotspot(prev_turn, want))


def _prune_orphan_duplicate_screens(
    states: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    wireframes: dict[str, Any],
    cluster_meta: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any], dict[str, str]]:
    incident: Counter[str] = Counter()
    for ed in edges:
        f = str(ed.get("from") or "")
        t = str(ed.get("to") or "")
        if f:
            incident[f] += 1
        if t:
            incident[t] += 1

    drop_ids: set[str] = set()
    remap: dict[str, str] = {}
    best_by_sig: dict[tuple[str, str, str], str] = {}

    for st in states:
        sid = str(st.get("id") or "")
        if st.get("entry"):
            continue
        sig = _wireframe_layout_signature(wireframes.get(sid))
        if sig == "empty":
            continue
        meta = st.get("meta") if isinstance(st.get("meta"), dict) else {}
        # 相同线框在不同 Tab / 不同页面角色下是不同逻辑页；只在同语义域内合并。
        group_key = (
            str(meta.get("tab") or ""),
            str(meta.get("page_role") or ""),
            sig,
        )
        prev = best_by_sig.get(group_key)
        if not prev:
            best_by_sig[group_key] = sid
            continue
        prev_score = int((cluster_meta.get(prev) or {}).get("visit_count") or 0) + incident[prev]
        cur_score = int((cluster_meta.get(sid) or {}).get("visit_count") or 0) + incident[sid]
        keep, lose = (prev, sid) if prev_score >= cur_score else (sid, prev)
        best_by_sig[group_key] = keep
        remap[lose] = keep
        drop_ids.add(lose)
        keep_meta = cluster_meta.get(keep) or {}
        lose_meta = cluster_meta.get(lose) or {}
        keep_meta["visit_count"] = int(keep_meta.get("visit_count") or 0) + int(
            lose_meta.get("visit_count") or 0
        )
        cluster_meta[keep] = keep_meta

    if not remap:
        return states, edges, wireframes, {}

    def _resolve(sid: str) -> str:
        seen: set[str] = set()
        while sid in remap and sid not in seen:
            seen.add(sid)
            sid = remap[sid]
        return sid

    new_states = [st for st in states if str(st.get("id") or "") not in drop_ids]
    new_edges: list[dict[str, Any]] = []
    seen_pairs: set[tuple[str, str, str]] = set()
    for ed in edges:
        e = dict(ed)
        f = _resolve(str(e.get("from") or ""))
        t = _resolve(str(e.get("to") or ""))
        if not f or not t or f == t or f in drop_ids or t in drop_ids:
            continue
        e["from"] = f
        e["to"] = t
        kind = str(e.get("kind") or "nav")
        pair = (f, t, kind)
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        new_edges.append(e)

    new_wf = {k: v for k, v in wireframes.items() if k not in drop_ids}
    return new_states, new_edges, new_wf, remap


def _resolve_remapped_state_id(sid: str, remap: dict[str, str]) -> str:
    seen: set[str] = set()
    while sid in remap and sid not in seen:
        seen.add(sid)
        sid = remap[sid]
    return sid


def _region_bottom_score(region: dict[str, Any]) -> float:
    rect = region.get("rect") if isinstance(region.get("rect"), dict) else {}
    y = float(rect.get("y") or 0)
    h = float(rect.get("h") or 0)
    return y + h


def _region_eligible_for_nav_anchor(region: dict[str, Any]) -> bool:
    rect = region.get("rect") if isinstance(region.get("rect"), dict) else {}
    w = float(rect.get("w") or 0)
    h = float(rect.get("h") or 0)
    if w * h > 0.22:
        return False
    if w > 0.88 and h > 0.35:
        return False
    if h > 0.55 and w > 0.45:
        return False
    return True


def _region_hotspot_key(region: dict[str, Any]) -> str:
    return f"{region.get('source') or 'r'}-{region.get('id') or 0}"


def _hotspot_target_id(state_id: str, region_key: str) -> str:
    return f"{state_id}::{region_key}"


def _nav_label_match_needles(action_label: str) -> list[str]:
    """从边 action_label 提取可用于线框 region 匹配的短文案。"""
    raw = str(action_label or "").strip()
    if not raw:
        return []
    needles: list[str] = []
    for part in raw.replace("点击", "").split("·"):
        p = str(part).strip()
        if p and p not in ("进入", "返回", "Tab", "tab") and p not in needles:
            needles.append(p)
    if raw not in needles:
        needles.insert(0, raw)
    return needles[:6]


def _pick_tab_hotspot(prev_turn: dict[str, Any], target_tab: str) -> str:
    """底栏 Tab 文案对应的可点线框区域（供跨 Tab 跳转连线）。"""
    want = str(target_tab or "").strip()
    if not want:
        return ""
    wf = _turn_wireframe(prev_turn)
    chrome = wf.get("chrome") or {}
    from mino_nexus.services.nav_screen_layout import DEFAULT_CONTENT_BOTTOM

    bottom = float(chrome.get("bottom") or DEFAULT_CONTENT_BOTTOM)
    best = ""
    best_y = -1.0
    for region in wf.get("regions") or []:
        if not region.get("clickable"):
            continue
        rect = region.get("rect") if isinstance(region.get("rect"), dict) else {}
        try:
            ry = float(rect.get("y") or 0)
            rh = float(rect.get("h") or 0)
        except (TypeError, ValueError):
            continue
        if ry + rh * 0.5 < bottom - 0.02:
            continue
        label = str(region.get("label") or "").strip()
        if want in label or label in want:
            if ry > best_y:
                best_y = ry
                best = _region_hotspot_key(region)
    if best:
        return best
    for region in wf.get("regions") or []:
        if not region.get("clickable"):
            continue
        rect = region.get("rect") if isinstance(region.get("rect"), dict) else {}
        try:
            ry = float(rect.get("y") or 0)
        except (TypeError, ValueError):
            continue
        if ry < bottom - 0.02:
            continue
        rk = _region_hotspot_key(region)
        if rk and not best:
            best = rk
    return best


def _pick_transition_hotspot(prev_turn: dict[str, Any], action_label: str) -> str:
    """上一屏线框里最可能触发跳转的可点区域 id（供关系图锚点连线）。"""
    wf = _turn_wireframe(prev_turn)
    needles = _nav_label_match_needles(action_label)
    fallback = ""
    for region in wf.get("regions") or []:
        if not region.get("clickable"):
            continue
        rk = _region_hotspot_key(region)
        if not fallback:
            fallback = rk
        rlabel = str(region.get("label") or "").strip().split("\n")[0]
        if not needles or needles == ["进入"]:
            continue
        for needle in needles:
            if not needle or needle in ("进入", "返回"):
                continue
            if rlabel and (rlabel == needle or needle in rlabel or rlabel in needle):
                return rk
    if fallback:
        return fallback
    for region in wf.get("regions") or []:
        if region.get("clickable"):
            rk = _region_hotspot_key(region)
            if rk:
                return rk
    return ""


def _sanitize_wireframe_nav_regions(wireframes: dict[str, Any]) -> None:
    """去掉整屏 region 上的 nav_to，避免 Studio 锚在屏幕中心。"""
    for _sid, wf in wireframes.items():
        if not isinstance(wf, dict):
            continue
        cleaned: list[dict[str, Any]] = []
        for region in wf.get("regions") or []:
            if not isinstance(region, dict):
                continue
            r = dict(region)
            if r.get("nav_to") and not _region_eligible_for_nav_anchor(r):
                r.pop("nav_to", None)
                if not r.get("clickable"):
                    r["clickable"] = False
            cleaned.append(r)
        wf["regions"] = cleaned


def _annotate_wireframes_from_edges(
    wireframes: dict[str, Any],
    edges: list[dict[str, Any]],
) -> None:
    """把出边挂到线框区域 nav_to，供 Studio 从控件连到目标页。"""
    for ed in edges:
        if str(ed.get("kind") or "") != "nav":
            continue
        src = str(ed.get("from") or "")
        dst = str(ed.get("to") or "")
        if not src or not dst:
            continue
        meta = ed.get("meta") if isinstance(ed.get("meta"), dict) else {}
        hs = str(meta.get("from_hotspot_id") or "")
        wf = wireframes.get(src)
        if not isinstance(wf, dict):
            continue
        regions = list(wf.get("regions") or [])
        region_key = ""
        if "::" in hs:
            region_key = hs.split("::", 1)[1]
        assigned = False
        for region in regions:
            rk = _region_hotspot_key(region)
            if region_key and rk != region_key:
                continue
            if not _region_eligible_for_nav_anchor(region):
                continue
            if region_key or region.get("clickable") or _region_bottom_score(region) >= 0.58:
                if region.get("nav_to") and str(region.get("nav_to")) != dst:
                    continue
                region["nav_to"] = dst
                region["clickable"] = True
                if meta.get("action_label"):
                    region["nav_label"] = str(meta.get("action_label"))
                assigned = True
                break
        if not assigned and not regions:
            syn_id = f"nav-anchor-{dst.split('.')[-1]}"
            regions.append(
                {
                    "source": "nav_hint",
                    "id": syn_id,
                    "label": str(meta.get("action_label") or "进入"),
                    "clickable": True,
                    "nav_to": dst,
                    "nav_label": str(meta.get("action_label") or "进入"),
                    "rect": {"x": 0.12, "y": 0.72, "w": 0.76, "h": 0.07},
                }
            )
            assigned = True
        elif not assigned and regions:
            action = str(meta.get("action_label") or "")
            action_type = str(meta.get("action_type") or "").lower()
            tab_needle = ""
            if action_type == "tab" or "Tab" in action or "tab" in action.lower():
                parts = action.replace("切换", "").split("·")
                if parts:
                    tab_needle = str(parts[-1]).strip()
            if not tab_needle:
                for needle in _nav_label_match_needles(action):
                    if needle and needle not in ("进入", "返回"):
                        tab_needle = needle
                        break
            if tab_needle:
                for region in regions:
                    if not _region_eligible_for_nav_anchor(region):
                        continue
                    lab = str(region.get("label") or "").strip().split("\n")[0]
                    if lab != tab_needle and tab_needle not in lab:
                        continue
                    if region.get("nav_to") and str(region.get("nav_to")) != dst:
                        continue
                    region["nav_to"] = dst
                    region["clickable"] = True
                    region["nav_label"] = action or lab
                    rk = _region_hotspot_key(region)
                    if rk and not hs:
                        meta = dict(meta)
                        meta["from_hotspot_id"] = _hotspot_target_id(src, rk)
                        ed["meta"] = meta
                    assigned = True
                    break
            if assigned:
                wf["regions"] = regions
                wireframes[src] = wf
                continue
            for needle in _nav_label_match_needles(action):
                if not needle or needle in ("进入", "返回"):
                    continue
                for region in regions:
                    if not _region_eligible_for_nav_anchor(region):
                        continue
                    lab = str(region.get("label") or "").strip().split("\n")[0]
                    if lab != needle and needle not in lab and lab not in needle:
                        continue
                    if region.get("nav_to") and str(region.get("nav_to")) != dst:
                        continue
                    region["nav_to"] = dst
                    region["clickable"] = True
                    region["nav_label"] = action or lab
                    rk = _region_hotspot_key(region)
                    if rk and not hs:
                        meta = dict(meta)
                        meta["from_hotspot_id"] = _hotspot_target_id(src, rk)
                        ed["meta"] = meta
                    assigned = True
                    break
                if assigned:
                    break
            if assigned:
                wf["regions"] = regions
                wireframes[src] = wf
                continue
            pool = sorted(
                [r for r in regions if _region_eligible_for_nav_anchor(r)],
                key=_region_bottom_score,
                reverse=True,
            )
            for region in pool:
                if region.get("nav_to") and str(region.get("nav_to")) != dst:
                    continue
                rk = _region_hotspot_key(region)
                if not rk:
                    continue
                region["nav_to"] = dst
                region["clickable"] = True
                if meta.get("action_label"):
                    region["nav_label"] = str(meta.get("action_label"))
                if not hs and rk:
                    meta["from_hotspot_id"] = _hotspot_target_id(src, rk)
                assigned = True
                break
        wf["regions"] = regions
        wireframes[src] = wf
        if assigned and not hs:
            rk_pick = ""
            for region in regions:
                if str(region.get("nav_to") or "") == dst:
                    rk_pick = _region_hotspot_key(region)
                    break
            if rk_pick:
                meta = dict(meta)
                meta["from_hotspot_id"] = _hotspot_target_id(src, rk_pick)
                ed["meta"] = meta
    _sanitize_wireframe_nav_regions(wireframes)


def _pick_tab_entry_sid(
    tab: str,
    state_key_to_id: dict[Any, str],
    cluster_meta: dict[str, dict[str, Any]] | None = None,
) -> str:
    keys = [k for k in state_key_to_id if isinstance(k, tuple) and k[0] == tab]
    if not keys:
        return ""
    meta_map = cluster_meta or {}

    def _rank(key: tuple[str, ...]) -> tuple[int, int, str]:
        sid = str(state_key_to_id.get(key) or "")
        cm = meta_map.get(sid) or {}
        fw = dict(cm.get("framework") or {})
        kind = str(fw.get("kind") or "").strip()
        if str(cm.get("cluster_surface") or "") == "main":
            pri = 0
        elif kind == "tab_shell":
            pri = 90
        elif kind in ("profile_page", "feed_grid", "content_page", "feed_list"):
            pri = 15
        else:
            pri = 40
        vc = -int(cm.get("visit_count") or 0)
        sk = str(key[1] if len(key) > 1 else "")
        return pri, vc, sk

    keys.sort(key=_rank)
    return str(state_key_to_id.get(keys[0]) or "")


def _tab_label_edge_slug(label: str) -> str:
    slug = re.sub(r"\s+", "_", str(label or "").strip())[:16]
    return re.sub(r"[^\w\u4e00-\u9fff]", "", slug) or "tab"


def _infer_tab_root_state_ids(
    timeline: list[dict[str, Any]],
    turn_cluster_ids: list[str],
    *,
    y_tab: int,
    known_labels: list[str],
    cluster_meta: dict[str, dict[str, Any]],
) -> dict[str, str]:
    """底栏 Tab 标签 → 该 Tab 下最常访问的 cluster state（架构图弱 nav 端点）。"""
    from collections import Counter

    from mino_nexus.services.nav_atlas_naming import sidebar_selections

    counts: dict[str, Counter[str]] = {t: Counter() for t in known_labels}
    for i, row in enumerate(timeline):
        if i >= len(turn_cluster_ids):
            break
        sid = str(turn_cluster_ids[i] or "")
        if not sid:
            continue
        tab = str(
            sidebar_selections(row["turn"], y_tab=y_tab, known_labels=known_labels).get("bottom") or ""
        ).strip()
        if tab and tab in counts:
            counts[tab][sid] += 1
    roots: dict[str, str] = {}
    for tab, ctr in counts.items():
        if not ctr:
            continue
        best = ""
        best_rank = (999, 0)
        for sid, visit_n in ctr.most_common(12):
            cm = cluster_meta.get(sid) or {}
            fw = dict(cm.get("framework") or {})
            kind = str(fw.get("kind") or "").strip()
            if str(cm.get("cluster_surface") or "") == "main":
                pri = 0
            elif kind in ("profile_page", "feed_grid", "content_page", "feed_list"):
                pri = 15
            else:
                pri = 40
            rank = (pri, -int(visit_n))
            if rank < best_rank:
                best_rank = rank
                best = sid
        if best:
            roots[tab] = best
    return roots


def _find_tab_switch_hotspot(
    timeline: list[dict[str, Any]],
    turn_cluster_ids: list[str],
    *,
    src_sid: str,
    target_tab: str,
    y_tab: int,
    known_labels: list[str],
) -> str:
    """在「当前为 src 屏、下一步切到 target_tab」的 turn 上取底栏热点。"""
    want = str(target_tab or "").strip()
    if not src_sid or not want:
        return ""
    for i in range(len(timeline) - 1):
        if i >= len(turn_cluster_ids):
            break
        if str(turn_cluster_ids[i] or "") != src_sid:
            continue
        rk = _pick_tab_hotspot(timeline[i]["turn"], want)
        if rk:
            return rk
    return ""


def _add_atlas_tab_bar_weak_nav_edges(
    edges: list[dict[str, Any]],
    *,
    tab_labels: list[str],
    tab_roots: dict[str, str],
    timeline: list[dict[str, Any]],
    turn_cluster_ids: list[str],
    y_tab: int,
    known_labels: list[str],
    add_edge: Any,
) -> None:
    """Tab 根页之间补 weak nav（edge.atlas.tab.*，Studio 会画线，非 edge.tab.*）。"""
    existing: set[tuple[str, str]] = set()
    for ed in edges:
        if str(ed.get("kind") or "") != "nav":
            continue
        src = str(ed.get("from") or "")
        dst = str(ed.get("to") or "")
        if src and dst:
            existing.add((src, dst))
    for src_tab in tab_labels:
        src = str(tab_roots.get(src_tab) or "")
        if not src:
            continue
        for dst_tab in tab_labels:
            if src_tab == dst_tab:
                continue
            dst = str(tab_roots.get(dst_tab) or "")
            if not dst or (src, dst) in existing:
                continue
            rk = _find_tab_switch_hotspot(
                timeline,
                turn_cluster_ids,
                src_sid=src,
                target_tab=dst_tab,
                y_tab=y_tab,
                known_labels=known_labels,
            )
            extra: dict[str, Any] = {
                "action_type": "tab",
                "source": "tab_bar_weak",
                "tab_target": dst_tab,
                "count": 0,
            }
            if rk:
                extra["from_hotspot_id"] = _hotspot_target_id(src, rk)
            add_edge(
                f"edge.atlas.tab.{_tab_label_edge_slug(src_tab)}_to_{_tab_label_edge_slug(dst_tab)}",
                src,
                dst,
                action_label=_format_nav_action_label("tab", dst_tab),
                extra_meta=extra,
            )
            existing.add((src, dst))


def _fsm_state_is_atlas_noise(st: dict[str, Any]) -> bool:
    """旧 FSM 中仅由百分比/时间等易变文案命中的状态，不补进 Screen Atlas。"""
    from mino_nexus.services.nav_layout import identify_is_volatile_only

    return identify_is_volatile_only(st)


def _scroll_delta_px(prev_nodes: list[dict[str, Any]], cur_nodes: list[dict[str, Any]]) -> int:
    def _sig(nodes: list[dict[str, Any]]) -> tuple[int, int] | None:
        for node in nodes or []:
            text = str(node.get("text") or "").strip()
            b = node.get("bounds") or []
            if not text or not isinstance(b, (list, tuple)) or len(b) < 4:
                continue
            if len(text) > 32:
                continue
            return int(b[1]), int(b[3])
        return None

    a, b = _sig(prev_nodes), _sig(cur_nodes)
    if not a or not b:
        return 0
    return abs(int(a[0]) - int(b[0]))


def _turn_cap_id(turn: dict[str, Any]) -> str:
    return str(turn.get("cap_id") or "").strip()


# Atlas 观测边：仅保留真实设备操作造成的相邻帧跳转（见 docs/9月16日—架构图连线与布局问题梳理.md）
_ATLAS_OBSERVED_NAV_CAPS = frozenset(
    {
        "tap_element",
        "multi_tap",
        "swipe_element_to_element",
        "swipe_direction",
        "long_press_element",
        "press_key",
    }
)
_ATLAS_OBSERVED_NAV_BLOCKLIST = frozenset(
    {
        "fsm_navigate",
        "recover_fsm_navigate",
        "assert_visual",
        "wait_ms",
        "noop",
        "exec_script",
        "relogin",
        "logout",
        "signal_done",
        "signal_skip",
        "signal_give_up",
        "signal_ask_human",
    }
)


def _press_key_is_physical_back(turn: dict[str, Any]) -> bool:
    if _turn_cap_id(turn) != "press_key":
        return False
    key = str(turn.get("action_key") or turn.get("selector_text") or "").strip().upper()
    if not key:
        return False
    return key in ("BACK", "ESCAPE", "KEYCODE_BACK", "4")


def _atlas_observed_nav_eligible(
    prev_turn: dict[str, Any],
    *,
    prev_sid: str,
    next_sid: str,
) -> bool:
    """Turn i 上执行的动作是否可作为 i→i+1 的架构 nav 观测边。"""
    if not prev_sid or not next_sid or prev_sid == next_sid:
        return False
    cap = _turn_cap_id(prev_turn)
    if not cap or cap.startswith("recover_") or cap in _ATLAS_OBSERVED_NAV_BLOCKLIST:
        return False
    if cap not in _ATLAS_OBSERVED_NAV_CAPS:
        return False
    if cap == "press_key":
        return _press_key_is_physical_back(prev_turn)
    return True


def _infer_transition_action(
    prev_turn: dict[str, Any],
    cur_turn: dict[str, Any],
    *,
    label: str,
) -> str:
    if _press_key_is_physical_back(prev_turn):
        return "back"
    if _turn_cap_id(prev_turn) == "swipe_direction":
        return "swipe"
    if str(label or "").strip() == "返回":
        return "back"
    from mino_nexus.services.nav_flow_blocks import action_label_indicates_ui_back

    if action_label_indicates_ui_back(str(label or "")):
        return "back"
    from mino_nexus.services.nav_synthesis import _horizontal_tab_row_labels

    prev_tabs = _horizontal_tab_row_labels(prev_turn)
    cur_tabs = _horizontal_tab_row_labels(cur_turn)
    if len(prev_tabs) >= 2 and len(cur_tabs) >= 2 and prev_tabs != cur_tabs:
        return "tab"
    if _scroll_delta_px(prev_turn.get("nodes") or [], cur_turn.get("nodes") or []) >= 48:
        return "swipe"
    return "tap"


def _format_nav_action_label(action_type: str, label: str) -> str:
    kind = str(action_type or "tap").strip()
    text = str(label or "进入").strip() or "进入"
    prefix = {
        "tap": "点击",
        "swipe": "滑动",
        "back": "返回",
        "tab": "切换 Tab",
    }.get(kind, "点击")
    if text in ("进入", "返回") or text == prefix:
        return prefix
    if text.startswith(prefix):
        return text
    return f"{prefix} · {text}"


def _infer_transition_label(prev_turn: dict[str, Any], cur_turn: dict[str, Any]) -> str:
    """优先用本步实际点过的文案；没有再从上一屏可点区域猜。"""
    sel = str(prev_turn.get("selector_text") or "").strip()
    if 1 <= len(sel) <= 24:
        return sel
    wf = _turn_wireframe(prev_turn)
    for region in wf.get("regions") or []:
        if not region.get("clickable"):
            continue
        label = str(region.get("label") or "").strip()
        if 2 <= len(label) <= 24:
            return label
    nodes = prev_turn.get("nodes") or []
    for node in reversed(nodes):
        if not (node.get("clickable") or node.get("enabled", True)):
            continue
        text = str(node.get("text") or node.get("content_desc") or "").strip()
        if 2 <= len(text) <= 24:
            return text
    return "进入"


def _timeline_session_index(timeline: list[dict[str, Any]]) -> dict[tuple[str, int], int]:
    out: dict[tuple[str, int], int] = {}
    for i, row in enumerate(timeline):
        turn = row.get("turn") or {}
        out[(str(turn.get("session_id") or ""), int(turn.get("turn_id") or 0))] = i
    return out


def _turn_nav_target_token(cur_turn: dict[str, Any], *, app_id: str = "") -> str:
    from mino_nexus.services.nav_app_skeleton import (
        shell_cluster_signature,
        wireframe_structure_signature,
    )

    wf = _turn_wireframe(cur_turn, app_id=app_id)
    tok = wireframe_structure_signature(wf)
    if tok != "empty":
        return tok
    sh = shell_cluster_signature(wf)
    if sh != "empty":
        return sh
    return f"turn:{int(cur_turn.get('turn_id') or 0)}"


def _observed_click_nav_signature_from_turn_pair(
    prev_turn: dict[str, Any],
    cur_turn: dict[str, Any],
    *,
    app_id: str = "",
) -> str | None:
    if str(prev_turn.get("session_id") or "") != str(cur_turn.get("session_id") or ""):
        return None
    cap = _turn_cap_id(prev_turn)
    if not cap or cap.startswith("recover_") or cap in _ATLAS_OBSERVED_NAV_BLOCKLIST:
        return None
    if cap not in _ATLAS_OBSERVED_NAV_CAPS:
        return None
    if cap == "press_key" and not _press_key_is_physical_back(prev_turn):
        return None
    label = _infer_transition_label(prev_turn, cur_turn)
    action = _infer_transition_action(prev_turn, cur_turn, label=label)
    if action == "swipe":
        return None
    tok = _turn_nav_target_token(cur_turn, app_id=app_id)
    return f"{action}:{tok}"


def _bucket_observed_click_nav_signature(
    bucket: dict[str, Any],
    timeline: list[dict[str, Any]],
    sess_idx: dict[tuple[str, int], int],
    *,
    app_id: str = "",
) -> frozenset[str]:
    sig: set[str] = set()
    for turn in bucket.get("sample_turns") or []:
        if not isinstance(turn, dict):
            continue
        key = (str(turn.get("session_id") or ""), int(turn.get("turn_id") or 0))
        i = sess_idx.get(key)
        if i is None or i + 1 >= len(timeline):
            continue
        cur_turn = (timeline[i + 1].get("turn") or {}) if isinstance(timeline[i + 1], dict) else {}
        piece = _observed_click_nav_signature_from_turn_pair(turn, cur_turn, app_id=app_id)
        if piece:
            sig.add(piece)
    return frozenset(sig)


def _nav_click_signatures_compatible(a: frozenset[str], b: frozenset[str]) -> bool:
    """点击/Tab/返回导致的出边签名一致才允许并页；任一侧有点击语义且不一致则禁止。"""
    if a == b:
        return True
    click_a = {x for x in a if x.startswith(("tap:", "tab:", "back:"))}
    click_b = {x for x in b if x.startswith(("tap:", "tab:", "back:"))}
    if click_a or click_b:
        return click_a == click_b and a == b
    return True


def _build_state_click_nav_signatures(
    timeline: list[dict[str, Any]],
    turn_cluster_ids: list[str],
    *,
    app_id: str = "",
) -> dict[str, frozenset[str]]:
    sigs: dict[str, set[str]] = {}
    for i in range(len(timeline) - 1):
        if i + 1 >= len(turn_cluster_ids):
            break
        src = str(turn_cluster_ids[i] or "")
        dst = str(turn_cluster_ids[i + 1] or "")
        if not src or not dst or src == dst:
            continue
        prev_turn = timeline[i]["turn"]
        cur_turn = timeline[i + 1]["turn"]
        if not _atlas_observed_nav_eligible(prev_turn, prev_sid=src, next_sid=dst):
            continue
        piece = _observed_click_nav_signature_from_turn_pair(prev_turn, cur_turn, app_id=app_id)
        if piece:
            sigs.setdefault(src, set()).add(piece)
    return {k: frozenset(v) for k, v in sigs.items()}


def _turn_wireframe(turn: dict[str, Any], *, app_id: str = "") -> dict[str, Any]:
    from mino_nexus.services.nav_live_graph import _turn_wireframe as build_turn_wireframe

    return build_turn_wireframe(turn, app_id=app_id)


def atlas_page_label(
    turn: dict[str, Any],
    *,
    y_tab: int,
    tab: str = "",
    exclude: set[str] | None = None,
    page_role: str = "",
) -> tuple[str, dict[str, Any], list[str]]:
    """逻辑页名 + 框架/chrome（chrome 仅进 identify，不进标题）。"""
    from mino_nexus.services.nav_layout import detect_layout_framework, is_volatile_text, stable_chrome_texts

    exclude = exclude or set()
    fw = detect_layout_framework(turn, y_tab_max=y_tab, exclude=exclude)
    chrome = stable_chrome_texts(turn, y_tab_max=y_tab, exclude=exclude)
    stable = [t for t in chrome if t and not is_volatile_text(t)]
    role = str(page_role or "").strip()
    label = _logical_page_title(str(tab or "").strip(), role, fw)
    return label, fw, stable


def _relation_layout(
    tab_labels: list[str],
    tab_entry_id: dict[str, str],
    subs_by_tab: dict[str, list[str]],
    *,
    extra_state_ids: list[str] | None = None,
) -> dict[str, dict[str, int]]:
    """Tab 分行、入口在左、同 Tab 子页在右（与合成 Nav 一致，非时间序网格）。"""
    entry_x, node_w, h_gap = 48, 220, 88
    row_min_h, row_pad = 520, 64
    layout_states: dict[str, dict[str, int]] = {}
    row_y = 48
    for t in tab_labels:
        eid = tab_entry_id.get(t) or ""
        if not eid:
            continue
        subs = sorted(subs_by_tab.get(t) or [])
        layout_states[eid] = {"x": entry_x, "y": row_y}
        for i, sid in enumerate(subs):
            layout_states[sid] = {
                "x": entry_x + node_w + h_gap + i * (node_w + h_gap),
                "y": row_y,
            }
        row_y += row_min_h + row_pad
    orphan_i = 0
    for sid in extra_state_ids or []:
        if not sid or sid in layout_states:
            continue
        layout_states[sid] = {
            "x": entry_x + (orphan_i % 3) * 300,
            "y": row_y + (orphan_i // 3) * row_min_h,
        }
        orphan_i += 1
    return layout_states


LANE_SUFFIX_MERGE_JACCARD = 0.40


def _coalesce_lane_suffix_buckets(
    refined_buckets: dict[str, dict[str, Any]],
    *,
    timeline: list[dict[str, Any]] | None = None,
    app_id: str = "",
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """同一粗车道下 *-sN 子桶：代表线框互 Jaccard 够则并回一页（见 9月15日-架构聚类过并过拆复盘 §4）。"""
    import re

    from mino_nexus.services.nav_app_skeleton import merge_skeleton_wireframe, wireframe_jaccard

    fp_remap: dict[str, str] = {}
    if not refined_buckets:
        return refined_buckets, fp_remap

    sess_idx = _timeline_session_index(timeline or [])

    def _rep_wf(bucket: dict[str, Any]) -> dict[str, Any]:
        wfs = list(bucket.get("wireframes") or [])
        return merge_skeleton_wireframe(wfs) if wfs else {}

    def _merge_bucket(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
        out = dict(a)
        for field in (
            "frameworks",
            "chromes",
            "inferred_roles",
            "wireframes",
            "sample_turns",
            "name_samples",
            "chrome_rows",
        ):
            out[field] = list(out.get(field) or []) + list(b.get(field) or [])
        morph = list(out.get("morph_wireframes") or []) + list(b.get("morph_wireframes") or [])
        if morph:
            out["morph_wireframes"] = morph
            out["morph_count"] = max(
                int(out.get("morph_count") or 0),
                int(b.get("morph_count") or 0),
                len(morph) - 1,
            )
        return out

    groups: dict[str, list[str]] = {}
    for key in refined_buckets:
        base = re.sub(r"-s\d+$", "", str(key))
        groups.setdefault(base, []).append(str(key))

    out = dict(refined_buckets)
    for base, keys in groups.items():
        uniq = sorted(set(keys))
        if len(uniq) <= 1:
            continue
        reps = {k: _rep_wf(out[k]) for k in uniq}

        def _max_cross_jaccard(ka: str, kb: str) -> float:
            wfa = list(out[ka].get("wireframes") or [])[:16]
            wfb = list(out[kb].get("wireframes") or [])[:16]
            if not wfa or not wfb:
                return wireframe_jaccard(reps[ka], reps[kb])
            best = 0.0
            for a in wfa:
                for b in wfb:
                    best = max(best, wireframe_jaccard(a, b))
            return best

        parent = {k: k for k in uniq}

        def _find(x: str) -> str:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def _union(a: str, b: str) -> None:
            ra, rb = _find(a), _find(b)
            if ra != rb:
                parent[rb] = ra

        def _may_union(ki: str, kj: str) -> bool:
            j = _max_cross_jaccard(ki, kj)
            sig_a = _bucket_observed_click_nav_signature(
                out[ki], timeline or [], sess_idx, app_id=app_id
            )
            sig_b = _bucket_observed_click_nav_signature(
                out[kj], timeline or [], sess_idx, app_id=app_id
            )
            if not _nav_click_signatures_compatible(sig_a, sig_b):
                return False
            rep_a = _rep_wf(out[ki])
            rep_b = _rep_wf(out[kj])
            from mino_nexus.services.nav_app_skeleton import (
                VIEWPORT_TRANSFORM_JACCARD_MIN,
                wireframe_aligned_transform_jaccard,
                wireframes_same_page_under_viewport_transform,
            )

            if wireframes_same_page_under_viewport_transform(rep_a, rep_b):
                return True
            if wireframe_aligned_transform_jaccard(rep_a, rep_b) >= VIEWPORT_TRANSFORM_JACCARD_MIN:
                return True
            if j >= LANE_SUFFIX_MERGE_JACCARD:
                return True
            from mino_nexus.services.nav_app_skeleton import SCROLL_MORPH_JACCARD_MIN

            if j >= SCROLL_MORPH_JACCARD_MIN and not sig_a and not sig_b:
                return True
            return False

        for i, ki in enumerate(uniq):
            for kj in uniq[i + 1 :]:
                if _may_union(ki, kj):
                    _union(ki, kj)

        clusters: dict[str, list[str]] = {}
        for k in uniq:
            clusters.setdefault(_find(k), []).append(k)

        for members in clusters.values():
            if len(members) <= 1:
                continue
            canon = base if base in members else sorted(members)[0]
            merged = dict(out[canon])
            for k in members:
                if k == canon:
                    continue
                fp_remap[k] = canon
                merged = _merge_bucket(merged, out[k])
                del out[k]
            merged["skeleton_fp"] = canon
            out[canon] = merged
    return out, fp_remap


def _atlas_cluster_doc(
    app_id: str,
    filtered: list[dict[str, Any]],
    *,
    project_id: str,
    y_tab: int,
    cap_meta: dict[str, Any],
) -> dict[str, Any] | None:
    """按骨骼 lane + 线框相似度合并为关系结构（state_id=page.sk*，Tab 仅用于边标签/命名）。"""
    from mino_nexus.services.nav_layout import (
        detect_layout_framework,
        framework_fingerprint,
        merge_frameworks,
        semantic_page_role,
        stable_chrome_texts,
    )
    from mino_nexus.services.nav_synthesis import (
        _build_identify_block,
        _is_bad_tab_slot_label,
        _slug_semantic_sub,
        _slug_tab,
        extract_tab_bar_labels,
        extract_tab_bar_slots,
    )
    from mino_nexus.services.nav_target_scope import resolve_app_target_scope

    if len(filtered) < 1:
        return None

    scope = resolve_app_target_scope(app_id)
    tab_slots = extract_tab_bar_slots(filtered, scope=scope, app_id=app_id)
    tab_labels = extract_tab_bar_labels(filtered, scope=scope, app_id=app_id)
    tab_labels = [t for t in tab_labels if not _is_bad_tab_slot_label(t)]
    known_labels = list(tab_labels)
    exclude_tabs = set(known_labels)

    from mino_nexus.services.nav_atlas_naming import page_label_from_turn
    from mino_nexus.services.nav_tab_prefs import resolve_home_tab_label

    home_tab = resolve_home_tab_label(app_id, known_labels) if known_labels else ""

    timeline: list[dict[str, Any]] = []
    for turn in filtered:
        fw = detect_layout_framework(turn, y_tab_max=y_tab, exclude=exclude_tabs)
        from mino_nexus.services.nav_layout import filter_app_chrome_texts

        chrome = filter_app_chrome_texts(
            stable_chrome_texts(turn, y_tab_max=y_tab, exclude=exclude_tabs)
        )
        fp, chrome_key = framework_fingerprint(fw, chrome)
        timeline.append(
            {
                "turn": turn,
                "framework": fw,
                "chrome": chrome,
                "fp": fp,
                "chrome_key": chrome_key,
            }
        )

    used_ids: set[str] = set()
    tab_entry_id: dict[str, str] = {}
    state_key_to_id: dict[str, str] = {}
    screen_rows: list[dict[str, Any]] = []
    cluster_meta: dict[str, dict[str, Any]] = {}
    buckets: dict[str, dict[str, Any]] = {}

    for row in timeline:
        fw = dict(row.get("framework") or {})
        role = semantic_page_role(fw)
        turn = row["turn"]
        wf = _turn_wireframe(turn, app_id=app_id)
        row_with_turn = {**row, "turn": turn}
        skeleton_fp = _atlas_layout_cluster_key(
            row_with_turn,
            wf,
            role,
            tab="",
            y_tab=y_tab,
            exclude_tabs=exclude_tabs,
            tab_labels=known_labels,
        )
        bucket = buckets.setdefault(
            skeleton_fp,
            {
                "frameworks": [],
                "chromes": [],
                "skeleton_fp": skeleton_fp,
                "inferred_roles": [],
                "wireframes": [],
                "sample_turns": [],
                "name_samples": [],
                "chrome_rows": [],
            },
        )
        bucket["frameworks"].append(fw)
        bucket["chrome_rows"].append(list(row.get("chrome") or []))
        bucket["inferred_roles"].append(role)
        bucket["sample_turns"].append(turn)
        if isinstance(wf, dict):
            bucket["wireframes"].append(wf)
        for text in row.get("chrome") or []:
            val = str(text or "").strip()
            if val and val not in bucket["chromes"]:
                bucket["chromes"].append(val)
        nm = page_label_from_turn(turn, y_tab=y_tab, known_labels=known_labels, wireframe=wf)
        if nm:
            bucket["name_samples"].append(nm)

    from mino_nexus.services.nav_atlas_naming import merge_cluster_display_name
    from mino_nexus.services.nav_app_skeleton import (
        merge_skeleton_wireframe,
        should_coalesce_morph_clusters,
        split_indices_by_wireframe_similarity,
    )

    refined_buckets: dict[str, dict[str, Any]] = {}
    turn_cluster_fp: dict[tuple[str, int], str] = {}
    split_i = 0
    for skeleton_fp, bucket in buckets.items():
        wfs = list(bucket.get("wireframes") or [])
        turns = list(bucket.get("sample_turns") or [])
        n = len(turns)
        if n != len(wfs):
            wfs = wfs[:n]
        sub_clusters = split_indices_by_wireframe_similarity(
            wfs,
            frameworks=list(bucket.get("frameworks") or [])[: len(wfs)],
        )
        morph_frames: list[dict[str, Any]] = []
        if should_coalesce_morph_clusters(wfs, sub_clusters):
            morph_frames = [wfs[i] for i in sorted({j for cl in sub_clusters for j in cl}) if i < len(wfs)]
            sub_clusters = [sorted({j for cl in sub_clusters for j in cl})]
        for cl in sub_clusters:
            use_fp = skeleton_fp if len(cl) == n else f"{skeleton_fp}-s{split_i}"
            if len(cl) != n:
                split_i += 1
            sub: dict[str, Any] = {
                "frameworks": [],
                "chromes": [],
                "skeleton_fp": use_fp,
                "inferred_roles": [],
                "wireframes": [],
                "sample_turns": [],
                "name_samples": [],
                "chrome_rows": [],
            }
            for idx in cl:
                sub["frameworks"].append(bucket["frameworks"][idx])
                sub["inferred_roles"].append(bucket["inferred_roles"][idx])
                t = bucket["sample_turns"][idx]
                sub["sample_turns"].append(t)
                for c in (bucket.get("chrome_rows") or [[]])[idx] if idx < len(bucket.get("chrome_rows") or []) else []:
                    if c and c not in sub["chromes"]:
                        sub["chromes"].append(c)
                if idx < len(wfs):
                    sub["wireframes"].append(wfs[idx])
                if idx < len(bucket.get("name_samples") or []):
                    sub["name_samples"].append(bucket["name_samples"][idx])
                sess = str(t.get("session_id") or "")
                tid = int(t.get("turn_id") or 0)
                if sess and tid:
                    turn_cluster_fp[(sess, tid)] = use_fp
            if morph_frames:
                sub["morph_wireframes"] = morph_frames
                sub["morph_count"] = max(0, len(morph_frames) - 1)
            refined_buckets[use_fp] = sub
    buckets, lane_fp_remap = _coalesce_lane_suffix_buckets(
        refined_buckets,
        timeline=timeline,
        app_id=app_id,
    )
    if lane_fp_remap:
        for key, sk_fp in list(turn_cluster_fp.items()):
            canon = sk_fp
            while canon in lane_fp_remap:
                canon = lane_fp_remap[canon]
            turn_cluster_fp[key] = canon

    for skeleton_fp, bucket in buckets.items():
        merged_fw = merge_frameworks(bucket["frameworks"])
        sid = _slug_atlas_page(skeleton_fp, used_ids)
        chromes = list(bucket["chromes"])[:8]
        roles = bucket.get("inferred_roles") or []
        inferred_role = roles[0] if roles else semantic_page_role(merged_fw)
        merged_wf = merge_skeleton_wireframe(list(bucket.get("wireframes") or []))
        display_name = merge_cluster_display_name(list(bucket.get("name_samples") or []))
        from mino_nexus.services.nav_viewport import classify_viewport_extent, evidence_tier_from_counts

        viewport = classify_viewport_extent(merged_fw, merged_wf)
        morph_count = int(bucket.get("morph_count") or 0)
        morph_wfs = list(bucket.get("morph_wireframes") or [])
        screen_rows.append(
            {
                "id": sid,
                "tab": "",
                "framework": merged_fw,
                "chrome": chromes,
                "is_entry": False,
                "page_role": inferred_role,
                "skeleton_fp": skeleton_fp,
            }
        )
        state_key_to_id[skeleton_fp] = sid
        cluster_meta[sid] = {
            "visit_count": 0,
            "wireframe": merged_wf,
            "turn_wireframes": list(bucket.get("wireframes") or []),
            "best_score": -1,
            "display_name": display_name,
            "tab": "",
            "page_role": inferred_role,
            "inferred_role": inferred_role,
            "chrome": chromes,
            "chrome_texts": chromes,
            "header_title": display_name,
            "skeleton_fp": skeleton_fp,
            "cluster_surface": "sub" if inferred_role == "detail" else "main",
            "framework": merged_fw,
            "name_samples": list(bucket.get("name_samples") or []),
            "layout_class": viewport.get("layout_class"),
            "layout_extent": viewport.get("layout_extent"),
            "morph_count": morph_count,
            "morphs": morph_wfs[:12] if morph_wfs else [],
            "evidence_tier": evidence_tier_from_counts(visit_count=0, morph_count=morph_count),
        }

    pin_map, pin_meta = _load_atlas_capture_pins(app_id)
    if pin_map:
        _inject_pinned_capture_states(
            screen_rows,
            cluster_meta,
            state_key_to_id,
            pin_map,
            pin_meta,
            app_id=app_id,
            y_tab=y_tab,
            tab_labels=known_labels,
            exclude_tabs=exclude_tabs,
            home_tab=home_tab,
        )

    turn_cluster_ids: list[str] = []
    atlas_turn_refs: list[dict[str, Any]] = []
    for row in timeline:
        role = semantic_page_role(row["framework"])
        turn = row["turn"]
        wf = _turn_wireframe(turn, app_id=app_id)
        pk = _capture_pin_key(str(turn.get("session_id") or ""), int(turn.get("turn_id") or 0))
        if pk in pin_map:
            sid = pin_map[pk]
        else:
            sess = str(turn.get("session_id") or "")
            tid = int(turn.get("turn_id") or 0)
            sk_fp = turn_cluster_fp.get((sess, tid))
            if not sk_fp:
                row_with_turn = {**row, "turn": turn}
                sk_fp = _atlas_layout_cluster_key(
                    row_with_turn,
                    wf,
                    role,
                    tab="",
                    y_tab=y_tab,
                    exclude_tabs=exclude_tabs,
                    tab_labels=known_labels,
                )
            sid = state_key_to_id.get(sk_fp) or ""
        turn_cluster_ids.append(sid)
        if sid:
            atlas_turn_refs.append(
                {
                    "state_id": sid,
                    "session_id": str(turn.get("session_id") or ""),
                    "turn_id": int(turn.get("turn_id") or 0),
                    "at": int(turn.get("at") or 0),
                    "platform": str(turn.get("platform") or ""),
                    "target_package": str(
                        turn.get("run_target_package")
                        or turn.get("target_package")
                        or ""
                    ),
                    "env_surface": str(turn.get("env_surface") or ""),
                    "env_profile": str(turn.get("env_profile") or ""),
                }
            )
        score = len(wf.get("regions") or []) * 1000 + int(turn.get("at") or 0)
        meta = cluster_meta.get(sid)
        if not meta:
            continue
        meta["visit_count"] = int(meta.get("visit_count") or 0) + 1
        if isinstance(wf, dict):
            meta.setdefault("turn_wireframes", []).append(wf)
        nm = page_label_from_turn(
            turn,
            y_tab=y_tab,
            known_labels=known_labels,
            user_display_name=str(meta.get("user_display_name") or ""),
            wireframe=wf,
        )
        if nm:
            meta.setdefault("name_samples", []).append(nm)
        if score >= int(meta.get("best_score") or -1):
            meta["best_score"] = score
            meta["inferred_role"] = role
            chromes = list(meta.get("chrome_texts") or meta.get("chrome") or [])
            for text in row.get("chrome") or []:
                val = str(text or "").strip()
                if val and val not in chromes:
                    chromes.append(val)
            meta["chrome_texts"] = chromes[:8]
            meta["chrome"] = chromes[:8]
            meta["display_name"] = merge_cluster_display_name(
                list(meta.get("name_samples") or []),
                user_display_name=str(meta.get("user_display_name") or ""),
            )
            meta["header_title"] = meta["display_name"]

    screen_rows, atlas_wf_remap = _merge_wireframe_duplicate_clusters(
        screen_rows,
        cluster_meta,
        turn_cluster_ids,
        timeline=timeline,
        app_id=app_id,
    )
    if atlas_wf_remap:
        for i, ref in enumerate(atlas_turn_refs):
            sid = str(ref.get("state_id") or "")
            while sid in atlas_wf_remap:
                sid = atlas_wf_remap[sid]
            if sid:
                atlas_turn_refs[i] = {**ref, "state_id": sid}
    from mino_nexus.services.nav_app_skeleton import merge_skeleton_wireframe

    for sid, meta in cluster_meta.items():
        turns_wf = list(meta.get("turn_wireframes") or [])
        if turns_wf:
            from mino_nexus.services.nav_app_skeleton import (
                annotate_morph_axes_from_turn_samples,
                merge_display_wireframe_for_atlas,
                merge_skeleton_wireframe,
            )

            display = merge_display_wireframe_for_atlas(turns_wf)
            merged = merge_skeleton_wireframe(turns_wf)
            if display and (display.get("regions") or []):
                annotate_morph_axes_from_turn_samples(
                    display,
                    turns_wf,
                    layout_class=str(meta.get("layout_class") or ""),
                    layout_extent=meta.get("layout_extent")
                    if isinstance(meta.get("layout_extent"), dict)
                    else None,
                )
                v_cnt = sum(
                    1
                    for r in (display.get("regions") or [])
                    if isinstance(r, dict) and str(r.get("morph_axis") or "") == "vertical"
                )
                h_cnt = sum(
                    1
                    for r in (display.get("regions") or [])
                    if isinstance(r, dict) and str(r.get("morph_axis") or "") == "horizontal"
                )
                meta["region_morph_vertical"] = v_cnt
                meta["region_morph_horizontal"] = h_cnt
                meta["wireframe"] = display
            elif merged:
                meta["wireframe"] = merged
            if str(meta.get("layout_class") or "") == "infinite_feed":
                from mino_nexus.services.nav_app_skeleton import count_scroll_morph_variants

                meta["morph_count"] = max(
                    int(meta.get("morph_count") or 0),
                    count_scroll_morph_variants(turns_wf),
                )
        morph_wfs = meta.get("morphs") or meta.get("morph_wireframes")
        if isinstance(morph_wfs, list):
            for mw in morph_wfs:
                if isinstance(mw, dict):
                    annotate_wireframe_region_morph(
                        mw,
                        layout_class=str(meta.get("layout_class") or ""),
                        layout_extent=meta.get("layout_extent")
                        if isinstance(meta.get("layout_extent"), dict)
                        else None,
                    )

    state_parents: dict[str, str] = {}
    for i, cid in enumerate(turn_cluster_ids):
        if not cid:
            continue
        row = timeline[i]
        turn = row["turn"]
        fw = dict(row.get("framework") or {})
        if fw.get("has_back") and i > 0:
            prev_c = turn_cluster_ids[i - 1]
            if prev_c and prev_c != cid:
                state_parents[cid] = prev_c
                continue
        from mino_nexus.services.nav_synthesis import _horizontal_tab_row_labels

        if len(_horizontal_tab_row_labels(turn)) < 2 and i > 0:
            prev_c = turn_cluster_ids[i - 1]
            if prev_c and prev_c != cid:
                state_parents[cid] = prev_c
                continue

    states: list[dict[str, Any]] = []
    wireframes: dict[str, Any] = {}
    for row in screen_rows:
        sid = str(row["id"] or "")
        meta = cluster_meta.get(sid) or {}
        is_entry = False
        display_name = str(meta.get("display_name") or "").strip() or sid
        wf = meta.get("wireframe")
        if isinstance(wf, dict):
            wireframes[sid] = wf
        states.append(
            {
                "id": sid,
                "kind": "page",
                "entry": is_entry,
                "identify": _build_identify_block(
                    tab=str(row.get("tab") or ""),
                    framework=dict(row.get("framework") or {}),
                    chrome=list(row.get("chrome") or []),
                    include_framework=not is_entry,
                    is_entry=is_entry,
                ),
                "guards": {},
                "meta": {
                    "screen_atlas": True,
                    "display_name": display_name,
                    "visit_count": int(meta.get("visit_count") or 0),
                    "page_role": str(row.get("page_role") or ""),
                    "inferred_role": str(meta.get("inferred_role") or row.get("page_role") or ""),
                    "tab": str(row.get("tab") or ""),
                    "parent_state_id": str(state_parents.get(sid) or ""),
                    "chrome_texts": list(meta.get("chrome_texts") or row.get("chrome") or [])[:8],
                    "header_title": str(meta.get("header_title") or ""),
                    "skeleton_fp": str(meta.get("skeleton_fp") or row.get("skeleton_fp") or ""),
                    "layout_class": str(meta.get("layout_class") or ""),
                    "layout_extent": meta.get("layout_extent") if isinstance(meta.get("layout_extent"), dict) else {},
                    "morph_count": int(meta.get("morph_count") or 0),
                    "region_morph_vertical": int(meta.get("region_morph_vertical") or 0),
                    "region_morph_horizontal": int(meta.get("region_morph_horizontal") or 0),
                    "evidence_tier": str(meta.get("evidence_tier") or ""),
                },
            }
        )

    edges: list[dict[str, Any]] = []
    seen_edge: set[str] = set()
    seen_pairs: set[tuple[str, str, str]] = set()

    def _add_edge(
        eid: str,
        src: str,
        dst: str,
        *,
        kind: str = "nav",
        note: str = "",
        action_label: str = "",
        extra_meta: dict[str, Any] | None = None,
    ) -> None:
        pair = (kind, src, dst)
        if not src or not dst or src == dst or eid in seen_edge or pair in seen_pairs:
            return
        seen_edge.add(eid)
        seen_pairs.add(pair)
        meta: dict[str, Any] = {"source": "screen_atlas", **(extra_meta or {})}
        if note:
            meta["note"] = note
        if action_label:
            meta["action_label"] = action_label
        if kind == "hierarchy":
            meta["display_only"] = True
        edges.append(
            {
                "id": eid,
                "kind": kind,
                "from": src,
                "to": dst,
                "guard": {},
                "execute": {"steps": ["tap_element"]},
                "effect_assert": {"within_ms": 8000, "require_any": [], "require_none": []},
                "on_fail": {},
                "meta": meta,
            }
        )

    nav_transitions: Counter[tuple[str, str]] = Counter()
    nav_labels: dict[tuple[str, str], str] = {}
    nav_action_types: dict[tuple[str, str], str] = {}
    nav_hotspots: dict[tuple[str, str], str] = {}
    from mino_nexus.services.nav_atlas_naming import sidebar_selections

    for i in range(len(turn_cluster_ids) - 1):
        a, b = turn_cluster_ids[i], turn_cluster_ids[i + 1]
        prev_turn = timeline[i]["turn"]
        if not _atlas_observed_nav_eligible(prev_turn, prev_sid=a, next_sid=b):
            continue
        side_a = sidebar_selections(
            timeline[i]["turn"], y_tab=y_tab, known_labels=known_labels
        )
        side_b = sidebar_selections(
            timeline[i + 1]["turn"], y_tab=y_tab, known_labels=known_labels
        )
        tab_a = str(side_a.get("bottom") or "")
        tab_b = str(side_b.get("bottom") or "")
        tab_switch = (
            tab_a
            and tab_b
            and tab_a != tab_b
            and _tab_switch_evidence(timeline[i]["turn"], tab_b, known_labels)
        )
        if a and b and a != b:
            nav_transitions[(a, b)] += 1
            pair = (a, b)
            if pair not in nav_labels:
                if tab_switch:
                    raw_label = tab_b
                    action_type = "tab"
                    label = _format_nav_action_label("tab", tab_b)
                    rk = _pick_tab_hotspot(timeline[i]["turn"], tab_b)
                else:
                    from mino_nexus.services.nav_synthesis import _is_bad_tab_slot_label

                    raw_label = _infer_transition_label(
                        timeline[i]["turn"], timeline[i + 1]["turn"]
                    )
                    if _is_bad_tab_slot_label(raw_label):
                        raw_label = "进入"
                    action_type = _infer_transition_action(
                        timeline[i]["turn"],
                        timeline[i + 1]["turn"],
                        label=raw_label,
                    )
                    if action_type == "tab" and raw_label not in known_labels:
                        action_type = "tap"
                    label = _format_nav_action_label(action_type, raw_label)
                    rk = _pick_transition_hotspot(timeline[i]["turn"], raw_label)
                nav_labels[pair] = label
                nav_action_types[pair] = action_type
                if rk:
                    nav_hotspots[pair] = _hotspot_target_id(a, rk)

    for st in states:
        sid = str(st.get("id") or "")
        parent = str((st.get("meta") or {}).get("parent_state_id") or "")
        if not sid or not parent or parent == sid:
            continue
        _add_edge(
            f"edge.atlas.hier.{parent.split('.')[-1]}_stack_{sid.split('.')[-1]}",
            parent,
            sid,
            kind="hierarchy",
            note="内页层级",
        )

    for (src, dst), count in nav_transitions.most_common(64):
        label = nav_labels.get((src, dst), "点击 · 进入")
        action_type = nav_action_types.get((src, dst), "tap")
        hs = nav_hotspots.get((src, dst), "")
        extra: dict[str, Any] = {"count": int(count), "action_type": action_type}
        if hs:
            extra["from_hotspot_id"] = hs
        _add_edge(
            f"edge.atlas.nav.{src.split('.')[-1]}_to_{dst.split('.')[-1]}",
            src,
            dst,
            action_label=label,
            extra_meta=extra,
        )
        if nav_transitions.get((dst, src)):
            back_label = nav_labels.get((dst, src), "返回")
            back_type = nav_action_types.get((dst, src), "back")
            _add_edge(
                f"edge.atlas.nav.{dst.split('.')[-1]}_back_{src.split('.')[-1]}",
                dst,
                src,
                action_label=back_label,
                extra_meta={
                    "count": int(nav_transitions[(dst, src)]),
                    "reverse": True,
                    "action_type": back_type,
                    "observed_back": True,
                },
            )

    states, edges, wireframes, prune_remap = _prune_orphan_duplicate_screens(
        states, edges, wireframes, cluster_meta
    )
    if prune_remap:
        turn_cluster_ids = [
            _resolve_remapped_state_id(cid, prune_remap) if cid else ""
            for cid in turn_cluster_ids
        ]
    for st in states:
        sid = str(st.get("id") or "")
        cm = cluster_meta.get(sid) or {}
        dn = str(cm.get("display_name") or "").strip()
        if dn:
            st.setdefault("meta", {})
            st["meta"]["display_name"] = dn
        samples = list(cm.get("name_samples") or [])
        if samples:
            st.setdefault("meta", {})
            aliases = [str(a).strip() for a in samples if str(a).strip() and str(a).strip() != dn]
            if aliases:
                st["meta"]["name_samples"] = aliases[:12]
                existing = st["meta"].get("aliases")
                if not isinstance(existing, list):
                    st["meta"]["aliases"] = aliases[:12]
    _annotate_wireframes_from_edges(wireframes, edges)

    tab_roots = _infer_tab_root_state_ids(
        timeline,
        turn_cluster_ids,
        y_tab=y_tab,
        known_labels=known_labels,
        cluster_meta=cluster_meta,
    )
    if len(tab_labels) >= 2 and tab_roots:
        for st in states:
            sid = str(st.get("id") or "")
            for tab, root_sid in tab_roots.items():
                if sid != root_sid:
                    continue
                st.setdefault("meta", {})
                st["meta"]["tab"] = tab
                if tab == home_tab:
                    st["entry"] = True

    _annotate_wireframes_from_edges(wireframes, edges)

    layout_states: dict[str, dict[str, int]] = {}
    for i, st in enumerate(states):
        sid = str(st.get("id") or "")
        if not sid:
            continue
        layout_states[sid] = {
            "x": 48 + (i % 4) * 280,
            "y": 48 + (i // 4) * 520,
        }
    home_id = str(tab_roots.get(home_tab) or "") if home_tab and tab_roots else ""
    if not home_id and states:
        home_id = str(states[0].get("id") or "")
    launch_id = ""
    for cid in turn_cluster_ids:
        if cid:
            launch_id = cid
            break

    screen_list = []
    for st in states:
        sid = str(st.get("id") or "")
        cm = cluster_meta.get(sid) or {}
        screen_list.append(
            {
                "screen_id": sid,
                "display_name": str((st.get("meta") or {}).get("display_name") or sid),
                "visit_count": int(cm.get("visit_count") or 0),
                "wireframe": wireframes.get(sid),
            }
        )

    import json

    state_ids = [str(s.get("id") or "") for s in states]
    content_src = json.dumps(
        {"states": state_ids, "edges": len(edges), "refs": len(atlas_turn_refs)},
        sort_keys=True,
        ensure_ascii=False,
    )
    atlas_content_hash = hashlib.sha256(content_src.encode("utf-8")).hexdigest()[:16]

    doc: dict[str, Any] = {
        "app_id": app_id,
        "project_id": project_id,
        "version": "v1",
        "meta": {
            "screen_atlas": True,
            "atlas_layout": "skeleton_grid",
            "atlas_built_at": int(time.time()),
            "atlas_content_hash": atlas_content_hash,
            "capture_turns": len(filtered),
            "capture_sessions": int(cap_meta.get("sessions") or 0),
            "atlas_turn_refs": atlas_turn_refs,
            "atlas_capture_pins": pin_map,
            "atlas_build_remap": dict(atlas_wf_remap) if atlas_wf_remap else {},
            "state_wireframes": wireframes,
            "studio_layout": {"states": layout_states},
            "tab_bar": {
                "entries": [tab_roots[t] for t in tab_labels if tab_roots.get(t)],
                "labels": {tab_roots[t]: t for t in tab_labels if tab_roots.get(t)},
                "home_state_id": home_id,
                "launch_state_id": launch_id or home_id,
                "home_tab_label": home_tab,
                "slots": tab_slots,
            },
        },
        "test_data": {},
        "states": states,
        "edges": edges,
    }
    from mino_nexus.services.nav_flow_blocks import enrich_atlas_doc

    enrich_atlas_doc(
        doc,
        timeline=timeline,
        turn_cluster_ids=turn_cluster_ids,
        nav_action_types=nav_action_types,
        cluster_meta=cluster_meta,
        fallback_layout=layout_states,
        app_id=app_id,
    )
    return {
        "doc": doc,
        "screen_list": screen_list,
        "states": states,
        "edges": edges,
        "_atlas_enrich": {
            "timeline": timeline,
            "turn_cluster_ids": turn_cluster_ids,
            "nav_action_types": nav_action_types,
            "cluster_meta": cluster_meta,
        },
    }


def _atlas_fallback_doc(
    app_id: str,
    filtered: list[dict[str, Any]],
    *,
    project_id: str,
    y_tab: int,
    cap_meta: dict[str, Any],
) -> dict[str, Any]:
    """无底栏 Tab 时：按结构指纹单节点聚类（仍不用时间序排布）。"""
    from mino_nexus.services.nav_synthesis import _build_identify_block

    screens: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for turn in filtered:
        sid = fingerprint_turn(turn, y_tab=y_tab, exclude=set())
        page_name, fw, chrome = atlas_page_label(turn, y_tab=y_tab)
        wf = _turn_wireframe(turn, app_id=app_id)
        score = len(wf.get("regions") or []) * 1000 + int(turn.get("at") or 0)
        row = screens.get(sid)
        if not row:
            order.append(sid)
            screens[sid] = {
                "display_name": page_name,
                "visit_count": 0,
                "best_score": score,
                "wireframe": wf,
                "framework": fw,
                "chrome": chrome,
            }
        row = screens[sid]
        row["visit_count"] = int(row.get("visit_count") or 0) + 1
        if score >= int(row.get("best_score") or 0):
            row["best_score"] = score
            row["wireframe"] = wf
            row["display_name"] = page_name
            row["framework"] = fw
            row["chrome"] = chrome

    col_w, row_h = 280, 200
    layout_states = {
        sid: {"x": 48 + (i % 4) * col_w, "y": 48 + (i // 4) * row_h}
        for i, sid in enumerate(order)
    }
    wireframes = {sid: screens[sid]["wireframe"] for sid in order}
    edges: list[dict[str, Any]] = []
    seen_e: set[str] = set()
    prev_sid = ""
    prev_turn: dict[str, Any] | None = None
    for turn in filtered:
        sid = fingerprint_turn(turn, y_tab=y_tab, exclude=set())
        if prev_sid and sid and prev_sid != sid:
            if not _atlas_observed_nav_eligible(prev_turn or {}, prev_sid=prev_sid, next_sid=sid):
                prev_sid = sid
                prev_turn = turn
                continue
            eid = f"edge.atlas.nav.{prev_sid.split('.')[-1]}_to_{sid.split('.')[-1]}"
            if eid not in seen_e:
                seen_e.add(eid)
                action_label = _infer_transition_label(prev_turn or {}, turn)
                edges.append(
                    {
                        "id": eid,
                        "kind": "nav",
                        "from": prev_sid,
                        "to": sid,
                        "guard": {},
                        "execute": {"steps": ["tap_element"]},
                        "effect_assert": {"within_ms": 8000, "require_any": [], "require_none": []},
                        "on_fail": {},
                        "meta": {
                            "source": "screen_atlas",
                            "action_label": action_label,
                            "action_type": "tap",
                        },
                    }
                )
        prev_sid = sid
        prev_turn = turn
    states = [
        {
            "id": sid,
            "kind": "page",
            "entry": False,
            "identify": _build_identify_block(
                tab="",
                framework=dict(screens[sid].get("framework") or {}),
                chrome=list(screens[sid].get("chrome") or []),
                include_framework=True,
                is_entry=False,
            ),
            "guards": {},
            "meta": {
                "screen_atlas": True,
                "display_name": str(screens[sid].get("display_name") or sid),
                "visit_count": int(screens[sid].get("visit_count") or 0),
            },
        }
        for sid in order
    ]
    doc = {
        "app_id": app_id,
        "project_id": project_id,
        "version": "v1",
        "meta": {
            "screen_atlas": True,
            "atlas_layout": "orphan_grid",
            "atlas_built_at": int(time.time()),
            "capture_turns": len(filtered),
            "capture_sessions": int(cap_meta.get("sessions") or 0),
            "state_wireframes": wireframes,
            "studio_layout": {"states": layout_states},
        },
        "test_data": {},
        "states": states,
        "edges": edges,
    }
    _annotate_wireframes_from_edges(wireframes, edges)
    screen_list = [
        {
            "screen_id": sid,
            "display_name": str(screens[sid].get("display_name") or sid),
            "visit_count": int(screens[sid].get("visit_count") or 0),
            "wireframe": wireframes.get(sid),
        }
        for sid in order
    ]
    return {"doc": doc, "screen_list": screen_list, "states": states, "edges": edges}


def _draft_meta_by_state_id(app_id: str) -> dict[str, dict[str, Any]]:
    from mino_nexus.services import nav_calibration_store as calib
    from mino_nexus.services import nav_fsm_store as store

    out: dict[str, dict[str, Any]] = {}
    for reader in (store.read_raw, calib.read_draft):
        doc = reader(app_id)
        if not isinstance(doc, dict):
            continue
        for st in doc.get("states") or []:
            if not isinstance(st, dict):
                continue
            sid = str(st.get("id") or "").strip()
            if not sid:
                continue
            meta = dict(st.get("meta") or {})
            prev = out.get(sid) or {}
            prev.update({k: v for k, v in meta.items() if v not in (None, "", [])})
            out[sid] = prev
    return out


def _overlay_atlas_draft_meta(doc: dict[str, Any], app_id: str) -> dict[str, Any]:
    """草稿 NavFSM 上的展示名/别名/chrome 覆盖到 atlas state（不改变分桶）。"""
    by_id = _draft_meta_by_state_id(app_id)
    if not by_id:
        return doc
    out = dict(doc)
    states = []
    for st in out.get("states") or []:
        if not isinstance(st, dict):
            continue
        row = dict(st)
        sid = str(row.get("id") or "")
        dm = by_id.get(sid) or {}
        if not dm:
            states.append(row)
            continue
        meta = dict(row.get("meta") or {})
        dn = str(dm.get("display_name") or "").strip()
        if dn:
            meta["user_display_name"] = dn
            meta["display_name"] = dn
        aliases = dm.get("aliases")
        if isinstance(aliases, list) and aliases:
            meta["aliases"] = [str(a).strip() for a in aliases if str(a).strip()]
        if dm.get("page_role"):
            meta["page_role"] = str(dm.get("page_role") or "")
        if dm.get("tab"):
            meta["tab"] = str(dm.get("tab") or "")
        ct = dm.get("chrome_texts")
        if isinstance(ct, list) and ct:
            meta["chrome_texts"] = [str(x).strip() for x in ct if str(x).strip()][:8]
        ht = str(dm.get("header_title") or "").strip()
        if ht:
            meta["header_title"] = ht
        row["meta"] = meta
        states.append(row)
    out["states"] = states
    return out


def _atlas_state_remap_table(app_id: str) -> dict[str, str]:
    from mino_nexus.services import nav_calibration_store as calib
    from mino_nexus.services import nav_fsm_store as store

    remap: dict[str, str] = {}
    for reader in (store.read_raw, calib.read_draft):
        doc = reader(app_id)
        if not isinstance(doc, dict):
            continue
        meta = doc.get("meta") if isinstance(doc.get("meta"), dict) else {}
        rows = meta.get("atlas_state_remap")
        if isinstance(rows, dict):
            for src, dst in rows.items():
                s = str(src or "").strip()
                d = str(dst or "").strip()
                if s and d and s != d:
                    remap[s] = d
    return remap


def _resolve_remap_chain(sid: str, remap: dict[str, str]) -> str:
    cur = str(sid or "").strip()
    seen: set[str] = set()
    while cur in remap and remap[cur] != cur:
        if cur in seen:
            break
        seen.add(cur)
        cur = str(remap[cur] or "").strip()
    return cur


def _apply_atlas_state_remap(doc: dict[str, Any], app_id: str) -> dict[str, Any]:
    remap = _atlas_state_remap_table(app_id)
    if not remap:
        return doc
    out = dict(doc)
    states_in = list(out.get("states") or [])
    merged_states: dict[str, dict[str, Any]] = {}
    for st in states_in:
        if not isinstance(st, dict):
            continue
        cid = _resolve_remap_chain(str(st.get("id") or ""), remap)
        if not cid:
            continue
        row = dict(st)
        row["id"] = cid
        meta = dict(row.get("meta") or {})
        prev = merged_states.get(cid)
        if not prev:
            merged_states[cid] = row
            continue
        pm = dict(prev.get("meta") or {})
        vm = int(pm.get("visit_count") or 0) + int(meta.get("visit_count") or 0)
        pm["visit_count"] = vm
        if not str(pm.get("display_name") or "").strip() and meta.get("display_name"):
            pm["display_name"] = meta["display_name"]
        prev["meta"] = pm
    states = list(merged_states.values())
    edges = []
    seen_edge: set[tuple[str, str, str]] = set()
    for ed in out.get("edges") or []:
        if not isinstance(ed, dict):
            continue
        row = dict(ed)
        row["from"] = _resolve_remap_chain(str(row.get("from") or ""), remap)
        row["to"] = _resolve_remap_chain(str(row.get("to") or ""), remap)
        pair = (str(row.get("kind") or "nav"), row["from"], row["to"])
        if row["from"] == row["to"] or pair in seen_edge:
            continue
        seen_edge.add(pair)
        edges.append(row)
    meta = dict(out.get("meta") or {})
    wfs = dict(meta.get("state_wireframes") or {})
    new_wfs: dict[str, Any] = {}
    for sid, wf in wfs.items():
        cid = _resolve_remap_chain(str(sid), remap)
        if cid not in new_wfs:
            new_wfs[cid] = wf
    meta["state_wireframes"] = new_wfs
    out["states"] = states
    out["edges"] = edges
    out["meta"] = meta
    return out


def _capture_pin_key(session_id: str, turn_id: int) -> str:
    return f"{str(session_id or '').strip()}/{int(turn_id)}"


def _inject_pinned_capture_states(
    screen_rows: list[dict[str, Any]],
    cluster_meta: dict[str, dict[str, Any]],
    state_key_to_id: dict[Any, str],
    pin_map: dict[str, str],
    pin_meta: dict[str, dict[str, Any]],
    *,
    app_id: str,
    y_tab: int,
    tab_labels: list[str],
    exclude_tabs: set[str],
    home_tab: str,
) -> None:
    """把钉死/拆分的采集补成独立 architecture state（聚类键覆盖不到时）。"""
    from mino_nexus.services.nav_layout import detect_layout_framework, semantic_page_role

    known = {str(r.get("id") or "") for r in screen_rows}
    for pk, sid in pin_map.items():
        forced = str(sid or "").strip()
        if not forced or forced in known:
            continue
        parts = str(pk).rsplit("/", 1)
        if len(parts) != 2:
            continue
        sess, tid_s = parts[0], parts[1]
        try:
            tid = int(tid_s)
        except ValueError:
            continue
        turn = capture.read_turn(app_id, sess, tid)
        if not turn:
            continue
        row_meta = pin_meta.get(pk) or {}
        tab = str(row_meta.get("tab") or home_tab or (tab_labels[0] if tab_labels else "misc"))
        fw = detect_layout_framework(turn, y_tab_max=y_tab, exclude=exclude_tabs)
        role = semantic_page_role(fw)
        wf = _turn_wireframe(turn, app_id=app_id)
        dn = str(row_meta.get("display_name") or "").strip()
        if not dn:
            dn, _, _ = atlas_page_label(turn, y_tab=y_tab, tab=tab, page_role=role)
        sk = f"pin|{pk}"
        screen_rows.append(
            {
                "id": forced,
                "tab": tab,
                "framework": fw,
                "chrome": list(row_meta.get("chrome") or [])[:8],
                "is_entry": False,
                "page_role": role,
                "skeleton_fp": sk,
            }
        )
        state_key_to_id[sk] = forced
        cluster_meta[forced] = {
            "visit_count": 0,
            "wireframe": wf,
            "best_score": -1,
            "display_name": dn,
            "tab": tab,
            "page_role": role,
            "inferred_role": role,
            "chrome": [],
            "chrome_texts": [],
            "header_title": dn,
            "skeleton_fp": sk,
            "cluster_surface": "sub",
            "framework": fw,
            "pinned_capture": pk,
        }
        known.add(forced)


def _load_atlas_capture_pins(app_id: str) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    """session/turn → state_id；附带展示 meta（拆分页用）。"""
    from mino_nexus.services import nav_calibration_store as calib
    from mino_nexus.services import nav_fsm_store as store

    pin_map: dict[str, str] = {}
    pin_meta: dict[str, dict[str, Any]] = {}
    for reader in (store.read_raw, calib.read_draft):
        doc = reader(app_id)
        if not isinstance(doc, dict):
            continue
        meta = doc.get("meta") if isinstance(doc.get("meta"), dict) else {}
        rows = meta.get("atlas_capture_pins")
        if isinstance(rows, dict):
            for k, v in rows.items():
                sid = str(v or "").strip()
                if k and sid:
                    pin_map[str(k)] = sid
        extra = meta.get("atlas_capture_pin_meta")
        if isinstance(extra, dict):
            for k, row in extra.items():
                if isinstance(row, dict):
                    pin_meta[str(k)] = dict(row)
    return pin_map, pin_meta


def _patch_atlas_draft_meta(
    app_id: str,
    *,
    project_id: str = "",
    updated_by: str = "",
    patch: dict[str, Any],
) -> dict[str, Any]:
    from mino_nexus.services import nav_calibration_store as calib
    from mino_nexus.services import nav_fsm_store as store

    published = store.read_raw(app_id)
    if isinstance(published, dict):
        meta = dict(published.get("meta") or {})
        meta.update(patch)
        published["meta"] = meta
        return store.save(app_id, published, updated_by=updated_by, allow_calibrate=True)
    draft = calib.read_draft(app_id) or {
        "app_id": app_id,
        "project_id": project_id,
        "version": "draft",
        "meta": {},
        "states": [],
        "edges": [],
    }
    meta = dict(draft.get("meta") or {})
    meta.update(patch)
    draft["meta"] = meta
    if project_id and not draft.get("project_id"):
        draft["project_id"] = project_id
    return calib.save_draft(app_id, draft, updated_by=updated_by)


def save_atlas_capture_pin(
    app_id: str,
    *,
    session_id: str,
    turn_id: int,
    state_id: str,
    project_id: str = "",
    updated_by: str = "",
) -> dict[str, Any]:
    """将单条采集钉到指定 architecture state（刷新 atlas 后生效）。"""
    sid = str(state_id or "").strip()
    sess = str(session_id or "").strip()
    tid = int(turn_id or 0)
    if not sid or not sess or tid <= 0:
        return {"ok": False, "reason": "state_id / session_id / turn_id 必填"}
    pin_map, pin_meta = _load_atlas_capture_pins(app_id)
    key = _capture_pin_key(sess, tid)
    pin_map[key] = sid
    saved = _patch_atlas_draft_meta(
        app_id,
        project_id=project_id,
        updated_by=updated_by,
        patch={"atlas_capture_pins": pin_map, "atlas_capture_pin_meta": pin_meta},
    )
    return {"ok": True, "pin_key": key, "state_id": sid, "doc": saved}


def save_atlas_capture_split(
    app_id: str,
    *,
    session_id: str,
    turn_id: int,
    tab: str = "",
    display_name: str = "",
    project_id: str = "",
    updated_by: str = "",
) -> dict[str, Any]:
    """按采集帧强制拆成独立 state（骨骼 hint 含 capture id，避免再并入主面桶）。"""
    from mino_nexus.services.nav_synthesis import _slug_tab

    sess = str(session_id or "").strip()
    tid = int(turn_id or 0)
    if not sess or tid <= 0:
        return {"ok": False, "reason": "session_id / turn_id 必填"}
    turn = capture.read_turn(app_id, sess, tid)
    if not turn:
        return {"ok": False, "reason": "采集不存在"}
    from mino_nexus.services.nav_screen_layout import infer_content_bands
    from mino_nexus.services.nav_synthesis import assign_turn_tabs, extract_tab_bar_labels
    from mino_nexus.services.nav_target_scope import resolve_app_target_scope

    scope = resolve_app_target_scope(app_id)
    if is_system_screen(turn, scope=scope):
        return {"ok": False, "reason": "系统挡屏帧不能拆页"}

    bands = infer_content_bands(turn.get("nodes") or [])
    y_tab = int(bands.get("content_bottom_px") or 0) or 9999
    tab_use = str(tab or "").strip()
    if not tab_use:
        labels = extract_tab_bar_labels([turn], scope=scope, app_id=app_id)
        assigned = assign_turn_tabs([turn], labels, y_tab=y_tab, exclude=set(labels))
        tab_use = assigned[0] if assigned else (labels[0] if labels else "misc")
    key = _capture_pin_key(sess, tid)
    forced_sid = f"page.skcap{_short_hash(key, 8)}"
    pin_map, pin_meta = _load_atlas_capture_pins(app_id)
    pin_map[key] = forced_sid
    dn = str(display_name or "").strip()
    if not dn:
        dn, _, _ = atlas_page_label(turn, y_tab=y_tab)
    pin_meta[key] = {
        "session_id": sess,
        "turn_id": tid,
        "tab": tab_use,
        "display_name": dn,
        "forced_split": True,
        "state_id": forced_sid,
    }
    saved = _patch_atlas_draft_meta(
        app_id,
        project_id=project_id,
        updated_by=updated_by,
        patch={"atlas_capture_pins": pin_map, "atlas_capture_pin_meta": pin_meta},
    )
    return {"ok": True, "pin_key": key, "state_id": forced_sid, "doc": saved}


def save_atlas_state_merge(
    app_id: str,
    *,
    canonical_id: str,
    merge_ids: list[str],
    project_id: str = "",
    updated_by: str = "",
) -> dict[str, Any]:
    """Studio「合并到另一页」：写入 draft meta.atlas_state_remap。"""
    from mino_nexus.services import nav_calibration_store as calib
    from mino_nexus.services import nav_fsm_store as store

    canon = str(canonical_id or "").strip()
    sources = [str(x).strip() for x in (merge_ids or []) if str(x).strip() and str(x).strip() != canon]
    if not canon or not sources:
        return {"ok": False, "reason": "canonical_id 与 merge_ids 必填"}
    remap_patch = {src: canon for src in sources}
    published = store.read_raw(app_id)
    if isinstance(published, dict):
        meta = dict(published.get("meta") or {})
        table = dict(meta.get("atlas_state_remap") or {})
        table.update(remap_patch)
        meta["atlas_state_remap"] = table
        published["meta"] = meta
        saved = store.save(app_id, published, updated_by=updated_by, allow_calibrate=True)
        return {"ok": True, "target": "published", "remap": table, "doc": saved}
    draft = calib.read_draft(app_id) or {
        "app_id": app_id,
        "project_id": project_id,
        "version": "draft",
        "meta": {},
        "states": [],
        "edges": [],
    }
    meta = dict(draft.get("meta") or {})
    table = dict(meta.get("atlas_state_remap") or {})
    table.update(remap_patch)
    meta["atlas_state_remap"] = table
    draft["meta"] = meta
    if project_id and not draft.get("project_id"):
        draft["project_id"] = project_id
    saved = calib.save_draft(app_id, draft, updated_by=updated_by)
    return {"ok": True, "target": "draft", "remap": table, "doc": saved}


def _merge_atlas_manual_patches(doc: dict[str, Any], app_id: str) -> dict[str, Any]:
    """合并用户在 Studio 保存的手动跳转边（不被下次自动聚类覆盖）。"""
    from mino_nexus.services import nav_calibration_store as calib
    from mino_nexus.services import nav_fsm_store as store

    doc = _overlay_atlas_draft_meta(doc, app_id)
    doc = _apply_atlas_state_remap(doc, app_id)
    manual: list[dict[str, Any]] = []
    saved = store.read_raw(app_id)
    if isinstance(saved, dict):
        meta_saved = saved.get("meta") if isinstance(saved.get("meta"), dict) else {}
        rows = meta_saved.get("atlas_manual_edges")
        if isinstance(rows, list):
            manual.extend(r for r in rows if isinstance(r, dict))
    draft = calib.read_draft(app_id)
    if isinstance(draft, dict):
        meta_draft = draft.get("meta") if isinstance(draft.get("meta"), dict) else {}
        rows = meta_draft.get("atlas_manual_edges")
        if isinstance(rows, list):
            manual.extend(r for r in rows if isinstance(r, dict))
    if not manual:
        return doc
    out = dict(doc)
    edges = list(out.get("edges") or [])
    seen = {str(e.get("id") or "") for e in edges}
    for ed in manual:
        if not isinstance(ed, dict):
            continue
        eid = str(ed.get("id") or "")
        if eid and eid in seen:
            continue
        edges.append(ed)
        if eid:
            seen.add(eid)
    out["edges"] = edges
    wireframes = dict((out.get("meta") or {}).get("state_wireframes") or {})
    _annotate_wireframes_from_edges(wireframes, edges)
    out_meta = dict(out.get("meta") or {})
    out_meta["state_wireframes"] = wireframes
    out["meta"] = out_meta
    return out


def save_atlas_flow_groups(
    app_id: str,
    groups: list[dict[str, Any]],
    *,
    project_id: str = "",
    updated_by: str = "",
) -> dict[str, Any]:
    """架构页保存业务流展示分组（覆盖 build_atlas 块名/members，不写 states/edges）。"""
    from mino_nexus.services import nav_calibration_store as calib
    from mino_nexus.services import nav_fsm_store as store

    clean = [g for g in (groups or []) if isinstance(g, dict)]
    published = store.read_raw(app_id)
    if isinstance(published, dict):
        meta = dict(published.get("meta") or {})
        meta["atlas_flow_groups"] = clean
        published["meta"] = meta
        saved = store.save(app_id, published, updated_by=updated_by, allow_calibrate=True)
        return {"ok": True, "target": "published", "count": len(clean), "doc": saved}
    draft = calib.read_draft(app_id) or {
        "app_id": app_id,
        "project_id": project_id,
        "version": "draft",
        "meta": {},
        "states": [],
        "edges": [],
    }
    meta = dict(draft.get("meta") or {})
    meta["atlas_flow_groups"] = clean
    draft["meta"] = meta
    if project_id and not draft.get("project_id"):
        draft["project_id"] = project_id
    saved = calib.save_draft(app_id, draft)
    return {"ok": True, "target": "draft", "count": len(clean), "doc": saved}


def save_atlas_manual_edges(
    app_id: str,
    edges: list[dict[str, Any]],
    *,
    project_id: str = "",
    updated_by: str = "",
) -> dict[str, Any]:
    """只写 meta.atlas_manual_edges，不把 atlas 整图 PUT 成正式 NavFSM。"""
    from mino_nexus.services import nav_calibration_store as calib
    from mino_nexus.services import nav_fsm_store as store

    clean = [e for e in (edges or []) if isinstance(e, dict)]
    published = store.read_raw(app_id)
    if isinstance(published, dict):
        meta = dict(published.get("meta") or {})
        meta["atlas_manual_edges"] = clean
        published["meta"] = meta
        saved = store.save(app_id, published, updated_by=updated_by, allow_calibrate=True)
        return {"ok": True, "target": "published", "count": len(clean), "doc": saved}
    draft = calib.read_draft(app_id) or {
        "app_id": app_id,
        "project_id": project_id,
        "version": "draft",
        "meta": {},
        "states": [],
        "edges": [],
    }
    meta = dict(draft.get("meta") or {})
    meta["atlas_manual_edges"] = clean
    draft["meta"] = meta
    if project_id and not draft.get("project_id"):
        draft["project_id"] = project_id
    saved = calib.save_draft(app_id, draft)
    return {"ok": True, "target": "draft", "count": len(clean), "doc": saved}


def _empty_atlas_built(
    app_id: str,
    *,
    project_id: str,
    cap_meta: dict[str, Any],
) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "app_id": app_id,
        "project_id": project_id,
        "version": "v1",
        "meta": {
            "screen_atlas": True,
            "atlas_layout": "empty",
            "atlas_built_at": int(time.time()),
            "capture_turns": 0,
            "capture_sessions": int(cap_meta.get("sessions") or 0),
            "state_wireframes": {},
            "studio_layout": {"states": {}},
        },
        "test_data": {},
        "states": [],
        "edges": [],
    }
    return {"doc": doc, "screen_list": [], "states": [], "edges": []}


def _atlas_tab_entry_sid(doc: dict[str, Any], tab_label: str) -> str:
    want = str(tab_label or "").strip()
    if not want:
        return ""
    best_sid = ""
    best_vc = -1
    for st in doc.get("states") or []:
        if not isinstance(st, dict):
            continue
        sid = str(st.get("id") or "").strip()
        if not sid.startswith("page.sk"):
            continue
        meta = st.get("meta") if isinstance(st.get("meta"), dict) else {}
        dn = str(meta.get("display_name") or "").strip()
        vc = int(meta.get("visit_count") or 0)
        if dn == want or (want and want in dn):
            if vc > best_vc:
                best_vc = vc
                best_sid = sid
    if best_sid:
        return best_sid
    bar = (doc.get("meta") or {}).get("tab_bar") or {}
    labels = bar.get("labels") if isinstance(bar.get("labels"), dict) else {}
    for eid, lbl in labels.items():
        if str(lbl or "").strip() == want:
            return str(eid or "").strip()
    return ""


def _atlas_profile_sid_for_tab(states: list[dict[str, Any]], tab_label: str) -> str:
    want = str(tab_label or "").strip()
    best_sid = ""
    best_vc = -1
    for st in states:
        if not isinstance(st, dict):
            continue
        meta = st.get("meta") if isinstance(st.get("meta"), dict) else {}
        if str(meta.get("tab") or "").strip() != want:
            continue
        if str(meta.get("page_role") or "").strip() != "profile":
            continue
        sid = str(st.get("id") or "").strip()
        if not sid.startswith("page.sk"):
            continue
        vc = int(meta.get("visit_count") or 0)
        if vc > best_vc:
            best_vc = vc
            best_sid = sid
    return best_sid


def _resolve_legacy_fsm_atlas_state_id(
    sid: str,
    doc: dict[str, Any],
    states: list[dict[str, Any]],
) -> str:
    """旧 NavFSM Tab 占位页（page.tab_X / .profile）对齐到当前聚类 state。"""
    raw = str(sid or "").strip()
    if not raw or raw.startswith("page.sk") or not raw.startswith("page.tab_"):
        return raw

    rest = raw[len("page.tab_") :]
    is_profile = rest.endswith(".profile")
    slug = rest[: -len(".profile")] if is_profile else rest
    tab_label = ""
    bar = (doc.get("meta") or {}).get("tab_bar") or {}
    for lbl in bar.get("labels", {}).values():
        val = str(lbl or "").strip()
        if not val:
            continue
        lbl_slug = re.sub(r"\s+", "_", val)[:16]
        lbl_slug = re.sub(r"[^\w\u4e00-\u9fff]", "", lbl_slug) or "tab"
        if lbl_slug == slug:
            tab_label = val
            break
    if not tab_label and slug:
        tab_label = slug
    if not tab_label:
        return raw
    if is_profile:
        hit = _atlas_profile_sid_for_tab(states, tab_label)
        if hit:
            return hit
    entry = _atlas_tab_entry_sid(doc, tab_label)
    if entry:
        return entry
    best_sid = ""
    best_vc = -1
    for st in states:
        if not isinstance(st, dict):
            continue
        cid = str(st.get("id") or "").strip()
        if not cid.startswith("page.sk"):
            continue
        meta = st.get("meta") if isinstance(st.get("meta"), dict) else {}
        role = str(meta.get("page_role") or meta.get("inferred_role") or "").strip()
        if role == "detail":
            continue
        vc = int(meta.get("visit_count") or 0)
        if vc > best_vc:
            best_vc = vc
            best_sid = cid
    return best_sid or raw


def _fsm_skeleton_remap_to_atlas_state(
    sid: str,
    wf: dict[str, Any],
    *,
    states: list[dict[str, Any]],
    wireframes: dict[str, Any],
) -> str:
    """NavFSM 遗留 page.sk* 对齐到当前 Atlas 桶，避免 augment 重复灌节点。"""
    raw = str(sid or "").strip()
    if not raw.startswith("page.sk") or not isinstance(wf, dict):
        return ""
    lane = _atlas_state_page_lane(raw)
    if not lane:
        return ""
    from mino_nexus.services.nav_app_skeleton import (
        wireframe_aligned_transform_jaccard,
        wireframes_same_page_under_viewport_transform,
    )

    best_sid = ""
    best_key = (-1, -1)
    for st in states:
        if not isinstance(st, dict):
            continue
        cid = str(st.get("id") or "").strip()
        if not cid or cid == raw:
            continue
        if _atlas_state_page_lane(cid) != lane:
            continue
        existing = wireframes.get(cid)
        if not isinstance(existing, dict):
            continue
        if not (existing.get("regions") or wf.get("regions")):
            continue
        same = wireframes_same_page_under_viewport_transform(existing, wf)
        aligned = 1.0 if same else wireframe_aligned_transform_jaccard(existing, wf)
        if not same and aligned < 0.72:
            continue
        meta = st.get("meta") if isinstance(st.get("meta"), dict) else {}
        vc = int(meta.get("visit_count") or 0)
        key = (1 if same else 0, vc)
        if key > best_key:
            best_key = key
            best_sid = cid
    return best_sid


def _fsm_lane_canonical_state(
    sid: str,
    states: list[dict[str, Any]],
) -> str:
    """同 page.sk 车道内 visit 最高的 Atlas 聚类态（FSM 孤儿对齐用）。"""
    lane = _atlas_state_page_lane(str(sid or "").strip())
    best_sid = ""
    best_vc = -1
    for st in states:
        if not isinstance(st, dict):
            continue
        cid = str(st.get("id") or "").strip()
        if not cid or _atlas_state_page_lane(cid) != lane:
            continue
        meta = st.get("meta") if isinstance(st.get("meta"), dict) else {}
        if meta.get("from_nav_fsm"):
            continue
        vc = int(meta.get("visit_count") or 0)
        if vc > best_vc:
            best_vc = vc
            best_sid = cid
    return best_sid


def _append_atlas_turn_ref(
    meta: dict[str, Any],
    *,
    state_id: str,
    turn: dict[str, Any],
    tab: str = "",
) -> None:
    sid = str(state_id or "").strip()
    sess = str(turn.get("session_id") or "").strip()
    tid = int(turn.get("turn_id") or 0)
    if not sid or not sess:
        return
    refs = list(meta.get("atlas_turn_refs") or [])
    seen = {(r.get("state_id"), r.get("session_id"), r.get("turn_id")) for r in refs}
    if (sid, sess, tid) in seen:
        return
    refs.append(
        {
            "state_id": sid,
            "session_id": sess,
            "turn_id": tid,
            "at": int(turn.get("at") or 0),
            "tab": str(tab or "").strip(),
        }
    )
    meta["atlas_turn_refs"] = refs


def _augment_atlas_from_fsm_localized_captures(
    doc: dict[str, Any],
    app_id: str,
    filtered: list[dict[str, Any]],
) -> dict[str, Any]:
    """把跑批 localize 命中的屏补进 Atlas。骨骼模式下只追加 turn_ref / 线框去重，不再灌入 page.tab_* 旧节点。"""
    from mino_nexus.services import nav_fsm_store as store
    from mino_nexus.services.nav_compiler import state_label

    fsm, _ = store.load_with_reason(app_id)
    if not fsm or not filtered:
        return doc
    skeleton_mode = _atlas_uses_skeleton_states(doc)
    out = dict(doc)
    states = list(out.get("states") or [])
    edges = list(out.get("edges") or [])
    meta = dict(out.get("meta") or {})
    wireframes = dict(meta.get("state_wireframes") or {})
    known = {str(s.get("id") or "") for s in states}
    fsm_by_id = {str(s.get("id") or ""): s for s in (fsm.get("states") or []) if s}
    fsm_remap: dict[str, str] = {}

    best: dict[str, tuple[int, dict[str, Any]]] = {}
    for turn in filtered:
        loc = turn.get("localized") if isinstance(turn.get("localized"), dict) else {}
        sid = str(loc.get("chosen") or turn.get("state_id") or "").strip()
        if not sid or sid not in fsm_by_id:
            continue
        if _fsm_state_is_atlas_noise(fsm_by_id[sid]):
            continue
        score = len(turn.get("nodes") or []) * 1000 + int(turn.get("at") or 0)
        prev = best.get(sid)
        if not prev or score > prev[0]:
            best[sid] = (score, turn)

    for sid in list(best.keys()):
        legacy_target = _resolve_legacy_fsm_atlas_state_id(sid, out, states)
        if legacy_target and legacy_target != sid:
            fsm_remap[sid] = legacy_target

    for sid, (_, turn) in best.items():
        if sid in fsm_remap:
            _append_atlas_turn_ref(
                meta,
                state_id=fsm_remap[sid],
                turn=turn,
            )
            continue
        if sid in known:
            _append_atlas_turn_ref(meta, state_id=sid, turn=turn)
            continue
        if skeleton_mode and (
            sid.startswith("page.tab_") or _fsm_state_is_atlas_noise(fsm_by_id.get(sid) or {})
        ):
            leg = _resolve_legacy_fsm_atlas_state_id(sid, out, states)
            if leg:
                _append_atlas_turn_ref(meta, state_id=leg, turn=turn)
            continue
        st = dict(fsm_by_id[sid])
        wf = _turn_wireframe(turn, app_id=app_id)
        if skeleton_mode and sid.startswith("page.sk"):
            atlas_hit = _fsm_skeleton_remap_to_atlas_state(
                sid,
                wf,
                states=states,
                wireframes=wireframes,
            )
            if atlas_hit:
                fsm_remap[sid] = atlas_hit
                _append_atlas_turn_ref(meta, state_id=atlas_hit, turn=turn)
                continue
        sig = _wireframe_layout_signature(wf)
        if sig != "empty":
            for existing_sid, existing_wf in wireframes.items():
                if _wireframe_layout_signature(existing_wf) == sig:
                    fsm_remap[sid] = str(existing_sid)
                    break
        if sid in fsm_remap:
            _append_atlas_turn_ref(
                meta,
                state_id=fsm_remap[sid],
                turn=turn,
            )
            continue
        if skeleton_mode and sid.startswith("page.sk"):
            lane_hit = _fsm_lane_canonical_state(sid, states)
            if lane_hit:
                fsm_remap[sid] = lane_hit
                _append_atlas_turn_ref(meta, state_id=lane_hit, turn=turn)
                continue
            continue
        wireframes[sid] = wf
        states.append(
            {
                "id": sid,
                "kind": st.get("kind") or "page",
                "entry": bool(st.get("entry")),
                "identify": st.get("identify") or {},
                "guards": st.get("guards") or {},
                "meta": {
                    "screen_atlas": True,
                    "display_name": state_label(fsm, sid),
                    "visit_count": 1,
                    "page_role": "capture",
                    "tab": "",
                    "from_nav_fsm": True,
                },
            }
        )
        known.add(sid)
        _append_atlas_turn_ref(meta, state_id=sid, turn=turn)

    for turn in filtered:
        loc = turn.get("localized") if isinstance(turn.get("localized"), dict) else {}
        sid = str(loc.get("chosen") or turn.get("state_id") or "").strip()
        if not sid or sid not in fsm_by_id or _fsm_state_is_atlas_noise(fsm_by_id[sid]):
            continue
        target = _resolve_remapped_state_id(
            _resolve_legacy_fsm_atlas_state_id(sid, out, states),
            fsm_remap,
        )
        if target in known:
            _append_atlas_turn_ref(meta, state_id=target, turn=turn)

    seen_eids = {str(e.get("id") or "") for e in edges}
    seen_pairs = {
        (
            str(e.get("kind") or "nav"),
            str(e.get("from") or ""),
            str(e.get("to") or ""),
        )
        for e in edges
    }
    for ed in fsm.get("edges") or []:
        if str(ed.get("kind") or "nav") != "nav":
            continue
        f = _resolve_remapped_state_id(str(ed.get("from") or ""), fsm_remap)
        t = _resolve_remapped_state_id(str(ed.get("to") or ""), fsm_remap)
        if _fsm_state_is_atlas_noise(fsm_by_id.get(f, {})) or _fsm_state_is_atlas_noise(
            fsm_by_id.get(t, {})
        ):
            continue
        if not f or not t or f == t:
            continue
        if skeleton_mode and (f.startswith("page.tab_") or t.startswith("page.tab_")):
            continue
        if f not in known or t not in known:
            continue
        eid = str(ed.get("id") or f"edge.{f}_to_{t}")
        if eid in seen_eids:
            continue
        pair = ("nav", f, t)
        if pair in seen_pairs:
            continue
        seen_eids.add(eid)
        seen_pairs.add(pair)
        row = dict(ed)
        row["from"] = f
        row["to"] = t
        edges.append(row)

    meta["state_wireframes"] = wireframes
    if fsm_remap:
        meta["atlas_fsm_remap"] = dict(fsm_remap)
    out["states"] = states
    out["edges"] = edges
    out["meta"] = meta
    return out


def _atlas_uses_skeleton_states(doc: dict[str, Any]) -> bool:
    """架构图已按骨骼聚类（page.sk*），不再以 page.tab_* 为屏态主键。"""
    if not isinstance(doc, dict):
        return False
    meta = doc.get("meta") if isinstance(doc.get("meta"), dict) else {}
    if meta.get("screen_atlas") and str(meta.get("atlas_layout") or "") == "skeleton_grid":
        return True
    for st in doc.get("states") or []:
        sid = str((st or {}).get("id") or "").strip()
        if sid.startswith("page.sk"):
            return True
    return False


def _hydrate_atlas_edge_execute(edge: dict[str, Any]) -> dict[str, Any]:
    """把 Atlas 边上的 action_label 写入 execute，供 fsm_navigate / tap 编译。"""
    row = dict(edge or {})
    if str(row.get("kind") or "nav") != "nav":
        return row
    exe = dict(row.get("execute") or {})
    from mino_nexus.services.nav_execute import execute_target_page

    meta = dict(row.get("meta") or {})
    action_type = str(meta.get("action_type") or "").strip()
    if action_type == "back" or meta.get("reverse"):
        exe.setdefault("steps", ["press_key"])
        exe.setdefault("key", "BACK")
        row["execute"] = exe
        return row
    if execute_target_page(exe) or exe.get("selector_text"):
        if exe.get("target_tab") and not exe.get("target_page"):
            exe["target_page"] = str(exe.get("target_tab") or "").strip()
        row["execute"] = exe
        return row
    label = str(meta.get("action_label") or "").strip()
    if action_type == "tab" and label:
        tab = label.split("·", 1)[-1].strip() if "·" in label else label
        if tab:
            exe["target_page"] = tab
    elif action_type == "tap" and label:
        sel = label.split("·", 1)[-1].strip() if "·" in label else label
        if sel and sel not in ("进入", "点击"):
            exe["selector_text"] = sel
            exe["text"] = sel
    row["execute"] = exe
    return row


def prepare_atlas_doc_for_nav_runtime(doc: dict[str, Any]) -> dict[str, Any]:
    """Screen Atlas 文档 → 跑批/路线图可用的 NavFSM 形状（仍含 display_name / 骨骼线框）。"""
    out = dict(doc or {})
    states: list[dict[str, Any]] = []
    for st in out.get("states") or []:
        if not isinstance(st, dict):
            continue
        row = dict(st)
        meta = dict(row.get("meta") or {})
        samples = meta.get("name_samples") if isinstance(meta.get("name_samples"), list) else []
        dn = str(meta.get("display_name") or "").strip()
        aliases = [str(a).strip() for a in (meta.get("aliases") or []) if str(a).strip()]
        for s in samples:
            val = str(s or "").strip()
            if val and val != dn and val not in aliases:
                aliases.append(val)
        if aliases:
            meta["aliases"] = aliases[:12]
        meta.setdefault("skeleton_fp", str(meta.get("skeleton_fp") or ""))
        row["meta"] = meta
        states.append(row)
    out["states"] = states
    out["edges"] = [_hydrate_atlas_edge_execute(ed) for ed in (out.get("edges") or [])]
    nav_meta = dict(out.get("meta") or {})
    nav_meta.setdefault("nav_source", "screen_atlas")
    out["meta"] = nav_meta
    return out


def atlas_doc_for_navigation(
    app_id: str,
    *,
    project_id: str = "",
    session_id: str = "",
) -> dict[str, Any] | None:
    """从采集构建骨骼架构图，作为导航/ localize / fsm_navigate 的真源。"""
    built = build_atlas(app_id, project_id=project_id, session_id=session_id)
    doc = built.get("doc") if isinstance(built, dict) else None
    if not doc or not (doc.get("states") or []):
        return None
    if not _atlas_uses_skeleton_states(doc):
        return None
    doc = prepare_atlas_doc_for_nav_runtime(doc)
    from mino_nexus.services.nav_edge_resolve import enrich_state_aliases_from_nav_edges

    doc, _ = enrich_state_aliases_from_nav_edges(doc)
    return doc


def _atlas_studio_snapshot_usable(doc: dict[str, Any] | None) -> bool:
    if not isinstance(doc, dict):
        return False
    if not (doc.get("states") or []):
        return False
    meta = doc.get("meta") if isinstance(doc.get("meta"), dict) else {}
    if not meta.get("screen_atlas"):
        return False
    layout = meta.get("studio_layout")
    return isinstance(layout, dict) and bool(layout.get("states"))


def load_atlas_studio_snapshot_doc(app_id: str) -> dict[str, Any] | None:
    """架构页快路径：读 nav_fsm.meta 里上次刷新落盘的完整 Atlas doc。"""
    from mino_nexus.services import nav_fsm_store as store

    raw = store.read_raw(app_id)
    if not isinstance(raw, dict):
        return None
    meta = raw.get("meta") if isinstance(raw.get("meta"), dict) else {}
    snap = meta.get("atlas_studio_snapshot")
    if not isinstance(snap, dict):
        return None
    doc = snap.get("doc")
    if not _atlas_studio_snapshot_usable(doc):
        return None
    return dict(doc)


def persist_atlas_studio_snapshot(
    app_id: str,
    built: dict[str, Any],
    *,
    updated_by: str = "atlas_rebuild",
) -> None:
    """刷新/首次 build 后写入快照（含边 meta / 线框 / layout，避免从 ORM 边表丢字段）。"""
    from mino_nexus.services import nav_fsm_store as store

    doc = built.get("doc") if isinstance(built.get("doc"), dict) else None
    if not _atlas_studio_snapshot_usable(doc):
        return
    raw = store.read_raw(app_id)
    if raw is None:
        raw = {
            "app_id": app_id,
            "project_id": str(built.get("project_id") or ""),
            "version": store.DEFAULT_VERSION,
            "meta": {},
            "test_data": {},
            "states": [],
            "edges": [],
        }
    meta = dict(raw.get("meta") or {})
    meta["atlas_studio_snapshot"] = {
        "saved_at": int(time.time()),
        "atlas_content_hash": str((doc.get("meta") or {}).get("atlas_content_hash") or ""),
        "doc": doc,
    }
    raw["meta"] = meta
    store.save(app_id, raw, updated_by=updated_by, allow_calibrate=True)


def _normalize_atlas_target(value: str, platform: str = "") -> str:
    from mino_nexus.services.nav_target_scope import _looks_like_url, _norm_platform

    v = str(value or "").strip()
    if not v:
        return ""
    plat = _norm_platform(platform)
    if plat == "web" or _looks_like_url(v):
        try:
            from urllib.parse import urlparse

            parsed = urlparse(v if "://" in v else f"https://{v}")
            path = (parsed.path or "/").rstrip("/") or ""
            return f"{parsed.hostname or ''}{path}".lower()
        except Exception:  # noqa: BLE001
            return v.rstrip("/").lower()
    return v


def _channel_target_for_env(
    env_doc: dict[str, Any],
    *,
    env_profile: str,
    channel_id: str,
) -> str:
    from mino_nexus.services.project_env import _profile_value, profile_snapshot

    channels = [c for c in (env_doc.get("channels") or []) if isinstance(c, dict)]
    ch = next((c for c in channels if str(c.get("id") or "") == channel_id), None)
    if not ch:
        return ""
    snap = profile_snapshot(env_doc, env_profile)
    block = snap.get(channel_id) if isinstance(snap, dict) else {}
    return str(_profile_value(block, str(ch.get("field") or "value")) or "").strip()


def filter_atlas_doc_by_env_surface(
    doc: dict[str, Any],
    *,
    env_surface: str,
    project_id: str = "",
    env_profile: str = "",
) -> dict[str, Any]:
    """按跑批 channel.id（env_surface）裁剪屏面图谱；优先匹配采集落盘的 env_surface。"""
    want = str(env_surface or "").strip()
    if not want or not isinstance(doc, dict):
        return doc
    refs = list((doc.get("meta") or {}).get("atlas_turn_refs") or [])
    if not refs:
        return doc

    env_doc: dict[str, Any] = {}
    pid = str(project_id or doc.get("project_id") or "").strip()
    prof = str(env_profile or "").strip() or "test"
    if pid:
        try:
            from mino_nexus.services import project_store as ps

            env_doc = ps.project_env(pid) or {}
            prof = str(env_profile or env_doc.get("default_profile") or "test").strip()
        except Exception:  # noqa: BLE001
            env_doc = {}
    configured = _channel_target_for_env(env_doc, env_profile=prof, channel_id=want) if env_doc else ""
    cfg_norm = _normalize_atlas_target(configured, "web")

    def _ref_matches(ref: dict[str, Any]) -> bool:
        if not isinstance(ref, dict):
            return False
        surf = str(ref.get("env_surface") or "").strip()
        if surf:
            return surf == want
        tgt = _normalize_atlas_target(
            str(ref.get("target_package") or ""),
            str(ref.get("platform") or ""),
        )
        if configured and tgt and cfg_norm:
            return tgt == cfg_norm or tgt.endswith(cfg_norm) or cfg_norm.endswith(tgt)
        if configured and tgt:
            return configured.rstrip("/") == str(ref.get("target_package") or "").rstrip("/")
        return False

    has_surface_meta = any(str(r.get("env_surface") or "").strip() for r in refs if isinstance(r, dict))
    has_target_meta = any(str(r.get("target_package") or "").strip() for r in refs if isinstance(r, dict))
    if not has_surface_meta and not has_target_meta and not configured:
        return doc

    matching = [r for r in refs if isinstance(r, dict) and _ref_matches(r)]
    state_ids = {str(r.get("state_id") or "").strip() for r in matching}
    state_ids.discard("")
    if not state_ids:
        return {
            **doc,
            "states": [],
            "edges": [],
            "meta": {
                **(doc.get("meta") or {}),
                "atlas_turn_refs": [],
                "arch_channel_filter": want,
            },
        }

    def _edge_endpoints(edge: dict[str, Any]) -> tuple[str, str]:
        return (
            str(edge.get("from") or edge.get("from_state") or "").strip(),
            str(edge.get("to") or edge.get("to_state") or "").strip(),
        )

    states = [s for s in (doc.get("states") or []) if str(s.get("id") or "").strip() in state_ids]
    edges = [
        e
        for e in (doc.get("edges") or [])
        if all(x in state_ids for x in _edge_endpoints(e))
    ]
    return {
        **doc,
        "states": states,
        "edges": edges,
        "meta": {
            **(doc.get("meta") or {}),
            "atlas_turn_refs": matching,
            "arch_channel_filter": want,
        },
    }


def fetch_screen_atlas(
    app_id: str,
    *,
    project_id: str = "",
    session_id: str = "",
    env_surface: str = "",
    env_profile: str = "",
    rebuild: bool = False,
    updated_by: str = "",
) -> dict[str, Any]:
    """Studio 架构图：默认读库内快照；rebuild=True 时全量 build_atlas 并更新快照。"""
    want_surface = str(env_surface or "").strip()

    def _apply_surface_filter(row: dict[str, Any]) -> dict[str, Any]:
        if not want_surface or not row.get("doc"):
            return row
        doc = filter_atlas_doc_by_env_surface(
            row["doc"],
            env_surface=want_surface,
            project_id=project_id or str(row.get("project_id") or ""),
            env_profile=env_profile,
        )
        return {
            **row,
            "doc": doc,
            "screen_count": len(doc.get("states") or []),
            "edge_count": len(doc.get("edges") or []),
        }

    if not rebuild:
        cached = load_atlas_studio_snapshot_doc(app_id)
        if cached is not None:
            doc = _merge_atlas_manual_patches(cached, app_id)
            snap_meta = {}
            from mino_nexus.services import nav_fsm_store as store

            raw = store.read_raw(app_id)
            if isinstance(raw, dict):
                sm = (raw.get("meta") or {}).get("atlas_studio_snapshot") or {}
                if isinstance(sm, dict):
                    snap_meta = sm
            doc_meta = dict(doc.get("meta") or {})
            return _apply_surface_filter(
                {
                    "app_id": app_id,
                    "project_id": project_id or str(doc.get("project_id") or ""),
                    "source": "screen_atlas_cached",
                    "doc": doc,
                    "screens": [],
                    "screen_count": len(doc.get("states") or []),
                    "edge_count": len(doc.get("edges") or []),
                    "capture": {
                        "turns_app": int(doc_meta.get("capture_turns") or 0),
                        "sessions": int(doc_meta.get("capture_sessions") or 0),
                    },
                    "updated_at": int(snap_meta.get("saved_at") or doc_meta.get("atlas_built_at") or 0),
                    "cached": True,
                }
            )

    built = build_atlas(app_id, project_id=project_id, session_id=session_id)
    persist_atlas_studio_snapshot(
        app_id,
        built,
        updated_by=updated_by or "atlas_rebuild",
    )
    out = dict(built)
    out["source"] = "screen_atlas"
    out["cached"] = False
    return _apply_surface_filter(out)


def build_atlas(
    app_id: str,
    *,
    project_id: str = "",
    session_id: str = "",
) -> dict[str, Any]:
    """从采集构建 Screen Atlas：骨骼聚类 + 观测跳转边（不按 Tab 分桶命名 state_id）。"""
    ordered, cap_meta = capture.iter_cumulative_turns(app_id, limit_sessions=40, limit_turns=2000)
    if session_id:
        ordered = [t for t in ordered if str(t.get("session_id") or "") == str(session_id)]
    from mino_nexus.services.nav_target_scope import resolve_app_target_scope

    scope = resolve_app_target_scope(app_id)
    filtered = [t for t in ordered if not is_system_screen(t, scope=scope)]

    if not filtered:
        built = _empty_atlas_built(app_id, project_id=project_id, cap_meta=cap_meta)
        doc = _merge_atlas_manual_patches(built["doc"], app_id)
        explore_turns = 0
        vlm_turns = 0
        doc_meta = dict(doc.get("meta") or {})
        doc_meta["capture_layout_sources"] = {
            "turns": 0,
            "layout_vision_turns": 0,
            "note": "线框 regions 来自 hierarchy；layout_vision 有数据时与 hierarchy 叠在 wireframe.sources 里",
        }
        doc = {**doc, "meta": doc_meta}
        return {
            "app_id": app_id,
            "project_id": project_id,
            "source": "screen_atlas",
            "doc": doc,
            "screens": [],
            "screen_count": 0,
            "edge_count": len(doc.get("edges") or []),
            "capture": {**cap_meta, "turns_app": 0, "explore_turns": 0},
            "updated_at": int(cap_meta.get("latest_at") or 0) or max((int(t.get("at") or 0) for t in ordered), default=0),
        }

    from mino_nexus.services.nav_screen_layout import infer_content_bands

    ys: list[int] = []
    for turn in filtered:
        bands = infer_content_bands(turn.get("nodes") or [])
        y = int(bands.get("content_bottom_px") or 0)
        if y > 0:
            ys.append(y)
    ys.sort()
    y_tab = ys[len(ys) // 2] if ys else 0

    built = _atlas_cluster_doc(
        app_id,
        filtered,
        project_id=project_id,
        y_tab=y_tab,
        cap_meta=cap_meta,
    )
    if not built:
        built = _atlas_fallback_doc(
            app_id,
            filtered,
            project_id=project_id,
            y_tab=y_tab,
            cap_meta=cap_meta,
        )

    doc = _merge_atlas_manual_patches(built["doc"], app_id)
    doc = _augment_atlas_from_fsm_localized_captures(doc, app_id, filtered)
    enrich_ctx = built.get("_atlas_enrich") if isinstance(built, dict) else None
    if isinstance(enrich_ctx, dict) and enrich_ctx.get("timeline") is not None:
        from mino_nexus.services.nav_flow_blocks import enrich_atlas_doc

        enrich_atlas_doc(
            doc,
            timeline=list(enrich_ctx.get("timeline") or []),
            turn_cluster_ids=list(enrich_ctx.get("turn_cluster_ids") or []),
            nav_action_types=dict(enrich_ctx.get("nav_action_types") or {}),
            cluster_meta=dict(enrich_ctx.get("cluster_meta") or {}),
            app_id=app_id,
        )
    explore_turns = sum(1 for t in filtered if str(t.get("run_type") or "").lower() == "explore")
    vlm_turns = sum(
        1
        for t in filtered
        if isinstance(t.get("layout_vision"), dict)
        and (t["layout_vision"].get("regions") or t["layout_vision"].get("elements"))
    )
    doc_meta = dict(doc.get("meta") or {})
    doc_meta["capture_layout_sources"] = {
        "turns": len(filtered),
        "layout_vision_turns": int(vlm_turns),
        "note": "线框 regions 来自 hierarchy；layout_vision 有数据时与 hierarchy 叠在 wireframe.sources 里",
    }
    from mino_nexus.services.nav_version_views import collect_nav_views_from_turns

    doc_meta["nav_views"] = collect_nav_views_from_turns(filtered)
    doc = {**doc, "meta": doc_meta}

    last_capture_at = max((int(t.get("at") or 0) for t in filtered), default=0)
    return {
        "app_id": app_id,
        "project_id": project_id,
        "source": "screen_atlas",
        "doc": doc,
        "screens": built["screen_list"],
        "screen_count": len(doc.get("states") or []),
        "edge_count": len(doc.get("edges") or []),
        "capture": {
            **cap_meta,
            "turns_app": len(filtered),
            "explore_turns": explore_turns,
        },
        "updated_at": last_capture_at,
    }


def explore_progress_block(
    *,
    known_screen_ids: list[str],
    step: int,
    max_steps: int,
    idle_steps: int,
    max_idle_steps: int,
    known_screen_labels: list[str] | None = None,
) -> str:
    labels = [str(x).strip() for x in (known_screen_labels or []) if str(x).strip()]
    shown = labels[-8:] if labels else [str(x) for x in (known_screen_ids or [])[-8:]]
    lines = [
        f"【探索进度】步数 {step}/{max_steps}；连续无新屏 {idle_steps}/{max_idle_steps}",
        f"已发现 {len(known_screen_ids)} 个不同屏面：{', '.join(shown) or '（尚无）'}",
        "尽量切换底栏、进入列表项、返回后再去未去过区域；无法发现新屏时可 signal_done 结束探索。",
    ]
    return "\n".join(lines)


def explore_screen_identity(nodes: list[dict[str, Any]]) -> tuple[str, str]:
    """与架构图同一套骨骼聚类键 + 可读名，供 ExploreCursor idle / 进度块使用。"""
    from mino_nexus.services.nav_layout import (
        detect_layout_framework,
        semantic_page_role,
        stable_chrome_texts,
    )
    from mino_nexus.services.nav_app_skeleton import chrome_header_title
    from mino_nexus.services.nav_screen_layout import infer_content_bands

    turn = {"nodes": nodes or []}
    bands = infer_content_bands(nodes or [])
    y_tab = int(bands.get("content_bottom_px") or 0) or 9999
    fw = detect_layout_framework(turn, y_tab_max=y_tab, exclude=set())
    chrome = stable_chrome_texts(turn, y_tab_max=y_tab, exclude=set())
    role = semantic_page_role(fw)
    row = {"turn": turn, "framework": fw, "chrome": chrome}
    key = _atlas_layout_cluster_key(row, None, str(role or ""))
    header = chrome_header_title(chrome)
    title = _compact_page_title(header) if header else _logical_page_title("", str(role or ""), fw)
    return key, title
