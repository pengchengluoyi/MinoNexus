"""从 hierarchy 采集样本合成可跑的 NavFSM（Tab 栏优先，其次内容聚类）。"""
from __future__ import annotations

import re
from typing import Any

_TIME_RE = re.compile(r"^\d{1,2}:\d{2}$")
_DIGIT_RE = re.compile(r"^\d{1,4}$")
_SELECTED_RID_RE = re.compile(r"(?:^|[/_])(?:selected|checked|active|current)(?:[_/]|$)", re.I)


def _is_noise(text: str) -> bool:
    val = str(text or "").strip()
    if len(val) < 2 or len(val) > 48:
        return True
    if _TIME_RE.match(val) or _DIGIT_RE.match(val):
        return True
    return False


def _is_tab_candidate_text(text: str) -> bool:
    """底栏 Tab 候选：只看长度/形态，不写 App 文案白名单。"""
    val = str(text or "").strip()
    if not val or len(val) > 10:
        return False
    if val.startswith(("#", "¥", "$", "+", "《")) or val.endswith("》"):
        return False
    if _TIME_RE.match(val) or _DIGIT_RE.match(val):
        return False
    if len(val) == 1 and not val.isalnum():
        return False
    return True


def _bounds_bottom(node: dict[str, Any]) -> int:
    b = node.get("bounds") or []
    if isinstance(b, (list, tuple)) and len(b) >= 4:
        return int(b[3])
    c = node.get("center") or []
    if isinstance(c, (list, tuple)) and len(c) >= 2:
        return int(c[1])
    return 0


def _screen_height(samples: list[dict[str, Any]]) -> int:
    max_y = 0
    for sample in samples:
        for node in sample.get("nodes") or []:
            b = node.get("bounds") or []
            if isinstance(b, (list, tuple)) and len(b) >= 4:
                max_y = max(max_y, int(b[3]))
    return max_y or 2400


def _tab_bar_y_min(samples: list[dict[str, Any]], *, bottom_ratio: float = 0.14) -> int:
    height = _screen_height(samples)
    return int(height * (1.0 - bottom_ratio))


def _pick_home_tab(tab_labels: list[str], tab_turns: dict[str, list[dict[str, Any]]]) -> str:
    """Recover 默认回到底栏最左 Tab（结构约定），不按 App 文案猜。"""
    if not tab_labels:
        return ""
    if len(tab_labels) == 1:
        return tab_labels[0]
    return tab_labels[0]


def _order_tabs_left_to_right(tab_labels: list[str], samples: list[dict[str, Any]], *, y_min: int) -> list[str]:
    """按底栏从左到右排序，便于生成可读的 state / edge id。"""
    centers: dict[str, list[float]] = {t: [] for t in tab_labels}
    for sample in samples:
        for node in sample.get("nodes") or []:
            text = str(node.get("text") or "").strip()
            if text not in centers or _bounds_bottom(node) < y_min:
                continue
            bounds = node.get("bounds") or []
            if isinstance(bounds, (list, tuple)) and len(bounds) >= 4:
                centers[text].append((int(bounds[0]) + int(bounds[2])) / 2.0)
    return sorted(
        tab_labels,
        key=lambda t: (
            sum(centers.get(t) or []) / max(1, len(centers.get(t) or [])),
            tab_labels.index(t),
        ),
    )


def _tab_bar_band(samples: list[dict[str, Any]], *, bottom_ratio: float = 0.14) -> tuple[int, int]:
    """返回 (band_top_px, band_bottom_px) 底栏垂直带。"""
    from mino_nexus.services.nav_screen_layout import infer_content_bands

    height = _screen_height(samples)
    y_bottom = _tab_bar_y_min(samples, bottom_ratio=bottom_ratio)
    if samples:
        bands = infer_content_bands(samples[0].get("nodes") or [])
        y_bottom = int(bands.get("content_bottom_px") or y_bottom)
    band_top = max(0, y_bottom - max(120, int(height * 0.12)))
    return band_top, height


def _horizontal_tab_row_labels(turn: dict[str, Any]) -> list[str]:
    """底栏 Tab：最底水平行聚类（nav_screen_layout），无屏幕百分比阈值。"""
    from mino_nexus.services.nav_screen_layout import find_bottom_horizontal_tab_row

    nodes = turn.get("nodes") or []
    row = find_bottom_horizontal_tab_row(nodes)
    if len(row) < 2:
        return []
    return [t for t in row if _is_tab_candidate_text(t)]


def extract_tab_bar_labels(
    samples: list[dict[str, Any]],
    *,
    scope: Any = None,
) -> list[str]:
    """从底栏水平 Tab 行推断标签；权限弹窗竖排按钮不计入 Tab。"""
    from mino_nexus.services.nav_capture_store import synthesis_turns
    from mino_nexus.services.nav_screen_layout import infer_tab_bar_band

    app_samples = synthesis_turns(samples, scope=scope)
    if not app_samples:
        return []
    counts: dict[str, int] = {}
    for sample in app_samples:
        row = _horizontal_tab_row_labels(sample)
        if len(row) < 2:
            continue
        for text in row:
            counts[text] = counts.get(text, 0) + 1
    if not counts:
        return []
    tabs = [t for t, c in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])) if c >= 2]
    if len(tabs) < 2:
        tabs = [t for t, c in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])) if c >= 1][:8]
    if len(tabs) < 2:
        return []
    band = infer_tab_bar_band(app_samples[0].get("nodes") or [])
    y_min = int(band.get("band_top_px") or band.get("content_bottom_px") or 0)
    tabs = _order_tabs_left_to_right(tabs[:8], app_samples, y_min=y_min)
    return tabs[:8]


def _content_fingerprint(sample: dict[str, Any], *, y_max: int, exclude: set[str]) -> set[str]:
    return set(_content_texts(sample, y_max=y_max, exclude=exclude)[:6])


def _segment_turns(
    ordered: list[dict[str, Any]],
    *,
    y_max: int,
    exclude: set[str],
) -> list[list[dict[str, Any]]]:
    """同一 Tab 内刷 feed 合并为一段；换 Tab 时内容突变开新段。"""
    if not ordered:
        return []
    segments: list[list[dict[str, Any]]] = [[ordered[0]]]
    prev = _content_fingerprint(ordered[0], y_max=y_max, exclude=exclude)
    for sample in ordered[1:]:
        cur = _content_fingerprint(sample, y_max=y_max, exclude=exclude)
        if not cur:
            segments[-1].append(sample)
            continue
        overlap = len(prev & cur) / max(1, len(prev | cur))
        if overlap < 0.32:
            segments.append([sample])
        else:
            segments[-1].append(sample)
        prev = cur
    return segments


def _assign_segments_to_tabs(
    segments: list[list[dict[str, Any]]],
    tab_labels: list[str],
    home_tab: str,
) -> list[tuple[list[dict[str, Any]], str]]:
    if not segments:
        return []
    out: list[tuple[list[dict[str, Any]], str]] = [(segments[0], home_tab)]
    prev_tab = home_tab
    for seg in segments[1:]:
        candidates = [t for t in tab_labels if t != prev_tab]
        nxt = candidates[0] if candidates else tab_labels[0]
        out.append((seg, nxt))
        prev_tab = nxt
    return out


def _content_texts(sample: dict[str, Any], *, y_max: int, exclude: set[str]) -> list[str]:
    out: list[str] = []
    for node in sample.get("nodes") or []:
        if _bounds_bottom(node) > y_max:
            continue
        text = str(node.get("text") or "").strip()
        if _is_noise(text) or text in exclude:
            continue
        if text not in out:
            out.append(text)
        if len(out) >= 8:
            break
    return out


def _slug_tab(label: str, used: set[str]) -> str:
    slug = re.sub(r"\s+", "_", label.strip())[:16]
    slug = re.sub(r"[^\w\u4e00-\u9fff]", "", slug) or "tab"
    sid = f"page.tab_{slug}"
    n = 2
    while sid in used:
        sid = f"page.tab_{slug}_{n}"
        n += 1
    used.add(sid)
    return sid


def _slug_semantic_sub(tab: str, role: str, used: set[str]) -> str:
    """业务子页 id：page.tab_{tab}.feed / profile / detail，不按控件组合编号。"""
    tab_slug = re.sub(r"[^\w\u4e00-\u9fff]", "", tab.strip())[:12] or "tab"
    slug = re.sub(r"[^\w]", "_", str(role or "main").strip())[:16] or "main"
    sid = f"page.tab_{tab_slug}.{slug}"
    n = 2
    while sid in used:
        sid = f"page.tab_{tab_slug}.{slug}_{n}"
        n += 1
    used.add(sid)
    return sid


def _tab_labels_from_selection(
    nodes: list[dict[str, Any]],
    tab_labels: list[str],
    *,
    band_top: int,
) -> list[str]:
    """底栏选中态：selected/checked 或 resource-id 结构特征。"""
    selected: list[str] = []
    for node in nodes or []:
        text = str(node.get("text") or node.get("content_desc") or "").strip()
        if text not in tab_labels:
            continue
        if _bounds_bottom(node) < band_top:
            continue
        rid = str(node.get("resource_id") or "")
        cls = str(node.get("class") or "")
        if node.get("selected") or node.get("checked"):
            selected.append(text)
        elif _SELECTED_RID_RE.search(rid) or _SELECTED_RID_RE.search(cls):
            selected.append(text)
    return list(dict.fromkeys(selected))


def infer_selected_tab_label(
    turn: dict[str, Any],
    tab_labels: list[str],
    *,
    band_top: int = 0,
    fallback: str = "",
    prev_tab: str = "",
) -> str:
    """单帧 Tab 推断：localize > 选中态；底栏未选中时不在此猜（避免全 Tab 共现误判）。"""
    chosen = str((turn.get("localized") or {}).get("chosen") or "").strip()
    if chosen.startswith("page.tab_"):
        for tab in tab_labels:
            if tab in chosen:
                return tab
    nodes = turn.get("nodes") or []
    if band_top <= 0:
        band_top, _ = _tab_bar_band([turn])
    selected = _tab_labels_from_selection(nodes, tab_labels, band_top=band_top)
    if len(selected) == 1:
        return selected[0]
    return prev_tab or fallback


def _segment_signature(
    turn: dict[str, Any],
    *,
    y_tab: int,
    exclude: set[str],
) -> tuple[str, str, tuple[str, ...]]:
    from mino_nexus.services.nav_layout import (
        detect_layout_framework,
        framework_fingerprint,
        semantic_page_role,
        stable_chrome_texts,
    )

    fw = detect_layout_framework(turn, y_tab_max=y_tab, exclude=exclude)
    chrome = stable_chrome_texts(turn, y_tab_max=y_tab, exclude=exclude)
    role = semantic_page_role(fw)
    fp, _ = framework_fingerprint(fw, chrome)
    content_fp = tuple(sorted(_content_fingerprint(turn, y_max=y_tab, exclude=exclude))[:4])
    return role, fp, content_fp


def _bottom_tab_node_states(
    turn: dict[str, Any],
    tab_labels: list[str],
    *,
    band_top: int,
) -> dict[str, tuple[bool, bool, bool, str, str]]:
    out: dict[str, tuple[bool, bool, bool, str, str]] = {}
    for node in turn.get("nodes") or []:
        text = str(node.get("text") or node.get("content_desc") or "").strip()
        if text not in tab_labels or _bounds_bottom(node) < band_top:
            continue
        out[text] = (
            bool(node.get("selected")),
            bool(node.get("checked")),
            bool(node.get("focused")),
            str(node.get("resource_id") or ""),
            str(node.get("class") or ""),
        )
    return out


def _infer_tab_from_bottom_diff(
    prev_turn: dict[str, Any],
    cur_turn: dict[str, Any],
    tab_labels: list[str],
    *,
    band_top: int,
) -> str:
    """Tab 切换时底栏节点状态通常只有一项变化。"""
    prev_states = _bottom_tab_node_states(prev_turn, tab_labels, band_top=band_top)
    cur_states = _bottom_tab_node_states(cur_turn, tab_labels, band_top=band_top)
    changed = [label for label in tab_labels if prev_states.get(label) != cur_states.get(label)]
    if len(changed) == 1:
        return changed[0]
    for label in changed:
        cur = cur_states.get(label, ())
        if cur and (cur[0] or cur[1] or _SELECTED_RID_RE.search(cur[3]) or _SELECTED_RID_RE.search(cur[4])):
            return label
    return ""


def assign_turn_tabs(
    turns: list[dict[str, Any]],
    tab_labels: list[str],
    *,
    y_tab: int,
    exclude: set[str],
    home_tab: str,
) -> list[str]:
    """逐帧 Tab 对齐：选中态 > 底栏 diff > 结构签名缓存 > 粘性上一帧。"""
    if not turns or not tab_labels:
        return []
    band_top, _ = _tab_bar_band(turns)
    sig_to_tab: dict[tuple[str, str, tuple[str, ...]], str] = {}
    out: list[str] = []
    prev_tab = home_tab

    for i, turn in enumerate(turns):
        tab = infer_selected_tab_label(
            turn,
            tab_labels,
            band_top=band_top,
            fallback="",
            prev_tab="",
        )
        if not tab and i > 0:
            tab = _infer_tab_from_bottom_diff(turns[i - 1], turn, tab_labels, band_top=band_top)
        sig = _segment_signature(turn, y_tab=y_tab, exclude=exclude)
        if not tab:
            tab = sig_to_tab.get(sig, "")
        if not tab:
            tab = prev_tab or home_tab

        if infer_selected_tab_label(turn, tab_labels, band_top=band_top, fallback="", prev_tab="") or (
            i > 0 and _infer_tab_from_bottom_diff(turns[i - 1], turn, tab_labels, band_top=band_top)
        ):
            sig_to_tab[sig] = tab
        elif sig not in sig_to_tab:
            sig_to_tab[sig] = tab

        out.append(tab)
        prev_tab = tab
    return out


def _is_login_turn(turn: dict[str, Any]) -> bool:
    """登录屏：多个输入框 / 密码框，不用 App 文案关键字。"""
    edits = 0
    has_password = False
    for node in turn.get("nodes") or []:
        cls = str(node.get("class") or "")
        if "EditText" in cls:
            edits += 1
        if node.get("password"):
            has_password = True
    return edits >= 2 or (edits >= 1 and has_password)


def _build_identify_block(
    *,
    tab: str,
    framework: dict[str, Any],
    chrome: list[str],
    include_framework: bool = True,
    is_entry: bool = False,
) -> dict[str, Any]:
    from mino_nexus.services.nav_layout import is_volatile_text

    required: list[dict[str, Any]] = []
    if tab:
        required.append({"signal": "tab_bar", "match": {"selected": tab}})
    kind = str(framework.get("kind") or "")
    if include_framework and kind and kind not in ("unknown", "tab_shell"):
        match = {k: framework[k] for k in ("kind", "columns", "has_back", "widgets") if k in framework}
        required.append({"signal": "layout_framework", "match": match})
    stable = [t for t in chrome if t and not is_volatile_text(t)]
    if stable and not is_entry and kind in ("profile_page", "detail_page", "chrome_page"):
        required.append({"signal": "text_landmarks", "any": stable[:4], "none_of": []})
    return {"required": required}


def build_from_tab_bar(
    app_id: str,
    ordered: list[dict[str, Any]],
    *,
    project_id: str,
    session_id: str,
    capture_meta: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    from mino_nexus.services import nav_fsm_template as tpl
    from mino_nexus.services.nav_candidate_compiler import autofill_draft_from_captures
    from mino_nexus.services.nav_layout import (
        detect_layout_framework,
        framework_fingerprint,
        merge_frameworks,
        semantic_page_role,
        stable_chrome_texts,
    )

    from mino_nexus.services.nav_capture_store import synthesis_turns
    from mino_nexus.services.nav_target_scope import resolve_app_target_scope

    scope = resolve_app_target_scope(app_id)
    app_turns = synthesis_turns(ordered, scope=scope)
    tab_labels = extract_tab_bar_labels(app_turns or ordered, scope=scope)
    if len(tab_labels) < 2:
        return None

    from mino_nexus.services.nav_screen_layout import infer_content_bands

    sample_nodes = (app_turns[0].get("nodes") or []) if app_turns else []
    bands = infer_content_bands(sample_nodes)
    y_tab = int(bands.get("content_bottom_px") or _tab_bar_y_min(ordered, bottom_ratio=0.14))
    exclude_tabs = set(tab_labels)
    home_tab = _pick_home_tab(tab_labels, {})

    login_turns: list[dict[str, Any]] = []
    main_turns: list[dict[str, Any]] = []
    for sample in app_turns:
        if _is_login_turn(sample):
            login_turns.append(sample)
        else:
            main_turns.append(sample)

    if len(main_turns) < 2:
        return None

    turn_tabs = assign_turn_tabs(
        main_turns,
        tab_labels,
        y_tab=y_tab,
        exclude=exclude_tabs,
        home_tab=home_tab,
    )

    # 按时间线：每帧 → Tab + 布局框架（不含流式商品文案）
    timeline: list[dict[str, Any]] = []
    for i, turn in enumerate(main_turns):
        tab = turn_tabs[i] if i < len(turn_tabs) else home_tab
        fw = detect_layout_framework(turn, y_tab_max=y_tab, exclude=exclude_tabs)
        chrome = stable_chrome_texts(turn, y_tab_max=y_tab, exclude=exclude_tabs)
        fp, chrome_key = framework_fingerprint(fw, chrome)
        timeline.append({"tab": tab, "framework": fw, "chrome": chrome, "fp": fp, "chrome_key": chrome_key})

    # 每个 Tab 内按结构分段
    tab_segments: dict[str, list[tuple[str, tuple[str, ...], dict[str, Any], list[str]]]] = {
        t: [] for t in tab_labels
    }
    tab_turn_counts: dict[str, int] = {t: 0 for t in tab_labels}
    for row in timeline:
        tab = row["tab"]
        tab_turn_counts[tab] = tab_turn_counts.get(tab, 0) + 1
        segs = tab_segments.setdefault(tab, [])
        key = (row["fp"], row["chrome_key"])
        if segs and segs[-1][0] == row["fp"] and segs[-1][1] == row["chrome_key"]:
            continue
        segs.append((row["fp"], row["chrome_key"], row["framework"], row["chrome"]))

    used_ids: set[str] = set()
    tab_entry_id: dict[str, str] = {}
    state_key_to_id: dict[tuple[str, str, tuple[str, ...]], str] = {}
    screens: list[dict[str, Any]] = []

    for tab in tab_labels:
        segs = tab_segments.get(tab) or []
        entry_sid = _slug_tab(tab, used_ids)
        tab_entry_id[tab] = entry_sid
        screens.append(
            {
                "id": entry_sid,
                "kind": "page",
                "tab": tab,
                "framework": {"kind": "tab_shell", "columns": 0, "widgets": ["tab_shell"]},
                "chrome": [],
                "is_entry": True,
                "is_home": tab == home_tab,
            }
        )
        state_key_to_id[(tab, "__entry__")] = entry_sid

        buckets: dict[str, dict[str, Any]] = {}
        for fp, chrome_key, fw, chrome in segs:
            role = semantic_page_role(fw)
            bucket = buckets.setdefault(
                role,
                {"frameworks": [], "chromes": [], "seg_keys": []},
            )
            bucket["frameworks"].append(fw)
            for text in chrome or []:
                val = str(text or "").strip()
                if val and val not in bucket["chromes"]:
                    bucket["chromes"].append(val)
            bucket["seg_keys"].append((fp, chrome_key))

        for role, bucket in buckets.items():
            merged_fw = merge_frameworks(bucket["frameworks"])
            sid = _slug_semantic_sub(tab, role, used_ids)
            screens.append(
                {
                    "id": sid,
                    "kind": "page",
                    "tab": tab,
                    "framework": merged_fw,
                    "chrome": list(bucket["chromes"])[:6],
                    "is_entry": False,
                    "is_home": False,
                    "page_role": role,
                }
            )
            state_key_to_id[(tab, role)] = sid
            for seg_key in bucket["seg_keys"]:
                state_key_to_id[(tab, seg_key[0], seg_key[1])] = sid

    if login_turns:
        login_content = _content_texts(login_turns[0], y_max=y_tab, exclude=set(tab_labels))
        primary = login_content[0] if login_content else "login"
        sid = _slug_tab(primary, used_ids)
        screens.append(
            {
                "id": sid,
                "kind": "page",
                "tab": "",
                "framework": {"kind": "chrome_page", "columns": 1},
                "chrome": login_content[:3],
                "is_entry": False,
                "is_home": False,
                "is_login": True,
            }
        )

    if len(screens) < 2:
        return None

    screen_rows = [(str(s["id"]), str(s["kind"])) for s in screens]
    doc = tpl.build_template(app_id, project_id=project_id, screens=screen_rows)

    for st in doc.get("states") or []:
        sid = str(st.get("id") or "")
        screen = next((s for s in screens if s["id"] == sid), None)
        if not screen:
            continue
        st["identify"] = _build_identify_block(
            tab=str(screen.get("tab") or ""),
            framework=dict(screen.get("framework") or {}),
            chrome=list(screen.get("chrome") or []),
            include_framework=not bool(screen.get("is_entry")),
            is_entry=bool(screen.get("is_entry")),
        )
        st["guards"] = {}
        if screen.get("is_entry"):
            st["entry"] = True
        if screen.get("is_login"):
            st["role"] = "login"

    # Tab 入口互连 + 时间线轨迹边（Tab 内子页 / 跨 Tab）
    nav_edges: list[dict[str, Any]] = []
    seen_edge: set[str] = set()

    def _add_edge(eid: str, src: str, dst: str, *, target_tab: str = "", note: str = "") -> None:
        if not src or not dst or src == dst or eid in seen_edge:
            return
        seen_edge.add(eid)
        effect: list[dict[str, Any]] = []
        if target_tab:
            effect.append({"tab_bar": {"selected": target_tab}})
        dst_screen = next((s for s in screens if s["id"] == dst), None)
        if dst_screen:
            fw = dst_screen.get("framework") or {}
            kind = str(fw.get("kind") or "")
            if kind and kind != "unknown":
                payload = {k: fw[k] for k in ("kind", "columns", "has_back", "widgets") if k in fw}
                effect.append({"layout_framework": payload})
            chrome = [t for t in (dst_screen.get("chrome") or []) if t][:2]
            if chrome and kind in ("profile_page", "detail_page", "chrome_page"):
                effect.append({"text_landmarks": chrome})
        nav_edges.append(
            {
                "id": eid,
                "kind": "nav",
                "from": src,
                "to": dst,
                "guard": {},
                "execute": {"steps": ["tap_element"], **({"target_tab": target_tab} if target_tab else {})},
                "effect_assert": {
                    "within_ms": 8000,
                    "require_any": effect or [{"tab_bar": {"selected": target_tab}}] if target_tab else [],
                    "require_none": [],
                    "state_delta": {},
                },
                "on_fail": {},
                "scroll_into_view": {},
            }
        )

    home_id = tab_entry_id[home_tab]
    for src_tab in tab_labels:
        src_id = tab_entry_id[src_tab]
        for dst_tab in tab_labels:
            if src_tab == dst_tab:
                continue
            _add_edge(
                f"edge.tab.{src_tab}_to_{dst_tab}",
                src_id,
                tab_entry_id[dst_tab],
                target_tab=dst_tab,
            )

    turn_state_ids: list[str] = []
    for row in timeline:
        tab = row["tab"]
        role = semantic_page_role(row["framework"])
        sid = (
            state_key_to_id.get((tab, row["fp"], row["chrome_key"]))
            or state_key_to_id.get((tab, role))
            or tab_entry_id.get(tab, "")
        )
        turn_state_ids.append(sid)

    for i in range(len(turn_state_ids) - 1):
        a, b = turn_state_ids[i], turn_state_ids[i + 1]
        if not a or not b or a == b:
            continue
        short_a = a.split(".")[-1]
        short_b = b.split(".")[-1]
        _add_edge(f"edge.trace.{short_a}_to_{short_b}", a, b)

    doc["edges"] = nav_edges
    meta = dict(doc.get("meta") or {})
    meta["synthesized_from_capture"] = True
    meta["capture_scope"] = "cumulative"
    cap = dict(capture_meta or {})
    meta["capture_sessions"] = int(cap.get("sessions") or 0)
    meta["capture_sessions_used"] = int(cap.get("sessions_used") or 0)
    meta["capture_turns"] = int(cap.get("turns") or len(ordered))
    meta["capture_session_id"] = session_id
    meta["synthesis_mode"] = "tab_bar_layered"
    meta["tab_bar"] = {
        "entries": [tab_entry_id[t] for t in tab_labels],
        "labels": {tab_entry_id[t]: t for t in tab_labels},
        "home_state_id": home_id,
    }
    meta["recover"] = {
        "default_goal_state_id": home_id,
        "case_goal_field": "nav_goal_state",
        "note": "迷路/跨 Tab 导航请调 recovery 能力 fsm_navigate（当前屏→目标屏，返回最短路）。",
    }
    entry_x, node_w, h_gap = 48, 220, 88
    row_min_h, row_pad = 420, 56
    layout_states: dict[str, dict[str, int]] = {}
    row_y = 48
    subs_by_tab: dict[str, list[str]] = {t: [] for t in tab_labels}
    for s in screens:
        if s.get("is_entry"):
            continue
        tab = str(s.get("tab") or "")
        sid = str(s.get("id") or "")
        if tab and sid and tab in subs_by_tab:
            subs_by_tab[tab].append(sid)
    for t in tab_labels:
        eid = tab_entry_id[t]
        subs = sorted(subs_by_tab.get(t) or [])
        layout_states[eid] = {"x": entry_x, "y": row_y}
        for i, sid in enumerate(subs):
            layout_states[sid] = {
                "x": entry_x + node_w + h_gap + i * (node_w + h_gap),
                "y": row_y,
            }
        row_y += row_min_h + row_pad
    orphan_i = 0
    for s in screens:
        sid = str(s.get("id") or "")
        if not sid or sid in layout_states:
            continue
        layout_states[sid] = {"x": entry_x + (orphan_i % 3) * 300, "y": row_y + (orphan_i // 3) * row_min_h}
        orphan_i += 1
    meta["studio_layout"] = {"states": layout_states}
    hc = dict(meta.get("hierarchy_calibration") or {})
    if session_id:
        hc["calibration_id"] = session_id
        hc["evidence_rel_path"] = f"nav/capture/{app_id}/{session_id}"
    else:
        hc["calibration_id"] = f"cumulative:{app_id}"
        hc["evidence_rel_path"] = f"nav/capture/{app_id}"
    hc.setdefault("hierarchy_format", "accessibility_json")
    hc.setdefault("hierarchy_result_key", "nodes")
    hc.setdefault("follow_filled_detectable", "text")
    if home_tab:
        hc["anchor_on_first_screen"] = home_tab
    meta["hierarchy_calibration"] = hc
    if scope:
        meta["target_scope"] = scope.as_dict()
    doc["meta"] = meta
    return autofill_draft_from_captures(app_id, doc)
