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
) -> str:
    """Atlas 聚类键：同 Tab+角色下按 layout_fp（banner / grid 等）分逻辑页，不按每帧线框拆页。"""
    from mino_nexus.services.nav_layout import layout_fp_from_match

    fp = str(row.get("fp") or "")
    fw = dict(row.get("framework") or {})
    layout_lane = layout_fp_from_match(fw)
    sig = _wireframe_layout_signature(wf)
    if role in ("feed", "profile", "main"):
        lane = layout_lane or (sig if sig != "empty" else "")
        return lane or fp or f"role:{role}"
    ck = row.get("chrome_key")
    if role == "detail" and ck:
        parts = [str(c) for c in (ck if isinstance(ck, (list, tuple)) else (ck,)) if c]
        if parts:
            return f"{fp}|{_short_hash(','.join(parts), 6)}"
    sig = _wireframe_layout_signature(wf)
    return sig if sig != "empty" else (fp or role)


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


def _merge_wireframe_duplicate_clusters(
    screen_rows: list[dict[str, Any]],
    cluster_meta: dict[str, dict[str, Any]],
    turn_cluster_ids: list[str],
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """同 Tab 内线框布局一致 → 保留访问最多的一屏，其余合并进 canonical。"""
    groups: dict[tuple[str, str], list[str]] = {}
    for row in screen_rows:
        if row.get("is_entry"):
            continue
        sid = str(row.get("id") or "")
        tab = str(row.get("tab") or "")
        wf = cluster_meta.get(sid, {}).get("wireframe") or {}
        sig = _wireframe_layout_signature(wf if isinstance(wf, dict) else {})
        if sig == "empty":
            continue
        groups.setdefault((tab, sig), []).append(sid)

    remap: dict[str, str] = {}
    for (_tab, _sig), sids in groups.items():
        if len(sids) < 2:
            continue
        canonical = max(sids, key=lambda s: int((cluster_meta.get(s) or {}).get("visit_count") or 0))
        can_meta = cluster_meta.get(canonical) or {}
        names: list[str] = []
        for s in sids:
            if s == canonical:
                continue
            remap[s] = canonical
            dup = cluster_meta.get(s) or {}
            can_meta["visit_count"] = int(can_meta.get("visit_count") or 0) + int(dup.get("visit_count") or 0)
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

    if not remap:
        return screen_rows, remap

    keep_ids = {str(r.get("id") or "") for r in screen_rows} - set(remap.keys())
    screen_rows = [r for r in screen_rows if str(r.get("id") or "") in keep_ids]
    for i, cid in enumerate(turn_cluster_ids):
        if cid in remap:
            turn_cluster_ids[i] = remap[cid]
    return screen_rows, remap


def _slug_atlas_page(
    tab: str,
    role: str,
    layout_key: str,
    used: set[str],
) -> str:
    """子页 id：Tab + 结构键（不用顶栏流式/通知文案当 slug）。"""
    from mino_nexus.services.nav_synthesis import _slug_semantic_sub

    tab_slug = re.sub(r"[^\w\u4e00-\u9fff]", "", tab.strip())[:12] or "tab"
    lk = re.sub(r"[^\w]", "_", str(layout_key or role or "page"))[:20]
    if role in ("feed", "main") and not layout_key:
        return _slug_semantic_sub(tab, role, used)
    sid = f"page.tab_{tab_slug}.{lk}"
    n = 2
    while sid in used:
        sid = f"page.tab_{tab_slug}.{lk}_{n}"
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


def _atlas_state_title(
    turn: dict[str, Any],
    *,
    tab_labels: list[str],
    assigned_tab: str,
    role: str,
    fw: dict[str, Any],
    is_entry: bool = False,
) -> str:
    """屏面标题：底栏 Tab 短名；同逻辑页靠聚类合并，不靠改标题区分。"""
    from mino_nexus.services.nav_synthesis import infer_selected_tab_label

    selected = infer_selected_tab_label(turn, tab_labels, fallback="", prev_tab="")
    name_tab = selected if _tab_label_allowed(selected, tab_labels) else ""
    if not name_tab and _tab_label_allowed(assigned_tab, tab_labels):
        name_tab = assigned_tab
    if name_tab:
        return _compact_page_title(name_tab)
    return _logical_page_title(
        str(assigned_tab or ""),
        str(role or ""),
        dict(fw or {}),
    )[:24]


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
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
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
    best_by_sig: dict[str, str] = {}

    for st in states:
        sid = str(st.get("id") or "")
        sig = _wireframe_layout_signature(wireframes.get(sid))
        if sig == "empty":
            continue
        prev = best_by_sig.get(sig)
        if not prev:
            best_by_sig[sig] = sid
            continue
        prev_score = int((cluster_meta.get(prev) or {}).get("visit_count") or 0) + incident[prev]
        cur_score = int((cluster_meta.get(sid) or {}).get("visit_count") or 0) + incident[sid]
        keep, lose = (prev, sid) if prev_score >= cur_score else (sid, prev)
        best_by_sig[sig] = keep
        remap[lose] = keep
        drop_ids.add(lose)

    if not remap:
        return states, edges, wireframes

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
    return new_states, new_edges, new_wf


def _region_hotspot_key(region: dict[str, Any]) -> str:
    return f"{region.get('source') or 'r'}-{region.get('id') or 0}"


def _hotspot_target_id(state_id: str, region_key: str) -> str:
    return f"{state_id}::{region_key}"


def _pick_tab_hotspot(prev_turn: dict[str, Any], target_tab: str) -> str:
    """底栏 Tab 文案对应的可点线框区域（供跨 Tab 跳转连线）。"""
    want = str(target_tab or "").strip()
    if not want:
        return ""
    wf = _turn_wireframe(prev_turn)
    chrome = wf.get("chrome") or {}
    bottom = float(chrome.get("bottom") or 0.88)
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
    label = str(action_label or "").strip()
    fallback = ""
    for region in wf.get("regions") or []:
        if not region.get("clickable"):
            continue
        rk = _region_hotspot_key(region)
        if not fallback:
            fallback = rk
        rlabel = str(region.get("label") or "").strip()
        if not label or label in ("进入", "返回"):
            return rk
        if rlabel and (rlabel == label or label in rlabel or rlabel in label):
            return rk
    return fallback


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
        if not regions:
            continue
        region_key = ""
        if "::" in hs:
            region_key = hs.split("::", 1)[1]
        for region in regions:
            rk = _region_hotspot_key(region)
            if region_key and rk != region_key:
                continue
            if region.get("clickable") or region_key:
                region["nav_to"] = dst
                region["clickable"] = True
                if meta.get("action_label"):
                    region["nav_label"] = str(meta.get("action_label"))
                break
        wf["regions"] = regions
        wireframes[src] = wf


_ROLE_ENTRY_ORDER = ("main", "profile", "feed", "detail", "chrome")


def _pick_tab_entry_sid(tab: str, state_key_to_id: dict[Any, str]) -> str:
    keys = [k for k in state_key_to_id if isinstance(k, tuple) and k[0] == tab]
    if not keys:
        return ""

    def _rank(key: tuple[str, ...]) -> tuple[int, str]:
        role = str(key[1] or "") if len(key) > 1 else ""
        try:
            ri = _ROLE_ENTRY_ORDER.index(role)
        except ValueError:
            ri = 99
        lk = str(key[2] or "") if len(key) > 2 else ""
        return ri, lk

    keys.sort(key=_rank)
    return str(state_key_to_id.get(keys[0]) or "")


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


def _infer_transition_action(
    prev_turn: dict[str, Any],
    cur_turn: dict[str, Any],
    *,
    label: str,
) -> str:
    if str(label or "").strip() == "返回":
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
    """从上一屏可点区域猜操作文案（证据，非硬编码 App 文案表）。"""
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


def _atlas_cluster_doc(
    app_id: str,
    filtered: list[dict[str, Any]],
    *,
    project_id: str,
    y_tab: int,
    cap_meta: dict[str, Any],
) -> dict[str, Any] | None:
    """按 Tab + 布局角色/chrome 合并为关系结构（与 nav_synthesis 同聚类键）。"""
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
        assign_turn_tabs,
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
    if not tab_labels:
        return None

    exclude_tabs = set(tab_labels)
    from mino_nexus.services.nav_tab_prefs import resolve_home_tab_label

    home_tab = resolve_home_tab_label(app_id, tab_labels)
    turn_tabs = assign_turn_tabs(
        filtered,
        tab_labels,
        y_tab=y_tab,
        exclude=exclude_tabs,
        home_tab=home_tab,
    )

    timeline: list[dict[str, Any]] = []
    for i, turn in enumerate(filtered):
        tab = turn_tabs[i] if i < len(turn_tabs) else home_tab
        fw = detect_layout_framework(turn, y_tab_max=y_tab, exclude=exclude_tabs)
        chrome = stable_chrome_texts(turn, y_tab_max=y_tab, exclude=exclude_tabs)
        fp, chrome_key = framework_fingerprint(fw, chrome)
        timeline.append(
            {
                "turn": turn,
                "tab": tab,
                "framework": fw,
                "chrome": chrome,
                "fp": fp,
                "chrome_key": chrome_key,
            }
        )

    used_ids: set[str] = set()
    tab_entry_id: dict[str, str] = {}
    state_key_to_id: dict[Any, str] = {}
    screen_rows: list[dict[str, Any]] = []
    cluster_meta: dict[str, dict[str, Any]] = {}

    for tab in tab_labels:
        buckets: dict[tuple[str, str, str], dict[str, Any]] = {}
        for row in timeline:
            if str(row.get("tab") or "") != tab:
                continue
            fw = dict(row.get("framework") or {})
            role = semantic_page_role(fw)
            turn = row["turn"]
            wf = _turn_wireframe(turn, app_id=app_id)
            layout_key = _atlas_layout_cluster_key(row, wf, role)
            bkey = (tab, role, layout_key)
            bucket = buckets.setdefault(
                bkey,
                {"frameworks": [], "chromes": [], "layout_key": layout_key, "role": role},
            )
            bucket["frameworks"].append(fw)
            for text in row.get("chrome") or []:
                val = str(text or "").strip()
                if val and val not in bucket["chromes"]:
                    bucket["chromes"].append(val)

        for bkey, bucket in buckets.items():
            _tab, role, layout_key = bkey
            merged_fw = merge_frameworks(bucket["frameworks"])
            sid = _slug_atlas_page(tab, role, layout_key, used_ids)
            chromes = list(bucket["chromes"])[:6]
            screen_rows.append(
                {
                    "id": sid,
                    "tab": tab,
                    "framework": merged_fw,
                    "chrome": chromes,
                    "is_entry": False,
                    "page_role": role,
                }
            )
            state_key_to_id[(tab, role, layout_key)] = sid
            cluster_meta[sid] = {
                "visit_count": 0,
                "wireframe": None,
                "best_score": -1,
                "display_name": "",
                "tab": tab,
                "page_role": role,
                "chrome": list(bucket["chromes"])[:6],
                "framework": merged_fw,
            }

    for tab in tab_labels:
        sid = _pick_tab_entry_sid(tab, state_key_to_id)
        if sid:
            tab_entry_id[tab] = sid

    turn_cluster_ids: list[str] = []
    for row in timeline:
        tab = row["tab"]
        role = semantic_page_role(row["framework"])
        turn = row["turn"]
        turn = row["turn"]
        wf = _turn_wireframe(turn, app_id=app_id)
        layout_key = _atlas_layout_cluster_key(row, wf, role)
        sid = (
            state_key_to_id.get((tab, role, layout_key))
            or tab_entry_id.get(tab, "")
        )
        turn_cluster_ids.append(sid)
        score = len(wf.get("regions") or []) * 1000 + int(turn.get("at") or 0)
        meta = cluster_meta.get(sid)
        if not meta:
            continue
        meta["visit_count"] = int(meta.get("visit_count") or 0) + 1
        if score >= int(meta.get("best_score") or -1):
            meta["best_score"] = score
            meta["wireframe"] = wf
            name, _, stable = atlas_page_label(
                turn,
                y_tab=y_tab,
                tab=tab,
                exclude=exclude_tabs,
                page_role=role,
            )
            entry_sid = tab_entry_id.get(tab) or ""
            meta["display_name"] = _atlas_state_title(
                turn,
                tab_labels=tab_labels,
                assigned_tab=tab,
                role=role,
                fw=dict(row.get("framework") or {}),
                is_entry=(sid == entry_sid),
            )

    screen_rows, wf_remap = _merge_wireframe_duplicate_clusters(
        screen_rows, cluster_meta, turn_cluster_ids
    )
    if wf_remap:
        for tab in tab_labels:
            sid = tab_entry_id.get(tab) or ""
            if sid in wf_remap:
                tab_entry_id[tab] = wf_remap[sid]

    state_parents: dict[str, str] = {}
    for i, cid in enumerate(turn_cluster_ids):
        if not cid:
            continue
        if cid in set(tab_entry_id.values()):
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
            if prev_c and prev_c != cid and prev_c not in tab_entry_id.values():
                state_parents[cid] = prev_c
                continue
        # 不把每个子页默认挂到 Tab 下（避免关系图变成「移动端→Tab→全是包含」海星）

    subs_by_tab: dict[str, list[str]] = {t: [] for t in tab_labels}
    for row in screen_rows:
        tab = str(row.get("tab") or "")
        sid = str(row.get("id") or "")
        entry_sid = tab_entry_id.get(tab) or ""
        if tab in subs_by_tab and sid and sid != entry_sid:
            subs_by_tab[tab].append(sid)

    states: list[dict[str, Any]] = []
    wireframes: dict[str, Any] = {}
    for row in screen_rows:
        sid = str(row["id"] or "")
        meta = cluster_meta.get(sid) or {}
        tab = str(row.get("tab") or "")
        is_entry = sid == tab_entry_id.get(tab)
        display_name = str(meta.get("display_name") or "").strip() or _logical_page_title(
            str(row.get("tab") or ""),
            str(row.get("page_role") or ""),
            dict(row.get("framework") or {}),
        )
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
                    "tab": str(row.get("tab") or ""),
                    "parent_state_id": str(state_parents.get(sid) or ""),
                },
            }
        )

    edges: list[dict[str, Any]] = []
    seen_edge: set[str] = set()

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
        if not src or not dst or src == dst or eid in seen_edge:
            return
        seen_edge.add(eid)
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
    for i in range(len(turn_cluster_ids) - 1):
        a, b = turn_cluster_ids[i], turn_cluster_ids[i + 1]
        tab_a = str(timeline[i].get("tab") or "")
        tab_b = str(timeline[i + 1].get("tab") or "")
        if tab_a not in tab_labels:
            tab_a = ""
        if tab_b not in tab_labels:
            tab_b = ""
        tab_switch = (
            tab_a
            and tab_b
            and tab_a != tab_b
            and _tab_switch_evidence(timeline[i]["turn"], tab_b, tab_labels)
        )
        if tab_switch:
            a = tab_entry_id.get(tab_a) or a
            b = tab_entry_id.get(tab_b) or b
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
                    if action_type == "tab" and raw_label not in tab_labels:
                        action_type = "tap"
                    label = _format_nav_action_label(action_type, raw_label)
                    rk = _pick_transition_hotspot(timeline[i]["turn"], raw_label)
                nav_labels[pair] = label
                nav_action_types[pair] = action_type
                if rk:
                    nav_hotspots[pair] = _hotspot_target_id(a, rk)

    tab_entry_ids = set(tab_entry_id.values())
    for st in states:
        sid = str(st.get("id") or "")
        parent = str((st.get("meta") or {}).get("parent_state_id") or "")
        if not sid or not parent or parent == sid:
            continue
        if parent in tab_entry_ids:
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
            rev = nav_transitions[(dst, src)]
            rev_hs = nav_hotspots.get((dst, src), "")
            rev_extra: dict[str, Any] = {
                "count": int(rev),
                "reverse": True,
                "action_type": "back",
            }
            if rev_hs:
                rev_extra["from_hotspot_id"] = rev_hs
            _add_edge(
                f"edge.atlas.nav.{dst.split('.')[-1]}_back_{src.split('.')[-1]}",
                dst,
                src,
                action_label="返回",
                extra_meta=rev_extra,
            )

    for row in screen_rows:
        tab = str(row.get("tab") or "")
        entry = tab_entry_id.get(tab, "")
        sid = str(row.get("id") or "")
        if not entry or not sid or entry == sid:
            continue
        if any(
            str(e.get("from") or "") == entry and str(e.get("to") or "") == sid
            for e in edges
        ):
            continue
        _add_edge(
            f"edge.atlas.tabscope.{entry.split('.')[-1]}_{sid.split('.')[-1]}",
            entry,
            sid,
            kind="tab_scope",
            action_label="同级页面",
            extra_meta={"display_only": True, "action_type": "scope"},
        )

    states, edges, wireframes = _prune_orphan_duplicate_screens(
        states, edges, wireframes, cluster_meta
    )
    for st in states:
        sid = str(st.get("id") or "")
        cm = cluster_meta.get(sid) or {}
        dn = str(cm.get("display_name") or "").strip()
        if dn:
            st.setdefault("meta", {})
            st["meta"]["display_name"] = dn
    _annotate_wireframes_from_edges(wireframes, edges)

    for i in range(len(turn_cluster_ids) - 1):
        a, b = turn_cluster_ids[i], turn_cluster_ids[i + 1]
        if not a or not b or a == b:
            continue
        if any(
            str(e.get("kind") or "") == "nav"
            and str(e.get("from") or "") == a
            and str(e.get("to") or "") == b
            for e in edges
        ):
            continue
        _add_edge(
            f"edge.atlas.seq.{i}_{a.split('.')[-1]}_{b.split('.')[-1]}",
            a,
            b,
            action_label="进入",
            extra_meta={"action_type": "tap", "source": "timeline_seq"},
        )
    _annotate_wireframes_from_edges(wireframes, edges)

    subs_by_tab = {t: [] for t in tab_labels}
    for st in states:
        tab = str((st.get("meta") or {}).get("tab") or "")
        sid = str(st.get("id") or "")
        entry_sid = tab_entry_id.get(tab) or ""
        if tab in subs_by_tab and sid and sid != entry_sid:
            subs_by_tab[tab].append(sid)

    layout_states = _relation_layout(tab_labels, tab_entry_id, subs_by_tab)
    home_id = tab_entry_id.get(home_tab) or ""
    launch_id = ""
    for cid in turn_cluster_ids:
        if cid:
            launch_id = cid
            break

    screen_list = []
    for st in states:
        if st.get("entry"):
            continue
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

    doc: dict[str, Any] = {
        "app_id": app_id,
        "project_id": project_id,
        "version": "v1",
        "meta": {
            "screen_atlas": True,
            "atlas_layout": "tab_relation",
            "atlas_built_at": int(time.time()),
            "capture_turns": len(filtered),
            "capture_sessions": int(cap_meta.get("sessions") or 0),
            "state_wireframes": wireframes,
            "studio_layout": {"states": layout_states},
            "tab_bar": {
                "entries": [tab_entry_id[t] for t in tab_labels if tab_entry_id.get(t)],
                "labels": {tab_entry_id[t]: t for t in tab_labels if tab_entry_id.get(t)},
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
    return {
        "doc": doc,
        "screen_list": screen_list,
        "states": states,
        "edges": edges,
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
        "edges": [],
    }
    screen_list = [
        {
            "screen_id": sid,
            "display_name": str(screens[sid].get("display_name") or sid),
            "visit_count": int(screens[sid].get("visit_count") or 0),
            "wireframe": wireframes.get(sid),
        }
        for sid in order
    ]
    return {"doc": doc, "screen_list": screen_list, "states": states, "edges": []}


def _merge_atlas_manual_patches(doc: dict[str, Any], app_id: str) -> dict[str, Any]:
    """合并用户在 Studio 保存的手动跳转边（不被下次自动聚类覆盖）。"""
    from mino_nexus.services import nav_fsm_store as store

    saved = store.read_raw(app_id)
    if not isinstance(saved, dict):
        return doc
    meta_saved = saved.get("meta") if isinstance(saved.get("meta"), dict) else {}
    manual = meta_saved.get("atlas_manual_edges")
    if not isinstance(manual, list) or not manual:
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


def _augment_atlas_from_fsm_localized_captures(
    doc: dict[str, Any],
    app_id: str,
    filtered: list[dict[str, Any]],
) -> dict[str, Any]:
    """把跑批 localize 命中的 NavFSM 屏（如 page.11 拍摄页）补进架构 Atlas，避免只在路线图有、架构图没有。"""
    from mino_nexus.services import nav_fsm_store as store
    from mino_nexus.services.nav_compiler import state_label

    fsm, _ = store.load_with_reason(app_id)
    if not fsm or not filtered:
        return doc
    out = dict(doc)
    states = list(out.get("states") or [])
    edges = list(out.get("edges") or [])
    meta = dict(out.get("meta") or {})
    wireframes = dict(meta.get("state_wireframes") or {})
    known = {str(s.get("id") or "") for s in states}
    fsm_by_id = {str(s.get("id") or ""): s for s in (fsm.get("states") or []) if s}

    best: dict[str, tuple[int, dict[str, Any]]] = {}
    for turn in filtered:
        loc = turn.get("localized") if isinstance(turn.get("localized"), dict) else {}
        sid = str(loc.get("chosen") or turn.get("state_id") or "").strip()
        if not sid or sid not in fsm_by_id:
            continue
        score = len(turn.get("nodes") or []) * 1000 + int(turn.get("at") or 0)
        prev = best.get(sid)
        if not prev or score > prev[0]:
            best[sid] = (score, turn)

    for sid, (_, turn) in best.items():
        if sid in known:
            continue
        st = dict(fsm_by_id[sid])
        wf = _turn_wireframe(turn, app_id=app_id)
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

    seen_eids = {str(e.get("id") or "") for e in edges}
    for ed in fsm.get("edges") or []:
        if str(ed.get("kind") or "nav") != "nav":
            continue
        f = str(ed.get("from") or "")
        t = str(ed.get("to") or "")
        if f not in known or t not in known:
            continue
        eid = str(ed.get("id") or f"edge.{f}_to_{t}")
        if eid in seen_eids:
            continue
        seen_eids.add(eid)
        edges.append(dict(ed))

    meta["state_wireframes"] = wireframes
    out["states"] = states
    out["edges"] = edges
    out["meta"] = meta
    return out


def build_atlas(
    app_id: str,
    *,
    project_id: str = "",
    session_id: str = "",
) -> dict[str, Any]:
    """从采集构建 Screen Atlas：Tab 关系结构 + 合并相似页。"""
    ordered, cap_meta = capture.iter_cumulative_turns(app_id, limit_sessions=40, limit_turns=2000)
    if session_id:
        ordered = [t for t in ordered if str(t.get("session_id") or "") == str(session_id)]
    filtered = [t for t in ordered if not is_system_screen(t)]

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
            "updated_at": int(time.time()),
        }

    from mino_nexus.services.nav_screen_layout import infer_content_bands

    y_tab = 0
    if filtered:
        bands = infer_content_bands(filtered[0].get("nodes") or [])
        y_tab = int(bands.get("content_bottom_px") or 0)

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
    from mino_nexus.services.nav_synthesis import (
        _is_bad_tab_slot_label,
        extract_tab_bar_slots,
    )
    from mino_nexus.services.nav_tab_prefs import load_tab_bar_prefs, resolve_home_tab_label
    from mino_nexus.services.nav_target_scope import resolve_app_target_scope

    prefs = load_tab_bar_prefs(app_id) if str(app_id or "").strip() else {}
    if not (prefs.get("labels") or []):
        scope = resolve_app_target_scope(app_id)
        slots = extract_tab_bar_slots(filtered, scope=scope, app_id=app_id)
        from mino_nexus.services.nav_tab_slots import tab_labels_from_slots

        suggested = [t for t in tab_labels_from_slots(slots) if not _is_bad_tab_slot_label(t)]
        if suggested:
            doc_meta["suggested_tab_bar_prefs"] = {
                "labels": suggested[:6],
                "home_tab_label": resolve_home_tab_label(app_id, suggested),
            }
    doc = {**doc, "meta": doc_meta}

    return {
        "app_id": app_id,
        "project_id": project_id,
        "source": "screen_atlas",
        "doc": doc,
        "screens": built["screen_list"],
        "screen_count": len(built["screen_list"]),
        "edge_count": len(built["edges"]),
        "capture": {
            **cap_meta,
            "turns_app": len(filtered),
            "explore_turns": explore_turns,
        },
        "updated_at": int(time.time()),
    }


def explore_progress_block(
    *,
    known_screen_ids: list[str],
    step: int,
    max_steps: int,
    idle_steps: int,
    max_idle_steps: int,
) -> str:
    lines = [
        f"【探索进度】步数 {step}/{max_steps}；连续无新屏 {idle_steps}/{max_idle_steps}",
        f"已发现 {len(known_screen_ids)} 个不同屏面：{', '.join(known_screen_ids[-8:]) or '（尚无）'}",
        "尽量切换底栏、进入列表项、返回后再去未去过区域；无法发现新屏时可 signal_done 结束探索。",
    ]
    return "\n".join(lines)
