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


def _merge_wireframe_duplicate_clusters(
    screen_rows: list[dict[str, Any]],
    cluster_meta: dict[str, dict[str, Any]],
    turn_cluster_ids: list[str],
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """同 Tab 内骨骼或线框一致 → 保留访问最多的一屏，其余合并进 canonical。"""
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
            continue
        wf = cm.get("wireframe") or {}
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
        sub_clusters = split_indices_by_wireframe_similarity(wfs)
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
            refined_buckets[use_fp] = sub
    buckets = refined_buckets

    for skeleton_fp, bucket in buckets.items():
        merged_fw = merge_frameworks(bucket["frameworks"])
        sid = _slug_atlas_page(skeleton_fp, used_ids)
        chromes = list(bucket["chromes"])[:8]
        roles = bucket.get("inferred_roles") or []
        inferred_role = roles[0] if roles else semantic_page_role(merged_fw)
        merged_wf = merge_skeleton_wireframe(list(bucket.get("wireframes") or []))
        display_name = merge_cluster_display_name(list(bucket.get("name_samples") or []))
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
            "wireframe_turns": list(bucket.get("wireframes") or []),
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
                }
            )
        score = len(wf.get("regions") or []) * 1000 + int(turn.get("at") or 0)
        meta = cluster_meta.get(sid)
        if not meta:
            continue
        meta["visit_count"] = int(meta.get("visit_count") or 0) + 1
        if isinstance(wf, dict):
            meta.setdefault("wireframe_turns", []).append(wf)
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

    from mino_nexus.services.nav_app_skeleton import merge_skeleton_wireframe

    for sid, meta in cluster_meta.items():
        turns_wf = meta.pop("wireframe_turns", None)
        if turns_wf:
            merged = merge_skeleton_wireframe(turns_wf)
            if merged:
                meta["wireframe"] = merged

    screen_rows, wf_remap = _merge_wireframe_duplicate_clusters(
        screen_rows, cluster_meta, turn_cluster_ids
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
        if not nav_transitions.get((dst, src)):
            _add_edge(
                f"edge.atlas.nav.{dst.split('.')[-1]}_back_{src.split('.')[-1]}",
                dst,
                src,
                action_label="返回",
                extra_meta={"count": 1, "reverse": True, "action_type": "back"},
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

    layout_states: dict[str, dict[str, int]] = {}
    for i, st in enumerate(states):
        sid = str(st.get("id") or "")
        if not sid:
            continue
        layout_states[sid] = {
            "x": 48 + (i % 4) * 280,
            "y": 48 + (i // 4) * 520,
        }
    home_id = str(states[0].get("id") or "") if states else ""
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
            "state_wireframes": wireframes,
            "studio_layout": {"states": layout_states},
            "tab_bar": {
                "entries": [],
                "labels": {},
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
    edges: list[dict[str, Any]] = []
    seen_e: set[str] = set()
    prev_sid = ""
    prev_turn: dict[str, Any] | None = None
    for turn in filtered:
        sid = fingerprint_turn(turn, y_tab=y_tab, exclude=set())
        if prev_sid and sid and prev_sid != sid:
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
        st = dict(fsm_by_id[sid])
        wf = _turn_wireframe(turn, app_id=app_id)
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
    doc = {**doc, "meta": doc_meta}

    last_capture_at = max((int(t.get("at") or 0) for t in filtered), default=0)
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
