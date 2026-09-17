"""业务流逻辑块：从采集时序分段 + 边上流转驱动类型 + Studio 布局 hints。"""
from __future__ import annotations

import hashlib
import re
from typing import Any

DEFAULT_VERIFICATION = "verified"

# 与 MinoStudio navRelationGraph WF_W / 行高对齐
_LAYOUT_NODE_W = 260
_LAYOUT_NODE_H = 520
_LAYOUT_COL_STEP = 296
_LAYOUT_BLOCK_PAD_X = 20
_LAYOUT_BLOCK_PAD_Y = 40
_LAYOUT_BLOCK_GAP = 28
_LAYOUT_ORPHAN_GAP = 28
_LAYOUT_NAV_MAX_DX = 320
_LAYOUT_NAV_MAX_DY = 56
_LAYOUT_CANVAS_X0 = 48
_LAYOUT_CANVAS_Y0 = 48
# Studio 架构图 zoomToFit 前的虚拟画布中心（节点 bbox 质心对齐于此）
_LAYOUT_CANVAS_CENTER_X = 640
_LAYOUT_CANVAS_CENTER_Y = 420
# 游离页相对主簇的最大纵向偏移（避免 _separate_rects 把页推到画布远端）
_ORPHAN_MAX_BELOW_MAIN = 128.0
_ORPHAN_ROW_STEP = 88.0


def _pick_display_blocks(
    blocks: list[dict[str, Any]],
    *,
    max_count: int = 6,
    min_states: int = 2,
) -> list[dict[str, Any]]:
    """画布展示用：优先多步、高频业务流，避免 9 列挤到画布右侧。"""
    rows = [b for b in blocks if len(b.get("state_ids") or []) >= min_states]
    if not rows:
        rows = list(blocks)
    rows.sort(
        key=lambda b: (
            -int(b.get("nav_link_count") or b.get("forward_step_count") or 0),
            0 if str(b.get("block_origin") or "") == "nav_graph" else 1,
            -len(b.get("state_ids") or []),
            -int(b.get("session_hits") or 0),
            str(b.get("flow_block_id") or ""),
        )
    )
    return rows[: max(1, max_count)]


def infer_transition_driver(
    *,
    action_type: str,
    same_state: bool,
    reverse: bool = False,
) -> str:
    if same_state:
        return "auto"
    kind = str(action_type or "tap").strip().lower()
    if reverse or kind == "back":
        return "system"
    if kind in ("tap", "tab", "swipe", "input"):
        return "manual"
    return "unknown"


def _dedupe_consecutive_state_ids(state_ids: list[str]) -> list[str]:
    out: list[str] = []
    for sid in state_ids:
        s = str(sid or "").strip()
        if not s:
            continue
        if not out or out[-1] != s:
            out.append(s)
    return out


def _segment_key(state_ids: list[str]) -> str:
    cleaned = [s for s in state_ids if s]
    if len(cleaned) < 2:
        return ""
    return "|".join(cleaned)


def _block_id_from_states(state_ids: list[str]) -> str:
    key = _segment_key(state_ids)
    if not key:
        return ""
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:10]
    return f"fb.{digest}"


def _default_block_name(state_ids: list[str], cluster_meta: dict[str, dict[str, Any]]) -> str:
    if not state_ids:
        return "流程"
    first = state_ids[0]
    cm = cluster_meta.get(first) or {}
    dn = str(cm.get("display_name") or "").strip()
    if dn:
        return f"{dn}…" if len(state_ids) > 1 else dn
    short = first.split(".")[-1][:8]
    return f"流程 · {short}"


def _pair_meta_from_edges(
    edges: list[dict[str, Any]],
) -> dict[tuple[str, str], dict[str, Any]]:
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for ed in edges:
        if str(ed.get("kind") or "") != "nav":
            continue
        src = str(ed.get("from") or "")
        dst = str(ed.get("to") or "")
        if not src or not dst or src == dst:
            continue
        meta = dict(ed.get("meta") or {})
        pair = (src, dst)
        prev = out.get(pair)
        if prev is None or int(meta.get("count") or 0) >= int(prev.get("count") or 0):
            out[pair] = meta
    return out


def action_label_indicates_ui_back(label: str) -> bool:
    """页面内返回控件（含「左上角返回箭头」）按返回语义，非前进 tap。"""
    raw = str(label or "").strip()
    if not raw:
        return False
    if raw in ("返回", "点击 · 返回", "返回 · 返回"):
        return True
    low = raw.lower()
    if "back" in low and "feedback" not in low:
        return True
    tail = raw.split("·")[-1].strip() if "·" in raw else raw
    if "返回" not in raw and "返回" not in tail:
        return False
    if tail == "返回" or raw.endswith("返回"):
        return True
    for hint in ("箭头", "左上", "右上", "返回键", "nav", "icon"):
        if hint in raw or hint in tail:
            return True
    return False


def classify_nav_pair(
    src: str,
    dst: str,
    *,
    nav_action_types: dict[tuple[str, str], str],
    pair_meta: dict[tuple[str, str], dict[str, Any]] | None = None,
) -> str:
    """forward | back | tab | weak — 用于逻辑块组成与块内排序权重。"""
    meta = (pair_meta or {}).get((src, dst)) or {}
    label = str(meta.get("action_label") or "")
    if action_label_indicates_ui_back(label):
        return "back"
    action = str(meta.get("action_type") or nav_action_types.get((src, dst)) or "tap").lower()
    if bool(meta.get("reverse")) or action == "back":
        return "back"
    if action == "tab":
        return "tab"
    if action in ("tap", "multi_tap", "swipe", "swipe_up", "swipe_down", "input", "long_press_element"):
        return "forward"
    return "weak"


def _timeline_state_rank(
    timeline: list[dict[str, Any]],
    turn_cluster_ids: list[str],
) -> dict[str, int]:
    rank: dict[str, int] = {}
    for i, sid in enumerate(turn_cluster_ids):
        s = str(sid or "").strip()
        if s and s not in rank:
            rank[s] = i
    return rank


def layout_left_right_pair(
    src: str,
    dst: str,
    *,
    nav_action_types: dict[tuple[str, str], str],
    pair_meta: dict[tuple[str, str], dict[str, Any]] | None = None,
) -> tuple[str, str] | None:
    """架构图横轴：返回 ⇒ 返回到的页在左；其余跳转（含无操作/auto）⇒ 跳转前在左。"""
    kind = classify_nav_pair(src, dst, nav_action_types=nav_action_types, pair_meta=pair_meta)
    if kind == "tab":
        return None
    if kind == "back":
        return dst, src
    return src, dst


def _collect_semantic_layout_constraints(
    allowed: set[str],
    *,
    edges: list[dict[str, Any]],
    nav_action_types: dict[tuple[str, str], str],
    turn_cluster_ids: list[str] | None,
    pair_meta: dict[tuple[str, str], dict[str, Any]],
    include_timeline: bool = True,
) -> list[tuple[str, str, int]]:
    """(before_left, after_right, weight)。"""
    out: list[tuple[str, str, int]] = []
    seen: set[tuple[str, str, int]] = set()

    def _add(left: str, right: str, weight: int) -> None:
        if left not in allowed or right not in allowed or left == right:
            return
        key = (left, right, weight)
        if key in seen:
            return
        seen.add(key)
        out.append(key)

    for ed in edges:
        if str(ed.get("kind") or "") != "nav":
            continue
        src = str(ed.get("from") or "")
        dst = str(ed.get("to") or "")
        lr = layout_left_right_pair(src, dst, nav_action_types=nav_action_types, pair_meta=pair_meta)
        if not lr:
            continue
        kind = classify_nav_pair(src, dst, nav_action_types=nav_action_types, pair_meta=pair_meta)
        _add(lr[0], lr[1], 3 if kind == "back" else 2)

    if include_timeline and turn_cluster_ids:
        for i in range(len(turn_cluster_ids) - 1):
            a = str(turn_cluster_ids[i] or "").strip()
            b = str(turn_cluster_ids[i + 1] or "").strip()
            if not a or not b or a == b:
                continue
            lr = layout_left_right_pair(a, b, nav_action_types=nav_action_types, pair_meta=pair_meta)
            if lr:
                _add(lr[0], lr[1], 2)
    return out


def order_states_in_flow_block(
    state_ids: list[str] | set[str],
    *,
    edges: list[dict[str, Any]],
    nav_action_types: dict[tuple[str, str], str],
    timeline_rank: dict[str, int],
    turn_cluster_ids: list[str] | None = None,
    include_timeline_constraints: bool = True,
) -> list[str]:
    """块内从左到右：返回目标在左 > 跳转前在左（边 + 时间轴观测跳变）。"""
    sids = [str(s) for s in state_ids if str(s)]
    if len(sids) <= 1:
        return sids
    allowed = set(sids)
    pair_meta = _pair_meta_from_edges(edges)
    constraints = _collect_semantic_layout_constraints(
        allowed,
        edges=edges,
        nav_action_types=nav_action_types,
        turn_cluster_ids=turn_cluster_ids if include_timeline_constraints else None,
        pair_meta=pair_meta,
        include_timeline=include_timeline_constraints,
    )

    ordered = sorted(sids, key=lambda s: (timeline_rank.get(s, 10**9), s))

    def _apply_constraint(seq: list[str], before: str, after: str) -> list[str]:
        if before not in allowed or after not in allowed or before == after:
            return seq
        ib = seq.index(before)
        ia = seq.index(after)
        if ib < ia:
            return seq
        seq = list(seq)
        seq.remove(after)
        ib = seq.index(before)
        seq.insert(ib + 1, after)
        return seq

    by_weight: dict[int, list[tuple[str, str]]] = {3: [], 2: []}
    for left, right, w in constraints:
        by_weight.setdefault(w, []).append((left, right))
    seq = ordered
    for w in (3, 2):
        for left, right in by_weight.get(w) or []:
            seq = _apply_constraint(seq, left, right)
    return seq


def align_architecture_layout_x(
    layout: dict[str, dict[str, Any]],
    *,
    edges: list[dict[str, Any]],
    nav_action_types: dict[tuple[str, str], str],
    turn_cluster_ids: list[str],
) -> None:
    """全图节点 x：统一「跳转前 / 返回目标在左，跳转后 / 返回起点在右」。"""
    if not layout:
        return
    saved_y = {sid: int(pos.get("y") or 0) for sid, pos in layout.items()}
    fb = {sid: str(pos.get("flow_block_id") or "") for sid, pos in layout.items()}
    rank = _timeline_state_rank([], turn_cluster_ids) if turn_cluster_ids else {}
    ordered = order_states_in_flow_block(
        list(layout.keys()),
        edges=edges,
        nav_action_types=nav_action_types,
        timeline_rank=rank,
        turn_cluster_ids=turn_cluster_ids or None,
        include_timeline_constraints=True,
    )
    for i, sid in enumerate(ordered):
        if sid not in layout:
            continue
        layout[sid] = {
            **layout[sid],
            "x": int(_LAYOUT_CANVAS_X0 + i * _LAYOUT_COL_STEP),
            "y": saved_y.get(sid, int(layout[sid].get("y") or 0)),
            "flow_block_id": fb.get(sid, ""),
        }


def align_layout_y_for_nav_pairs(
    layout: dict[str, dict[str, Any]],
    *,
    edges: list[dict[str, Any]],
    nav_action_types: dict[tuple[str, str], str],
) -> None:
    """有跳转关系的页对齐同一行（y），便于读 forward/返回 横排。"""
    if not layout:
        return
    pair_meta = _pair_meta_from_edges(edges)
    for _ in range(8):
        moved = False
        for ed in edges:
            if str(ed.get("kind") or "") != "nav":
                continue
            src = str(ed.get("from") or "")
            dst = str(ed.get("to") or "")
            if src not in layout or dst not in layout or src == dst:
                continue
            kind = classify_nav_pair(src, dst, nav_action_types=nav_action_types, pair_meta=pair_meta)
            if kind == "tab":
                continue
            lr = layout_left_right_pair(src, dst, nav_action_types=nav_action_types, pair_meta=pair_meta)
            if not lr:
                continue
            left, right = lr
            y = min(int(layout[left]["y"]), int(layout[right]["y"]))
            if int(layout[left]["y"]) != y or int(layout[right]["y"]) != y:
                layout[left]["y"] = y
                layout[right]["y"] = y
                moved = True
        if not moved:
            break
    _resolve_layout_overlaps(layout, gap=float(_LAYOUT_ORPHAN_GAP))


def _nav_pairs_for_block_membership(
    edges: list[dict[str, Any]],
    nav_action_types: dict[tuple[str, str], str],
) -> list[tuple[str, str]]:
    """Atlas 上凡可跳转（含返回/合成返回边）均参与成块；Tab 切换视为换流，不连块。"""
    pair_meta = _pair_meta_from_edges(edges)
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for ed in edges:
        if str(ed.get("kind") or "") != "nav":
            continue
        src = str(ed.get("from") or "")
        dst = str(ed.get("to") or "")
        if not src or not dst or src == dst:
            continue
        kind = classify_nav_pair(src, dst, nav_action_types=nav_action_types, pair_meta=pair_meta)
        if kind == "tab":
            continue
        pair = (src, dst)
        if pair not in seen:
            seen.add(pair)
            out.append(pair)
    return out


def _greedy_nav_chains_in_component(
    comp: set[str],
    edges: list[dict[str, Any]],
    nav_action_types: dict[tuple[str, str], str],
    *,
    timeline_rank: dict[str, int],
    turn_cluster_ids: list[str] | None = None,
) -> list[list[str]]:
    """在连通分量内按边观测次数贪心拼链，不要求 session 内连续点进。"""
    pair_meta = _pair_meta_from_edges(edges)
    weighted: list[tuple[int, str, str]] = []
    for ed in edges:
        if str(ed.get("kind") or "") != "nav":
            continue
        src = str(ed.get("from") or "")
        dst = str(ed.get("to") or "")
        if src not in comp or dst not in comp or src == dst:
            continue
        kind = classify_nav_pair(src, dst, nav_action_types=nav_action_types, pair_meta=pair_meta)
        if kind == "tab":
            continue
        w = int((ed.get("meta") or {}).get("count") or 1)
        weighted.append((w, src, dst))
    weighted.sort(key=lambda t: (-t[0], t[1], t[2]))

    chains: list[list[str]] = []

    def _merge_endpoint(chain: list[str], node: str, *, at_head: bool) -> None:
        if node in chain:
            return
        if at_head:
            chain.insert(0, node)
        else:
            chain.append(node)

    for _w, u, v in weighted:
        placed = False
        for ch in chains:
            if ch[-1] == u:
                _merge_endpoint(ch, v, at_head=False)
                placed = True
                break
            if ch[0] == v:
                _merge_endpoint(ch, u, at_head=True)
                placed = True
                break
            if ch[-1] == v:
                _merge_endpoint(ch, u, at_head=False)
                placed = True
                break
            if ch[0] == u:
                _merge_endpoint(ch, v, at_head=True)
                placed = True
                break
        if not placed:
            seed = order_states_in_flow_block(
                [u, v],
                edges=edges,
                nav_action_types=nav_action_types,
                timeline_rank=timeline_rank,
                turn_cluster_ids=turn_cluster_ids,
            )
            chains.append(list(seed))

    # 共享节点的链合并
    merged = True
    while merged:
        merged = False
        for i in range(len(chains)):
            if i >= len(chains):
                break
            for j in range(i + 1, len(chains)):
                if j >= len(chains):
                    break
                a, b = chains[i], chains[j]
                if not a or not b:
                    continue
                if a[-1] == b[0]:
                    a.extend(b[1:])
                    chains.pop(j)
                    merged = True
                    break
                if b[-1] == a[0]:
                    b.extend(a[1:])
                    chains[i] = b
                    chains.pop(j)
                    merged = True
                    break
                if set(a) & set(b):
                    union = list(dict.fromkeys(a + b))
                    chains[i] = order_states_in_flow_block(
                        union,
                        edges=edges,
                        nav_action_types=nav_action_types,
                        timeline_rank=timeline_rank,
                        turn_cluster_ids=turn_cluster_ids,
                    )
                    chains.pop(j)
                    merged = True
                    break
            if merged:
                break

    out: list[list[str]] = []
    for ch in chains:
        ordered = order_states_in_flow_block(
            ch,
            edges=edges,
            nav_action_types=nav_action_types,
            timeline_rank=timeline_rank,
            turn_cluster_ids=turn_cluster_ids,
            include_timeline_constraints=False,
        )
        ordered = _dedupe_consecutive_state_ids(ordered)
        if len(ordered) >= 2:
            out.append(ordered)
    if not out and len(comp) >= 2:
        out.append(
            order_states_in_flow_block(
                comp,
                edges=edges,
                nav_action_types=nav_action_types,
                timeline_rank=timeline_rank,
                turn_cluster_ids=turn_cluster_ids,
            )
        )
    return out


def _forward_edges_from_atlas(
    edges: list[dict[str, Any]],
    nav_action_types: dict[tuple[str, str], str],
) -> list[tuple[str, str]]:
    pair_meta = _pair_meta_from_edges(edges)
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for ed in edges:
        if str(ed.get("kind") or "") != "nav":
            continue
        src = str(ed.get("from") or "")
        dst = str(ed.get("to") or "")
        if not src or not dst or src == dst:
            continue
        kind = classify_nav_pair(src, dst, nav_action_types=nav_action_types, pair_meta=pair_meta)
        if kind != "forward":
            continue
        pair = (src, dst)
        if pair not in seen:
            seen.add(pair)
            out.append(pair)
    return out


def _undirected_components(nodes: set[str], pairs: list[tuple[str, str]]) -> list[set[str]]:
    adj: dict[str, set[str]] = {n: set() for n in nodes}
    for a, b in pairs:
        if a in adj and b in adj:
            adj[a].add(b)
            adj[b].add(a)
    seen: set[str] = set()
    comps: list[set[str]] = []
    for start in nodes:
        if start in seen:
            continue
        stack = [start]
        comp: set[str] = set()
        while stack:
            n = stack.pop()
            if n in seen:
                continue
            seen.add(n)
            comp.add(n)
            for nb in adj.get(n, ()):
                if nb not in seen:
                    stack.append(nb)
        if comp:
            comps.append(comp)
    return comps


def _longest_forward_chain_in_segment(
    segment: list[str],
    nav_action_types: dict[tuple[str, str], str],
    pair_meta: dict[tuple[str, str], dict[str, Any]],
) -> list[str]:
    """session 段中仅保留前向跳转 spine，削弱返回键造成的来回拉长。"""
    if len(segment) < 2:
        return list(segment)
    best: list[str] = []
    cur: list[str] = []
    for i in range(len(segment) - 1):
        a, b = segment[i], segment[i + 1]
        kind = classify_nav_pair(a, b, nav_action_types=nav_action_types, pair_meta=pair_meta)
        if kind == "forward":
            if not cur:
                cur = [a]
            if cur[-1] != b:
                cur.append(b)
        else:
            if len(cur) > len(best):
                best = cur
            cur = []
    if len(cur) > len(best):
        best = cur
    return best if len(best) >= 2 else []


def build_flow_blocks_hybrid(
    *,
    edges: list[dict[str, Any]],
    timeline: list[dict[str, Any]],
    turn_cluster_ids: list[str],
    nav_action_types: dict[tuple[str, str], str],
    cluster_meta: dict[str, dict[str, Any]],
    atlas_state_ids: set[str],
) -> list[dict[str, Any]]:
    """组成：Atlas 可跳转边（贪心成链）> session 仅计 session_hits。"""
    pair_meta = _pair_meta_from_edges(edges)
    timeline_rank = _timeline_state_rank(timeline, turn_cluster_ids)
    merged: dict[str, dict[str, Any]] = {}

    nav_pairs = _nav_pairs_for_block_membership(edges, nav_action_types)
    nav_nodes = {s for pair in nav_pairs for s in pair} & atlas_state_ids
    for comp in _undirected_components(nav_nodes, nav_pairs):
        if len(comp) < 2:
            continue
        for ordered in _greedy_nav_chains_in_component(
            comp,
            edges,
            nav_action_types,
            timeline_rank=timeline_rank,
            turn_cluster_ids=turn_cluster_ids,
        ):
            bid = _block_id_from_states(ordered)
            if bid in merged:
                continue
            fwd_steps = sum(
                1
                for i in range(len(ordered) - 1)
                if classify_nav_pair(
                    ordered[i],
                    ordered[i + 1],
                    nav_action_types=nav_action_types,
                    pair_meta=pair_meta,
                )
                == "forward"
            )
            nav_links = sum(
                1
                for i in range(len(ordered) - 1)
                if (ordered[i], ordered[i + 1]) in pair_meta
                or (ordered[i + 1], ordered[i]) in pair_meta
            )
            merged[bid] = {
                "flow_block_id": bid,
                "display_name": _default_block_name(ordered, cluster_meta),
                "name_sources": [{"kind": "nav_graph", "confidence": 0.85}],
                "state_ids": ordered,
                "entry_state_id": ordered[0],
                "terminal_state_ids": [ordered[-1]],
                "intra_block_edge_ids": [],
                "verification_status": DEFAULT_VERIFICATION,
                "session_hits": 0,
                "block_origin": "nav_graph",
                "forward_step_count": fwd_steps,
                "nav_link_count": nav_links,
            }

    timeline_segments = segment_session_state_paths(timeline, turn_cluster_ids, nav_action_types)
    for path in timeline_segments:
        spine = _dedupe_consecutive_state_ids(path)
        if len(spine) < 2:
            continue
        matched = False
        spine_set = set(spine)
        for block in merged.values():
            block_set = set(block.get("state_ids") or [])
            if len(spine_set & block_set) >= 2:
                block["session_hits"] = int(block.get("session_hits") or 0) + 1
                matched = True
        if matched:
            continue
        # 仅当 nav 图完全未覆盖该段时才落 timeline 块
        if spine_set <= nav_nodes:
            continue
        ordered = order_states_in_flow_block(
            spine,
            edges=edges,
            nav_action_types=nav_action_types,
            timeline_rank=timeline_rank,
            turn_cluster_ids=turn_cluster_ids,
            include_timeline_constraints=False,
        )
        ordered = _dedupe_consecutive_state_ids(ordered)
        if len(ordered) < 2:
            continue
        bid = _block_id_from_states(ordered)
        if bid in merged:
            merged[bid]["session_hits"] = int(merged[bid].get("session_hits") or 0) + 1
            continue
        merged[bid] = {
            "flow_block_id": bid,
            "display_name": _default_block_name(ordered, cluster_meta),
            "name_sources": [{"kind": "capture_segment", "confidence": 0.35}],
            "state_ids": ordered,
            "entry_state_id": ordered[0],
            "terminal_state_ids": [ordered[-1]],
            "intra_block_edge_ids": [],
            "verification_status": DEFAULT_VERIFICATION,
            "session_hits": 1,
            "block_origin": "timeline",
            "forward_step_count": 0,
            "nav_link_count": 0,
        }

    # 同一 state 可能落在多块：重排每块内顺序；primary 选择交给 _state_primary_block
    blocks = list(merged.values())
    for block in blocks:
        sids = _dedupe_consecutive_state_ids(block.get("state_ids") or [])
        if len(sids) < 2:
            continue
        block["state_ids"] = order_states_in_flow_block(
            sids,
            edges=edges,
            nav_action_types=nav_action_types,
            timeline_rank=timeline_rank,
            turn_cluster_ids=turn_cluster_ids,
            include_timeline_constraints=False,
        )
        block["state_ids"] = _dedupe_consecutive_state_ids(block["state_ids"])
        if len(block["state_ids"]) < 2:
            continue
        block["entry_state_id"] = block["state_ids"][0]
        block["terminal_state_ids"] = [block["state_ids"][-1]]

    blocks = [b for b in blocks if len(b.get("state_ids") or []) >= 2]

    blocks.sort(
        key=lambda b: (
            -int(b.get("forward_step_count") or 0),
            0 if str(b.get("block_origin") or "") == "nav_graph" else 1,
            -int(b.get("session_hits") or 0),
            str(b.get("flow_block_id") or ""),
        )
    )
    return blocks


def segment_session_state_paths(
    timeline: list[dict[str, Any]],
    turn_cluster_ids: list[str],
    nav_action_types: dict[tuple[str, str], str],
) -> list[list[str]]:
    """单 session 内按 Tab 切换切分，保留有序 state 路径（去连续重复）。"""
    if not timeline or len(timeline) != len(turn_cluster_ids):
        return []

    by_session: dict[str, list[int]] = {}
    for i, row in enumerate(timeline):
        turn = row.get("turn") if isinstance(row.get("turn"), dict) else {}
        sess = str(turn.get("session_id") or "")
        if not sess:
            continue
        by_session.setdefault(sess, []).append(i)

    segments: list[list[str]] = []
    for indices in by_session.values():
        path: list[str] = []
        for pos, i in enumerate(indices):
            sid = str(turn_cluster_ids[i] or "").strip()
            if not sid:
                continue
            if pos > 0:
                prev_i = indices[pos - 1]
                prev_sid = str(turn_cluster_ids[prev_i] or "").strip()
                if prev_sid and prev_sid != sid:
                    pair = (prev_sid, sid)
                    if nav_action_types.get(pair) == "tab":
                        if len(path) >= 2:
                            segments.append(list(path))
                        path = [sid]
                        continue
            if not path or path[-1] != sid:
                path.append(sid)
        if len(path) >= 2:
            segments.append(path)
    return segments


def merge_segments_to_blocks(
    segments: list[list[str]],
    *,
    cluster_meta: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for path in segments:
        key = _segment_key(path)
        if not key:
            continue
        bid = _block_id_from_states(path)
        if bid in merged:
            merged[bid]["session_hits"] = int(merged[bid].get("session_hits") or 0) + 1
            continue
        merged[bid] = {
            "flow_block_id": bid,
            "display_name": _default_block_name(path, cluster_meta),
            "name_sources": [{"kind": "capture_segment", "confidence": 0.5}],
            "state_ids": list(path),
            "entry_state_id": path[0],
            "terminal_state_ids": [path[-1]],
            "intra_block_edge_ids": [],
            "verification_status": DEFAULT_VERIFICATION,
            "session_hits": 1,
        }
    blocks = list(merged.values())
    blocks.sort(key=lambda b: (-int(b.get("session_hits") or 0), str(b.get("flow_block_id") or "")))
    return blocks


def _state_primary_block(state_id: str, blocks: list[dict[str, Any]]) -> str:
    best = ""
    best_key: tuple[int, int, int, int] = (-1, -1, -1, -1)
    for block in blocks:
        sids = block.get("state_ids") or []
        if state_id not in sids:
            continue
        origin = str(block.get("block_origin") or "")
        nav_pri = 1 if origin == "nav_graph" else 0
        key = (
            nav_pri,
            int(block.get("nav_link_count") or block.get("forward_step_count") or 0),
            len(sids),
            int(block.get("session_hits") or 0),
        )
        if key > best_key:
            best_key = key
            best = str(block.get("flow_block_id") or "")
    return best


def annotate_edge_transitions(
    edges: list[dict[str, Any]],
    blocks: list[dict[str, Any]],
    nav_action_types: dict[tuple[str, str], str],
) -> None:
    block_by_id = {str(b.get("flow_block_id") or ""): b for b in blocks}
    for ed in edges:
        if str(ed.get("kind") or "") != "nav":
            continue
        src = str(ed.get("from") or "")
        dst = str(ed.get("to") or "")
        meta = dict(ed.get("meta") or {})
        same = bool(src and src == dst)
        action_type = str(meta.get("action_type") or nav_action_types.get((src, dst)) or "tap")
        driver = infer_transition_driver(
            action_type=action_type,
            same_state=same,
            reverse=bool(meta.get("reverse")),
        )
        flow_block_id = ""
        relation = "global"
        step_index = -1
        for block in blocks:
            sids = block.get("state_ids") or []
            bid = str(block.get("flow_block_id") or "")
            for i in range(len(sids) - 1):
                if sids[i] == src and sids[i + 1] == dst:
                    flow_block_id = bid
                    relation = "intra"
                    step_index = i
                    break
                if {sids[i], sids[i + 1]} == {src, dst}:
                    flow_block_id = bid
                    relation = "intra"
                    step_index = i
                    break
            if flow_block_id:
                break
        if not flow_block_id and action_type == "tab":
            relation = "weak"
        meta["transition"] = {
            "driver": driver,
            "flow_block_id": flow_block_id,
            "step_index": step_index,
            "relation": relation,
        }
        ed["meta"] = meta
        if flow_block_id and relation == "intra":
            block = block_by_id.get(flow_block_id)
            if block is not None:
                eid = str(ed.get("id") or "")
                ids = list(block.get("intra_block_edge_ids") or [])
                if eid and eid not in ids:
                    ids.append(eid)
                    block["intra_block_edge_ids"] = ids


def _rect_overlap(a: dict[str, float], b: dict[str, float], gap: float) -> bool:
    return not (
        a["x"] + a["w"] + gap <= b["x"]
        or b["x"] + b["w"] + gap <= a["x"]
        or a["y"] + a["h"] + gap <= b["y"]
        or b["y"] + b["h"] + gap <= a["y"]
    )


def _separate_rects(rects: list[dict[str, Any]], gap: float, *, max_iter: int = 32) -> None:
    """推开游离页与逻辑块的最小间距。"""
    blocks = [r for r in rects if r.get("kind") == "block"]
    orphans = [r for r in rects if r.get("kind") == "orphan"]
    for _ in range(max_iter):
        moved = False
        for o in orphans:
            for b in blocks:
                if not _rect_overlap(o, b, gap):
                    continue
                overlap_x = min(o["x"] + o["w"] + gap - b["x"], b["x"] + b["w"] + gap - o["x"])
                overlap_y = min(o["y"] + o["h"] + gap - b["y"], b["y"] + b["h"] + gap - o["y"])
                if overlap_x > 0 and (overlap_y <= 0 or overlap_x <= overlap_y):
                    o["x"] = float(o["x"]) + overlap_x + 4
                    moved = True
                elif overlap_y > 0:
                    push = min(overlap_y + 4, _ORPHAN_MAX_BELOW_MAIN)
                    o["y"] = float(o["y"]) + push
                    moved = True
        if not moved:
            break


def _nav_edge_pairs(edges: list[dict[str, Any]]) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for ed in edges:
        if str(ed.get("kind") or "") != "nav":
            continue
        src = str(ed.get("from") or "")
        dst = str(ed.get("to") or "")
        if src and dst and src != dst:
            pairs.append((src, dst))
    return pairs


def _main_nav_component(
    layout: dict[str, dict[str, Any]],
    edges: list[dict[str, Any]],
) -> set[str]:
    """无向 nav 连通分量中节点数最多的一块（布局锚点）。"""
    if not layout:
        return set()
    adj: dict[str, set[str]] = {sid: set() for sid in layout}
    for src, dst in _nav_edge_pairs(edges):
        if src not in layout or dst not in layout:
            continue
        adj[src].add(dst)
        adj[dst].add(src)
    seen: set[str] = set()
    best: set[str] = set()
    for start in layout:
        if start in seen:
            continue
        stack = [start]
        comp: set[str] = set()
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            comp.add(node)
            for nb in adj.get(node, ()):
                if nb not in seen:
                    stack.append(nb)
        if len(comp) > len(best):
            best = comp
    return best


def _component_node_bbox(
    layout: dict[str, dict[str, Any]],
    component: set[str],
) -> tuple[float, float, float, float] | None:
    xs: list[float] = []
    ys: list[float] = []
    for sid in component:
        pos = layout.get(sid)
        if not pos:
            continue
        xs.append(float(pos["x"]))
        ys.append(float(pos["y"]))
    if not xs:
        return None
    min_x = min(xs)
    min_y = min(ys)
    max_x = max(xs) + float(_LAYOUT_NODE_W)
    max_y = max(ys) + float(_LAYOUT_NODE_H)
    return min_x, min_y, max_x, max_y


def _center_layout_on_canvas(
    layout: dict[str, dict[str, Any]],
    regions: list[dict[str, Any]] | None = None,
    *,
    center_x: float | None = None,
    center_y: float | None = None,
    focus_sids: set[str] | None = None,
) -> None:
    """将 focus 子集 bbox 质心平移到虚拟画布中心（默认全量；架构图应对主簇而非离群 orphan）。"""
    if not layout:
        return
    keys = [k for k in (focus_sids or set(layout.keys())) if k in layout]
    if not keys:
        keys = list(layout.keys())
    cx = float(center_x if center_x is not None else _LAYOUT_CANVAS_CENTER_X)
    cy = float(center_y if center_y is not None else _LAYOUT_CANVAS_CENTER_Y)
    xs = [float(layout[k]["x"]) for k in keys]
    ys = [float(layout[k]["y"]) for k in keys]
    bbox_cx = (min(xs) + max(xs) + float(_LAYOUT_NODE_W)) / 2.0
    bbox_cy = (min(ys) + max(ys) + float(_LAYOUT_NODE_H)) / 2.0
    dx = int(round(cx - bbox_cx))
    dy = int(round(cy - bbox_cy))
    if dx == 0 and dy == 0:
        return
    for v in layout.values():
        v["x"] = int(v["x"]) + dx
        v["y"] = int(v["y"]) + dy
    if regions:
        for reg in regions:
            reg["x"] = int(reg.get("x") or 0) + dx
            reg["y"] = int(reg.get("y") or 0) + dy


def _snap_orphans_beside_main(
    layout: dict[str, dict[str, Any]],
    orphan_sids: list[str],
    edges: list[dict[str, Any]],
) -> None:
    """碰撞推开后，把游离页收回主 nav 簇右侧窄列，避免与主簇相距半个画布。"""
    if not orphan_sids:
        return
    main = _main_nav_component(layout, edges)
    if not main:
        main = {sid for sid, v in layout.items() if str(v.get("flow_block_id") or "")}
    if not main:
        main = set(layout.keys()) - set(orphan_sids)
    bbox = _component_node_bbox(layout, main)
    if not bbox:
        return
    _min_x, min_y, max_x, max_y = bbox
    base_x = int(max_x + _LAYOUT_ORPHAN_GAP)
    for i, sid in enumerate(orphan_sids):
        pos = layout.get(sid)
        if not pos:
            continue
        ox = int(pos.get("x") or base_x)
        oy = int(pos.get("y") or int(min_y))
        if ox > max_x + _LAYOUT_COL_STEP * 2.5:
            ox = base_x
        if oy > max_y + _ORPHAN_MAX_BELOW_MAIN or oy < int(min_y) - 32:
            oy = int(min_y + i * _ORPHAN_ROW_STEP)
        layout[sid] = {**pos, "x": ox, "y": oy, "flow_block_id": ""}


def _place_orphans_by_nav(
    layout: dict[str, dict[str, Any]],
    orphans: list[str],
    edges: list[dict[str, Any]],
    *,
    nav_action_types: dict[tuple[str, str], str],
    default_x: int,
    default_y: int,
) -> None:
    """游离页按跳转语义贴邻：跳转前/返回目标在左，跳转后在右。"""
    pair_meta = _pair_meta_from_edges(edges)
    component = _main_nav_component(layout, edges)
    comp_bbox = _component_node_bbox(layout, component) if component else None
    row = 0

    def _pos_from_edge(sid: str, src: str, dst: str) -> tuple[int, int] | None:
        if sid not in (src, dst):
            return None
        lr = layout_left_right_pair(src, dst, nav_action_types=nav_action_types, pair_meta=pair_meta)
        if not lr:
            return None
        left, right = lr
        if sid == right and left in layout:
            ax, ay = int(layout[left]["x"]), int(layout[left]["y"])
            return ax + _LAYOUT_COL_STEP, ay
        if sid == left and right in layout:
            ax, ay = int(layout[right]["x"]), int(layout[right]["y"])
            return ax - _LAYOUT_COL_STEP, ay
        if sid == right and src in layout and left == src:
            ax, ay = int(layout[src]["x"]), int(layout[src]["y"])
            return ax + _LAYOUT_COL_STEP, ay
        if sid == left and dst in layout and right == dst:
            ax, ay = int(layout[dst]["x"]), int(layout[dst]["y"])
            return ax - _LAYOUT_COL_STEP, ay
        return None

    for sid in orphans:
        placed: tuple[int, int] | None = None
        for ed in edges:
            if str(ed.get("kind") or "") != "nav":
                continue
            src = str(ed.get("from") or "")
            dst = str(ed.get("to") or "")
            if src not in layout and dst not in layout:
                continue
            placed = _pos_from_edge(sid, src, dst)
            if placed:
                break
        if placed:
            layout[sid] = {"x": int(placed[0]), "y": int(placed[1]), "flow_block_id": ""}
            continue
        if comp_bbox:
            _min_x, min_y, max_x, _max_y = comp_bbox
            layout[sid] = {
                "x": int(max_x + _LAYOUT_ORPHAN_GAP),
                "y": int(min_y + row * 72),
                "flow_block_id": "",
            }
            row += 1
        else:
            layout[sid] = {
                "x": int(default_x),
                "y": int(default_y + row * (_LAYOUT_NODE_H + 24)),
                "flow_block_id": "",
            }
            row += 1


def _resolve_layout_overlaps(layout: dict[str, dict[str, Any]], *, gap: float = 36.0) -> None:
    """节点 bbox 重叠时沿 x 推开（保持 y）。"""
    sids = list(layout.keys())
    w = float(_LAYOUT_NODE_W)
    h = float(_LAYOUT_NODE_H)
    for _ in range(12):
        moved = False
        for i, a in enumerate(sids):
            for b in sids[i + 1 :]:
                la, lb = layout[a], layout[b]
                ax, ay = float(la["x"]), float(la["y"])
                bx, by = float(lb["x"]), float(lb["y"])
                overlap_x = min(ax + w + gap - bx, bx + w + gap - ax)
                overlap_y = min(ay + h + gap - by, by + h + gap - ay)
                if overlap_x <= 0 or overlap_y <= 0:
                    continue
                if overlap_x <= overlap_y:
                    lb["x"] = int(bx + overlap_x + 4)
                    moved = True
                else:
                    lb["y"] = int(by + overlap_y + 4)
                    moved = True
        if not moved:
            break


def compact_layout_by_nav_edges(
    layout: dict[str, dict[str, Any]],
    edges: list[dict[str, Any]],
    *,
    max_dx: float | None = None,
    max_dy: float | None = None,
    iterations: int = 10,
) -> None:
    """仅拉近过大的纵向间距；不强行改 x，避免多目标页叠在同一列。"""
    if not layout:
        return
    lim_dy = float(max_dy if max_dy is not None else _LAYOUT_NAV_MAX_DY)
    pairs = _nav_edge_pairs(edges)
    if not pairs:
        return
    for _ in range(iterations):
        moved = False
        for src, dst in pairs:
            la = layout.get(src)
            lb = layout.get(dst)
            if not la or not lb:
                continue
            same_block = str(la.get("flow_block_id") or "") and la.get("flow_block_id") == lb.get(
                "flow_block_id"
            )
            ax, ay = float(la["x"]), float(la["y"])
            bx, by = float(lb["x"]), float(lb["y"])
            dy = by - ay
            if same_block and abs(float(lb["x"]) - float(la["x"])) >= _LAYOUT_COL_STEP * 0.85:
                continue
            if abs(dy) > lim_dy:
                lb["y"] = int(ay + (lim_dy if dy > 0 else -lim_dy))
                moved = True
        if not moved:
            break
    _resolve_layout_overlaps(layout, gap=float(_LAYOUT_ORPHAN_GAP))


def layout_states_for_flow_blocks(
    states: list[dict[str, Any]],
    blocks: list[dict[str, Any]],
    *,
    edges: list[dict[str, Any]] | None = None,
    nav_action_types: dict[tuple[str, str], str] | None = None,
    turn_cluster_ids: list[str] | None = None,
    fallback_grid: dict[str, dict[str, int]] | None = None,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """自由画布：块内横向排页；最终 x 按跳转/返回语义对齐。"""
    layout: dict[str, dict[str, Any]] = {}
    edge_list = edges if isinstance(edges, list) else []
    nav_types = nav_action_types if isinstance(nav_action_types, dict) else {}
    turn_ids = turn_cluster_ids if isinstance(turn_cluster_ids, list) else []
    regions: list[dict[str, Any]] = []
    state_ids_set = {str(s.get("id") or "") for s in states if str(s.get("id") or "")}
    display_blocks = _pick_display_blocks(blocks)
    in_display: set[str] = set()

    rects: list[dict[str, Any]] = []
    block_placements: list[tuple[dict[str, Any], float, float, float, float, list[str]]] = []

    row_x = [float(_LAYOUT_CANVAS_X0), float(_LAYOUT_CANVAS_X0)]
    row_y = [float(_LAYOUT_CANVAS_Y0), float(_LAYOUT_CANVAS_Y0)]
    row_track_h = [0.0, 0.0]
    placed_idx = 0
    max_block_right = float(_LAYOUT_CANVAS_X0)
    for block in display_blocks:
        sids = [str(s) for s in (block.get("state_ids") or []) if str(s) in state_ids_set]
        bid = str(block.get("flow_block_id") or "")
        if len(sids) < 2:
            continue
        inner_w = len(sids) * _LAYOUT_COL_STEP + _LAYOUT_BLOCK_PAD_X * 2
        inner_h = _LAYOUT_NODE_H + _LAYOUT_BLOCK_PAD_Y + 28
        row = 0 if row_x[0] <= row_x[1] else 1
        bx = row_x[row]
        by = row_y[row]
        row_x[row] = bx + inner_w + _LAYOUT_BLOCK_GAP
        row_track_h[row] = max(row_track_h[row], inner_h)
        max_block_right = max(max_block_right, bx + inner_w)
        if row_x[row] > _LAYOUT_CANVAS_X0 + 720:
            row_y[row] += row_track_h[row] + _LAYOUT_BLOCK_GAP
            row_x[row] = float(_LAYOUT_CANVAS_X0)
            row_track_h[row] = 0.0

        block_placements.append((block, bx, by, inner_w, inner_h, sids))
        placed_idx += 1
        rects.append(
            {
                "kind": "block",
                "id": bid,
                "x": bx - 16,
                "y": by - 36,
                "w": inner_w + 32,
                "h": inner_h + 40,
            }
        )

    for block, bx, by, inner_w, inner_h, sids in block_placements:
        bid = str(block.get("flow_block_id") or "")
        node_y = by + _LAYOUT_BLOCK_PAD_Y
        ordered_sids = [s for s in (block.get("state_ids") or []) if str(s) in sids]
        if len(ordered_sids) < len(sids):
            ordered_sids = sids
        for j, sid in enumerate(ordered_sids):
            layout[sid] = {
                "x": int(bx + _LAYOUT_BLOCK_PAD_X + j * _LAYOUT_COL_STEP),
                "y": int(node_y),
                "flow_block_id": bid,
            }
            in_display.add(sid)
        regions.append(
            {
                "flow_block_id": bid,
                "display_name": str(block.get("display_name") or bid),
                "x": int(bx - 16),
                "y": int(by - 36),
                "width": int(inner_w + 32),
                "height": int(inner_h + 40),
            }
        )

    orphans = [str(s.get("id") or "") for s in states if str(s.get("id") or "") not in in_display]
    orphan_x = int(max_block_right + _LAYOUT_ORPHAN_GAP)
    orphan_y = _LAYOUT_CANVAS_Y0
    _place_orphans_by_nav(
        layout,
        orphans,
        edge_list,
        nav_action_types=nav_types,
        default_x=orphan_x,
        default_y=orphan_y,
    )
    for sid in orphans:
        pos = layout.get(sid) or {}
        ox = int(pos.get("x") or orphan_x)
        oy = int(pos.get("y") or orphan_y)
        layout[sid] = {"x": ox, "y": oy, "flow_block_id": ""}
        rects.append(
            {
                "kind": "orphan",
                "id": sid,
                "x": float(ox - 8),
                "y": float(oy - 20),
                "w": float(_LAYOUT_NODE_W + 16),
                "h": float(_LAYOUT_NODE_H + 36),
            }
        )

    _separate_rects(rects, float(_LAYOUT_ORPHAN_GAP))

    orphan_rects = [r for r in rects if r.get("kind") == "orphan"]
    for r in orphan_rects:
        sid = str(r.get("id") or "")
        if sid in layout:
            layout[sid]["x"] = int(r["x"] + 8)
            layout[sid]["y"] = int(r["y"] + 20)

    _snap_orphans_beside_main(layout, orphans, edge_list)

    for r in orphan_rects:
        sid = str(r.get("id") or "")
        if sid in layout:
            r["x"] = float(layout[sid]["x"] - 8)
            r["y"] = float(layout[sid]["y"] - 20)

    block_rects = {str(r.get("id") or ""): r for r in rects if r.get("kind") == "block"}
    for reg in regions:
        bid = str(reg.get("flow_block_id") or "")
        br = block_rects.get(bid)
        if not br:
            continue
        reg["x"] = int(br["x"])
        reg["y"] = int(br["y"])
        reg["width"] = int(br["w"])
        reg["height"] = int(br["h"])

    bid_counts: dict[str, int] = {}
    for v in layout.values():
        bid = str(v.get("flow_block_id") or "")
        if bid:
            bid_counts[bid] = int(bid_counts.get(bid) or 0) + 1
    for sid, v in layout.items():
        bid = str(v.get("flow_block_id") or "")
        if bid and int(bid_counts.get(bid) or 0) < 2:
            layout[sid] = {**v, "flow_block_id": ""}

    if layout:
        min_x = min(int(v["x"]) for v in layout.values())
        min_y = min(int(v["y"]) for v in layout.values())
        for v in layout.values():
            v["x"] = int(v["x"]) - min_x + _LAYOUT_CANVAS_X0
            v["y"] = int(v["y"]) - min_y + _LAYOUT_CANVAS_Y0
        for reg in regions:
            reg["x"] = int(reg["x"]) - min_x + _LAYOUT_CANVAS_X0
            reg["y"] = int(reg["y"]) - min_y + _LAYOUT_CANVAS_Y0

    compact_layout_by_nav_edges(layout, edge_list)

    if turn_ids:
        align_architecture_layout_x(
            layout,
            edges=edge_list,
            nav_action_types=nav_types,
            turn_cluster_ids=turn_ids,
        )
        align_layout_y_for_nav_pairs(
            layout,
            edges=edge_list,
            nav_action_types=nav_types,
        )

    main_focus = _main_nav_component(layout, edge_list)
    if not main_focus:
        main_focus = {sid for sid, v in layout.items() if str(v.get("flow_block_id") or "")}
    focus = set(main_focus) | set(orphans)
    _center_layout_on_canvas(layout, regions, focus_sids=focus)

    return layout, regions


def build_flow_context(
    fsm: dict[str, Any] | None,
    *,
    state_id: str,
    localized: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    sid = str(state_id or "").strip()
    if not sid or not fsm:
        return None
    meta = fsm.get("meta") if isinstance(fsm.get("meta"), dict) else {}
    blocks = meta.get("flow_blocks") if isinstance(meta.get("flow_blocks"), list) else []
    if not blocks:
        return None
    for block in _pick_display_blocks(blocks):
        sids = [str(s) for s in (block.get("state_ids") or []) if str(s)]
        if sid not in sids:
            continue
        idx = sids.index(sid)
        terminals = [str(t) for t in (block.get("terminal_state_ids") or []) if str(t)]
        if not terminals:
            terminals = [sids[-1]] if sids else []
        on_terminal = sid in terminals
        loc = localized or {}
        tier = str(loc.get("evidence_tier") or "")
        ref_only = loc.get("band") == "explore" or tier == "low"
        phase = "done" if on_terminal else "in_progress"
        if sid == sids[0] and idx == 0 and not on_terminal:
            phase = "entry"
        hint = ""
        if on_terminal:
            hint = "本业务流已到终态，勿重复触发流内前置操作（如再次下载/再次获取验证码）。"
        elif idx + 1 < len(sids):
            hint = f"流内第 {idx + 1}/{len(sids)} 步，完成后应进入下一屏而非返回流外 Tab。"
        return {
            "flow_block_id": str(block.get("flow_block_id") or ""),
            "flow_display_name": str(block.get("display_name") or ""),
            "step_index": idx + 1,
            "step_total": len(sids),
            "current_state_id": sid,
            "terminal_states": terminals,
            "phase": phase,
            "progress_hint": hint,
            "reference_only": ref_only,
        }
    return None


def format_flow_context_block(ctx: dict[str, Any] | None) -> str:
    if not ctx:
        return ""
    ref = "（参考意见，非确证）" if ctx.get("reference_only") else ""
    name = str(ctx.get("flow_display_name") or "业务流")
    step = int(ctx.get("step_index") or 0)
    total = int(ctx.get("step_total") or 0)
    phase = str(ctx.get("phase") or "")
    lines = [
        f"【业务流】{name}{ref}：第 {step}/{total} 步（{phase}）",
    ]
    hint = str(ctx.get("progress_hint") or "").strip()
    if hint:
        lines.append(hint)
    return "\n".join(lines)


def enrich_atlas_doc(
    doc: dict[str, Any],
    *,
    timeline: list[dict[str, Any]],
    turn_cluster_ids: list[str],
    nav_action_types: dict[tuple[str, str], str],
    cluster_meta: dict[str, dict[str, Any]],
    fallback_layout: dict[str, dict[str, int]] | None = None,
) -> dict[str, Any]:
    """写入 flow_blocks、边 transition、按块布局。"""
    edges = doc.get("edges") if isinstance(doc.get("edges"), list) else []
    states = doc.get("states") if isinstance(doc.get("states"), list) else []
    atlas_state_ids = {str(s.get("id") or "") for s in states if str(s.get("id") or "")}
    blocks = build_flow_blocks_hybrid(
        edges=edges,
        timeline=timeline,
        turn_cluster_ids=turn_cluster_ids,
        nav_action_types=nav_action_types,
        cluster_meta=cluster_meta,
        atlas_state_ids=atlas_state_ids,
    )
    annotate_edge_transitions(edges, blocks, nav_action_types)
    doc["edges"] = edges
    meta = dict(doc.get("meta") or {})
    meta["flow_blocks"] = blocks
    meta["flow_block_display_count"] = len(_pick_display_blocks(blocks))
    if blocks:
        meta["atlas_layout"] = "flow_blocks"
        node_layout, block_regions = layout_states_for_flow_blocks(
            states,
            blocks,
            edges=edges,
            nav_action_types=nav_action_types,
            turn_cluster_ids=turn_cluster_ids,
            fallback_grid=None,
        )
        meta["studio_layout"] = {
            "states": node_layout,
            "layout_mode": "free_canvas",
            "flow_blocks": [
                {
                    "flow_block_id": b.get("flow_block_id"),
                    "display_name": b.get("display_name"),
                    "state_ids": b.get("state_ids"),
                }
                for b in blocks
            ],
            "flow_block_regions": block_regions,
        }
    doc["meta"] = meta
    return doc
