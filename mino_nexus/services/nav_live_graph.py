"""从采集实时合成导航图 + 轨迹；进入页面即最新，无需手点发布。"""
from __future__ import annotations

import re
import time
from collections import Counter
from typing import Any

from mino_nexus.services import nav_capture_store as capture
from mino_nexus.services import nav_fsm_store as store
from mino_nexus.services.nav_candidate_compiler import build_fsm_from_captures, prepare_publish
from mino_nexus.services.nav_capture_store import infer_screen_package, is_system_screen
from mino_nexus.services.nav_screen_layout import (
    merge_layout_views,
    sanitize_wireframe,
    wireframe_from_hierarchy,
)

_SKIP_LOCALIZE_RE = re.compile(r"^page\.(home|list|detail|new)(\.|$)")
_ORPHAN_NOISE_RE = re.compile(r"^page\.(\d+(_\d+)?)$")
_LEGACY_KIND_SUFFIX_RE = re.compile(
    r"^page\.tab_[^.]+\.(feed_grid|feed_list|content_page|tab_shell)(_\d+)?$"
)


def _resolve_project_id(app_id: str, project_id: str = "") -> str:
    if project_id:
        return project_id
    try:
        from mino_nexus.services import project_store as ps

        app = ps.find_app(app_id)
        return str((app or {}).get("project_id") or "")
    except Exception:
        return ""


def _state_ids(doc: dict[str, Any]) -> set[str]:
    return {str(s.get("id") or s.get("state_id") or "").strip() for s in (doc.get("states") or []) if s}


def _edge_key(ed: dict[str, Any]) -> tuple[str, str]:
    return str(ed.get("from") or ""), str(ed.get("to") or "")


def _append_edge(
    edges: list[dict[str, Any]],
    seen: set[tuple[str, str]],
    *,
    eid: str,
    src: str,
    dst: str,
    meta: dict[str, Any] | None = None,
) -> None:
    if not src or not dst or src == dst:
        return
    key = (src, dst)
    if key in seen:
        return
    seen.add(key)
    row = {
        "id": eid,
        "kind": "nav",
        "from": src,
        "to": dst,
        "guard": {},
        "execute": {"steps": ["tap_element"]},
        "effect_assert": {"within_ms": 8000, "require_any": [], "require_none": []},
        "on_fail": {},
    }
    if meta:
        row["meta"] = meta
    edges.append(row)


def _tab_parent_id(state_id: str) -> str:
    """page.tab_我的.feed_grid → page.tab_我的"""
    parts = str(state_id or "").split(".")
    if len(parts) < 3 or not parts[1].startswith("tab_"):
        return ""
    return ".".join(parts[:2])


def augment_nav_connectivity(doc: dict[str, Any], ordered: list[dict[str, Any]]) -> dict[str, Any]:
    """补全子页↔Tab 壳层、观测到的反向边，让路径规划可走通。"""
    out = dict(doc or {})
    states = list(out.get("states") or [])
    known = _state_ids(out)
    edges = list(out.get("edges") or [])
    seen = {_edge_key(e) for e in edges}

    # 子页面 ↔ Tab 入口
    for sid in list(known):
        parent = _tab_parent_id(sid)
        if parent and parent in known:
            slug = sid.split(".")[-1]
            _append_edge(
                edges,
                seen,
                eid=f"edge.shell.{parent.split('.')[-1]}_to_{slug}",
                src=parent,
                dst=sid,
                meta={"inferred": True, "reason": "tab_sub_enter"},
            )
            _append_edge(
                edges,
                seen,
                eid=f"edge.shell.{slug}_to_{parent.split('.')[-1]}",
                src=sid,
                dst=parent,
                meta={"inferred": True, "reason": "tab_sub_back"},
            )

    # 从采集序列提取观测转移（跳过同态重复）
    observed: Counter[tuple[str, str]] = Counter()
    last_sid = ""
    for turn in ordered or []:
        sid = str((turn.get("localized") or {}).get("chosen") or "").strip()
        if not sid:
            continue
        if last_sid and last_sid != sid:
            observed[(last_sid, sid)] += 1
        last_sid = sid

    for (src, dst), count in observed.items():
        if src not in known or dst not in known:
            continue
        _append_edge(
            edges,
            seen,
            eid=f"edge.trace.{src.split('.')[-1]}_to_{dst.split('.')[-1]}",
            src=src,
            dst=dst,
            meta={"observed": True, "count": count},
        )
        # 仅观测到单向时，为路径规划补反向边（执行时仍优先正向）
        if (dst, src) not in observed:
            _append_edge(
                edges,
                seen,
                eid=f"edge.infer.{dst.split('.')[-1]}_to_{src.split('.')[-1]}",
                src=dst,
                dst=src,
                meta={"inferred": True, "reason": "observed_reverse"},
            )

    out["states"] = states
    out["edges"] = edges
    return out


def _resolve_orphan_state_id(sid: str, known: set[str]) -> str:
    """把旧 localize id 映射到 Tab 合成后的 canonical 子页，避免重复节点。"""
    if sid in known:
        return sid
    if _ORPHAN_NOISE_RE.match(sid) or _LEGACY_KIND_SUFFIX_RE.match(sid):
        return ""
    m = re.match(r"^(page\.tab_[^.]+)\.(.+)$", sid)
    if m:
        prefix, tail = m.group(1), m.group(2)
        if tail in ("feed_grid", "feed_list", "content_page", "tab_shell"):
            for kid in sorted(known):
                if kid.startswith(f"{prefix}.") and kid != prefix:
                    return kid
        if prefix in known:
            return prefix
    return sid


def merge_localize_orphans(doc: dict[str, Any], ordered: list[dict[str, Any]]) -> dict[str, Any]:
    """Tab 合成模式下不盲目追加 localize 孤儿页。"""
    out = dict(doc or {})
    states = list(out.get("states") or [])
    known = _state_ids(out)
    mode = str((out.get("meta") or {}).get("synthesis_mode") or "")
    tab_layered = mode == "tab_bar_layered"
    hits: Counter[str] = Counter()
    for turn in ordered or []:
        raw = str((turn.get("localized") or {}).get("chosen") or "").strip()
        if not raw.startswith("page."):
            continue
        if _SKIP_LOCALIZE_RE.match(raw):
            continue
        sid = _resolve_orphan_state_id(raw, known) if tab_layered else raw
        if not sid or sid in known:
            continue
        if tab_layered and (_ORPHAN_NOISE_RE.match(sid) or _LEGACY_KIND_SUFFIX_RE.match(sid)):
            continue
        hits[sid] += 1

    for sid, count in hits.most_common():
        if count < 1:
            continue
        slug = sid.split(".", 1)[-1][:24]
        label = slug.replace("_", " ")
        states.append(
            {
                "id": sid,
                "kind": "page",
                "identify": {
                    "required": [{"signal": "text_landmarks", "any": [label], "none_of": []}],
                },
                "guards": {},
                "meta": {"from_localize": True, "evidence_turns": count},
            }
        )
        known.add(sid)

    out["states"] = states
    meta = dict(out.get("meta") or {})
    meta["localize_orphans_merged"] = len(hits)
    out["meta"] = meta
    return augment_nav_connectivity(out, ordered)


def _capture_ref(turn: dict[str, Any], app_id: str = "") -> dict[str, Any]:
    ref = {
        "app_id": str(app_id or turn.get("app_id") or ""),
        "session_id": str(turn.get("session_id") or ""),
        "turn_id": int(turn.get("turn_id") or 0),
        "has_screenshot": bool(turn.get("has_screenshot") or turn.get("screenshot_rel")),
    }
    return ref


def _turn_wireframe(turn: dict[str, Any], *, app_id: str = "") -> dict[str, Any]:
    nodes = turn.get("nodes") or []
    h = wireframe_from_hierarchy(nodes) if nodes else {}
    v = turn.get("layout_vision") if isinstance(turn.get("layout_vision"), dict) else {}
    wf = sanitize_wireframe(merge_layout_views(h, v))
    cap = _capture_ref(turn, app_id)
    if cap.get("session_id"):
        wf["capture"] = cap
    return wf


def _mark_nav_regions(wf: dict[str, Any], state_id: str, doc: dict[str, Any]) -> dict[str, Any]:
    """可点击组件若文案/描述命中出边 target_tab，标记跳转目标。"""
    regions = list(wf.get("regions") or [])
    if not regions or not state_id:
        return wf
    labels_map = (doc.get("meta") or {}).get("tab_bar", {}).get("labels") or {}
    for ed in doc.get("edges") or []:
        if str(ed.get("from") or "") != state_id:
            continue
        to_state = str(ed.get("to") or "")
        if not to_state:
            continue
        exec_body = ed.get("execute") if isinstance(ed.get("execute"), dict) else {}
        hints = [
            str(exec_body.get("target_tab") or ""),
            labels_map.get(to_state, ""),
            to_state.split(".")[-1],
        ]
        hints = [h for h in hints if h]
        for region in regions:
            label = str(region.get("label") or "")
            if not label or not region.get("clickable"):
                continue
            if any(h in label or label in h for h in hints):
                region["nav_to"] = to_state
    wf["regions"] = regions
    return wf


def _turn_context(turn: dict[str, Any]) -> dict[str, str]:
    pkg = infer_screen_package(
        turn.get("nodes") or [],
        target_package=str(turn.get("target_package") or ""),
    )
    if turn.get("foreground_package"):
        pkg["foreground_package"] = str(turn.get("foreground_package") or "")
    if turn.get("screen_kind"):
        pkg["screen_kind"] = str(turn.get("screen_kind") or "")
    return pkg


def _collapse_steps(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """连续相同屏态+包名只保留一段，附停留步数。"""
    out: list[dict[str, Any]] = []
    for step in steps:
        key = (
            str(step.get("localize_chosen") or step.get("state_id") or ""),
            str(step.get("foreground_package") or ""),
            str(step.get("screen_kind") or ""),
        )
        if out:
            prev = out[-1]
            prev_key = (
                str(prev.get("localize_chosen") or prev.get("state_id") or ""),
                str(prev.get("foreground_package") or ""),
                str(prev.get("screen_kind") or ""),
            )
            if key == prev_key and key[0]:
                prev["dwell_count"] = int(prev.get("dwell_count") or 1) + 1
                prev["turn_to"] = step.get("turn_id")
                continue
        row = dict(step)
        row["dwell_count"] = 1
        row["turn_from"] = step.get("turn_id")
        row["turn_to"] = step.get("turn_id")
        out.append(row)
    return out


def filter_app_turns(
    ordered: list[dict[str, Any]],
    *,
    scope: Any = None,
) -> list[dict[str, Any]]:
    """去掉系统桌面 / 权限弹窗 / 非本 app 包名或站点 的采集帧。"""
    return [t for t in (ordered or []) if not is_system_screen(t, scope=scope)]


def sync_on_new_capture(
    app_id: str,
    *,
    project_id: str = "",
    updated_by: str = "capture",
) -> dict[str, Any]:
    """每帧采集后增量合成并发布；失败不阻断跑批。"""
    if not str(app_id or "").strip():
        return {"ok": False, "reason": "app_id 为空"}
    try:
        row = get_live_graph(
            app_id,
            project_id=project_id,
            updated_by=updated_by,
            sync=True,
        )
        return {
            "ok": True,
            "synced": bool(row.get("synced")),
            "state_count": int(row.get("state_count") or 0),
            "source": str(row.get("source") or ""),
            "publish_error": str(row.get("publish_error") or ""),
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": str(exc)}


def enrich_ui_logic_edges(doc: dict[str, Any]) -> dict[str, Any]:
    """为边补充 UI 动作文案，供 Studio 逻辑图展示。"""
    out = dict(doc or {})
    labels = (out.get("meta") or {}).get("tab_bar", {}).get("labels") or {}
    edges = []
    for ed in out.get("edges") or []:
        row = dict(ed)
        eid = str(row.get("id") or "")
        exec_body = row.get("execute") if isinstance(row.get("execute"), dict) else {}
        steps = exec_body.get("steps") or []
        meta = row.get("meta") if isinstance(row.get("meta"), dict) else {}
        scroll = row.get("scroll_into_view") if isinstance(row.get("scroll_into_view"), dict) else {}
        action = "跳转"
        action_kind = "navigate"

        if eid.startswith("edge.tab."):
            dst = str(row.get("to") or "")
            tab = str(exec_body.get("target_tab") or labels.get(dst) or dst.split(".")[-1])
            action = f"点击 Tab · {tab}"
            action_kind = "tap_tab"
        elif meta.get("reason") == "tab_sub_enter":
            action = "进入子页"
            action_kind = "enter_sub"
        elif meta.get("reason") == "tab_sub_back":
            action = "返回上级"
            action_kind = "back"
        elif meta.get("reason") == "observed_reverse":
            action = "返回"
            action_kind = "back"
        elif scroll.get("direction"):
            d = str(scroll.get("direction") or "").lower()
            action = "下滑查找" if d in ("down", "bottom") else "上滑查找"
            action_kind = "scroll"
        elif steps:
            step = str(steps[0] or "")
            if step == "tap_element":
                tab = str(exec_body.get("target_tab") or "")
                action = f"点击 Tab · {tab}" if tab else "点击"
                action_kind = "tap"
            elif step == "swipe_direction":
                d = str(exec_body.get("direction") or scroll.get("direction") or "")
                if d in ("left", "right"):
                    action = f"{'右' if d == 'right' else '左'}滑"
                elif d in ("up", "down"):
                    action = f"{'下' if d == 'down' else '上'}滑"
                else:
                    action = "滑动"
                action_kind = "swipe"
            elif step == "swipe_element_to_element":
                action = "拖拽"
                action_kind = "swipe"
            else:
                action = step
        elif meta.get("observed"):
            cnt = int(meta.get("count") or 1)
            action = f"跳转 ×{cnt}" if cnt > 1 else "跳转"

        row["ui_action"] = action
        row["ui_action_kind"] = action_kind
        edges.append(row)
    out["edges"] = edges
    return out


def _tab_prefix(state_id: str) -> str:
    parts = str(state_id or "").split(".")
    if len(parts) >= 2 and parts[1].startswith("tab_"):
        return ".".join(parts[:2])
    return ""


def _guess_state_from_turn(
    turn: dict[str, Any],
    doc: dict[str, Any],
    *,
    tab_label: str = "",
) -> str:
    chosen = str((turn.get("localized") or {}).get("chosen") or "").strip()
    if chosen:
        return chosen
    labels = (doc.get("meta") or {}).get("tab_bar", {}).get("labels") or {}
    if tab_label:
        for eid, label in labels.items():
            if str(label) == str(tab_label):
                return str(eid)
    return ""


def _score_turn_for_tab(
    turn: dict[str, Any],
    tab_label: str,
    tab_labels: list[str],
    *,
    band_top: int,
) -> int:
    from mino_nexus.services.nav_synthesis import (
        _SELECTED_RID_RE,
        _bottom_tab_node_states,
        infer_selected_tab_label,
    )

    score = 0
    if infer_selected_tab_label(turn, tab_labels, band_top=band_top, fallback="", prev_tab="") == tab_label:
        score += 10000
    states = _bottom_tab_node_states(turn, tab_labels, band_top=band_top).get(tab_label)
    if states and (states[0] or states[1] or _SELECTED_RID_RE.search(states[3]) or _SELECTED_RID_RE.search(states[4])):
        score += 5000
    return score


def _index_capture_wireframes(
    ordered: list[dict[str, Any]],
    doc: dict[str, Any],
    *,
    app_id: str = "",
) -> dict[str, dict[str, Any]]:
    """按 localize / 结构 Tab 对齐索引线框样本（不用 hierarchy 全文子串匹配）。"""
    from mino_nexus.services.nav_synthesis import (
        _infer_tab_from_bottom_diff,
        assign_turn_tabs,
        extract_tab_bar_labels,
    )

    filtered = filter_app_turns(ordered)
    fresh_tabs = extract_tab_bar_labels(filtered)
    labels_map = (doc.get("meta") or {}).get("tab_bar", {}).get("labels") or {}
    tab_labels = fresh_tabs if len(fresh_tabs) >= 2 else list(labels_map.values())
    turn_tabs: list[str] = []
    band_top = 0
    if tab_labels and len(filtered) >= 1:
        from mino_nexus.services.nav_screen_layout import infer_content_bands
        from mino_nexus.services.nav_synthesis import _tab_bar_band

        bands = infer_content_bands((filtered[0].get("nodes") or []))
        y_tab = int(bands.get("content_bottom_px") or 0)
        band_top, _ = _tab_bar_band(filtered)
        home_tab = tab_labels[0]
        turn_tabs = assign_turn_tabs(
            filtered,
            tab_labels,
            y_tab=y_tab,
            exclude=set(tab_labels),
            home_tab=home_tab,
        )

    best: dict[str, tuple[int, dict[str, Any]]] = {}
    for i, turn in enumerate(filtered):
        tab_label = turn_tabs[i] if i < len(turn_tabs) else ""
        sid = _guess_state_from_turn(turn, doc, tab_label=tab_label)
        if not sid:
            continue
        wf = _turn_wireframe(turn, app_id=app_id)
        score = len(wf.get("regions") or []) * 1000 + int(turn.get("at") or 0)
        prev = best.get(sid)
        if not prev or score > prev[0]:
            best[sid] = (score, wf)

    if tab_labels and band_top > 0:
        for eid, label in labels_map.items():
            if eid in best:
                continue
            scored: list[tuple[int, dict[str, Any]]] = []
            for i, turn in enumerate(filtered):
                pts = _score_turn_for_tab(turn, str(label), tab_labels, band_top=band_top)
                if i > 0:
                    if _infer_tab_from_bottom_diff(filtered[i - 1], turn, tab_labels, band_top=band_top) == label:
                        pts += 3000
                if pts <= 0:
                    continue
                wf = _turn_wireframe(turn, app_id=app_id)
                scored.append((pts + len(wf.get("regions") or []), wf))
            if scored:
                scored.sort(key=lambda x: x[0], reverse=True)
                best[eid] = (scored[0][0], scored[0][1])
    return {sid: wf for sid, (_, wf) in best.items()}


def attach_state_wireframes(
    doc: dict[str, Any],
    ordered: list[dict[str, Any]],
    *,
    app_id: str = "",
) -> dict[str, Any]:
    """为每个 state 挂线框：优先精确匹配，其次父子 id。"""
    raw = _index_capture_wireframes(ordered, doc, app_id=app_id)
    state_ids = list(_state_ids(doc))
    out: dict[str, dict[str, Any]] = {}

    def pick_best(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
        if not candidates:
            return None
        scored = [(len(c.get("regions") or []), c) for c in candidates]
        return max(scored, key=lambda x: x[0])[1]

    empty = {"regions": [], "chrome": {}, "screen": {"w": 1080, "h": 1920}, "source": "empty"}

    for sid in state_ids:
        if sid in raw:
            out[sid] = _mark_nav_regions(sanitize_wireframe(raw[sid]), sid, doc)
            continue
        cands: list[dict[str, Any]] = []
        for key, wf in raw.items():
            if key == sid or key.startswith(f"{sid}.") or sid.startswith(f"{key}."):
                cands.append(wf)
        best = pick_best(cands)
        if best is None:
            child_wfs = [raw[k] for k in raw if k.startswith(f"{sid}.")]
            best = pick_best(child_wfs)
        wf = sanitize_wireframe(best if best is not None else dict(empty))
        out[sid] = _mark_nav_regions(wf, sid, doc)
    return out


_OVERLAY_HINTS = ("说点什么", "留下你的想法", "请输入", "输入", "评论", "发表")


def _overlay_label_from_turn(turn: dict[str, Any]) -> str:
    for node in turn.get("nodes") or []:
        for field in ("text", "content_desc", "hint"):
            val = str(node.get(field) or "").strip()
            if val and any(h in val for h in _OVERLAY_HINTS):
                return val[:32]
        cls = str(node.get("class") or "")
        if "EditText" in cls:
            val = str(node.get("text") or node.get("hint") or node.get("content_desc") or "").strip()
            if val:
                return val[:32]
    return ""


def augment_overlay_states(doc: dict[str, Any], ordered: list[dict[str, Any]]) -> dict[str, Any]:
    """输入框/评论弹层 → dialog 状态 + 采集先后跳转边。"""
    out = dict(doc or {})
    states = list(out.get("states") or [])
    edges = list(out.get("edges") or [])
    known = _state_ids(out)
    edge_seen = {str(ed.get("id") or "") for ed in edges}

    def slug_label(label: str) -> str:
        s = re.sub(r"\s+", "_", label.strip())[:20]
        s = re.sub(r"[^\w\u4e00-\u9fff]", "", s) or "overlay"
        return s

    prev_sid = ""
    for turn in filter_app_turns(ordered):
        base_sid = _guess_state_from_turn(turn, out) or str((turn.get("localized") or {}).get("chosen") or "").strip()
        overlay = _overlay_label_from_turn(turn)
        if overlay:
            sid = f"dialog.{slug_label(overlay)}"
            if sid not in known:
                states.append(
                    {
                        "id": sid,
                        "kind": "dialog",
                        "identify": {
                            "required": [{"signal": "text_landmarks", "any": [overlay], "none_of": []}],
                        },
                        "guards": {},
                        "meta": {"from_overlay": True},
                    }
                )
                known.add(sid)
            from_sid = prev_sid or base_sid
            if from_sid and from_sid != sid:
                eid = f"edge.trace.{from_sid.split('.')[-1]}_to_{sid.split('.')[-1]}"
                if eid not in edge_seen:
                    edges.append(
                        {
                            "id": eid,
                            "kind": "nav",
                            "from": from_sid,
                            "to": sid,
                            "guard": {},
                            "execute": {"steps": ["tap_element"]},
                            "effect_assert": {
                                "within_ms": 8000,
                                "require_any": [{"text_landmarks": [overlay]}],
                                "require_none": [],
                                "state_delta": {},
                            },
                            "meta": {"observed": True, "reason": "overlay_open"},
                        }
                    )
                    edge_seen.add(eid)
            prev_sid = sid
        elif base_sid:
            prev_sid = base_sid

    out["states"] = states
    out["edges"] = edges
    return out


def build_trajectory(ordered: list[dict[str, Any]], doc: dict[str, Any]) -> dict[str, Any]:
    """跨屏轨迹（仅 App 内）；架构页请用 FSM 逻辑图，不用本字段。"""
    ordered = filter_app_turns(ordered)
    known = _state_ids(doc)
    raw_steps: list[dict[str, Any]] = []
    transitions: Counter[tuple[str, str]] = Counter()
    prev_sid = ""

    for i, turn in enumerate(ordered or []):
        sid = str((turn.get("localized") or {}).get("chosen") or "").strip()
        wf = _turn_wireframe(turn)
        ctx = _turn_context(turn)
        raw_steps.append(
            {
                "index": i,
                "session_id": str(turn.get("session_id") or ""),
                "turn_id": int(turn.get("turn_id") or 0),
                "at": int(turn.get("at") or 0),
                "state_id": sid if sid in known else "",
                "localize_chosen": sid,
                "region_count": len(wf.get("regions") or []),
                "wireframe": wf,
                **ctx,
            }
        )
        if sid and prev_sid and sid != prev_sid:
            transitions[(prev_sid, sid)] += 1
        if sid:
            prev_sid = sid

    collapsed = _collapse_steps(raw_steps)
    traj_edges = [
        {
            "from": a,
            "to": b,
            "count": c,
            "id": f"edge.traj.{a.split('.')[-1]}_to_{b.split('.')[-1]}",
        }
        for (a, b), c in transitions.most_common(80)
        if a and b and a != b
    ]

    # 转移图节点：按屏态聚合（并行路线）
    node_hits: Counter[str] = Counter()
    for step in raw_steps:
        sid = str(step.get("localize_chosen") or "").strip()
        if sid:
            node_hits[sid] += 1
    flow_nodes = [
        {
            "state_id": sid,
            "visit_count": count,
            "sample": next(
                (s for s in reversed(raw_steps) if s.get("localize_chosen") == sid),
                {},
            ),
        }
        for sid, count in node_hits.most_common(40)
    ]

    return {
        "step_count": len(raw_steps),
        "raw_step_count": len(raw_steps),
        "collapsed_step_count": len(collapsed),
        "unique_states": len(node_hits),
        "steps": collapsed[-60:],
        "raw_steps": raw_steps[-30:],
        "edges": traj_edges,
        "flow_nodes": flow_nodes,
        "flow_edges": traj_edges,
    }


def get_live_graph(
    app_id: str,
    *,
    project_id: str = "",
    updated_by: str = "",
    sync: bool = True,
) -> dict[str, Any]:
    """合成最新图；sync=True 时尽力写入正式库（失败仍返回合成结果给 UI）。"""
    from mino_nexus.services.nav_target_scope import resolve_app_target_scope

    project_id = _resolve_project_id(app_id, project_id)
    scope = resolve_app_target_scope(app_id)
    ordered, cap_meta = capture.iter_cumulative_turns(app_id)
    app_turns = filter_app_turns(ordered, scope=scope)
    cap_meta = {
        **cap_meta,
        "turns_app": len(app_turns),
        "turns_system_skipped": max(0, len(ordered) - len(app_turns)),
        "target_scope": scope.as_dict() if scope else {},
    }
    synthesized = build_fsm_from_captures(app_id, project_id=project_id)
    source = "capture_synthesis" if synthesized else "published"

    if synthesized:
        synthesized = merge_localize_orphans(synthesized, ordered)
        doc = augment_overlay_states(synthesized, ordered)
    else:
        doc = store.read_raw(app_id) or {}
        if doc:
            doc = augment_nav_connectivity(doc, ordered)
            doc = augment_overlay_states(doc, ordered)

    trajectory = build_trajectory(ordered, doc)
    synced = False
    publish_error = ""

    if sync and synthesized:
        try:
            result = prepare_publish(app_id, promote=True, updated_by=updated_by)
            if result.get("published"):
                doc = merge_localize_orphans(dict(result["published"]), ordered)
                synced = True
                source = "capture_synthesis_synced"
            elif result.get("draft"):
                doc = merge_localize_orphans(dict(result["draft"]), ordered)
                publish_error = str(result.get("human_summary") or "")
        except Exception as exc:  # noqa: BLE001
            publish_error = str(exc)

    # sync 会替换 doc，线框必须在最后写入，否则前端收到空 regions
    doc = enrich_ui_logic_edges(doc)
    wireframes = attach_state_wireframes(doc, ordered, app_id=app_id)
    meta = dict(doc.get("meta") or {})
    meta["state_wireframes"] = wireframes
    doc["meta"] = meta

    return {
        "doc": doc,
        "trajectory": trajectory,
        "capture": cap_meta,
        "synced": synced,
        "source": source,
        "publish_error": publish_error,
        "updated_at": int(time.time()),
        "state_count": len(doc.get("states") or []),
        "edge_count": len(doc.get("edges") or []),
    }
