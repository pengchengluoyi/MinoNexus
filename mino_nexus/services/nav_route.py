"""NavFSM 路径规划：给定起止屏态，在 nav 边图上求最短路（运行时，不画恢复边）。"""
from __future__ import annotations

import copy
import re
from typing import Any

from mino_nexus.services import nav_fsm as F
from mino_nexus.services import nav_fsm_store as store
from mino_nexus.services.nav_execute import execute_target_page

_NAV_REF_QUOTE_RE = re.compile(r"[「『\"“]([^」』\"”]{1,24})[」』\"”]")
_NAV_REF_WRAP_RE = re.compile(
    r"^(?:点击|点一下|切换到|进入|打开)(?:底部|上面|中间)?"
)
_NAV_REF_TAB_TAIL_RE = re.compile(r"(?:底部)?(?:Tab|tab|TAB)$")


def click_label_from_nav_ref(ref: str) -> str:
    """从 from/to 口语里抽出可点文案（引号内或短标签），忽略 page.sk* 节点 id。"""
    raw = str(ref or "").strip()
    if not raw or raw.startswith("page.") or raw.startswith("tab_"):
        return ""
    quoted = _NAV_REF_QUOTE_RE.search(raw)
    if quoted:
        return str(quoted.group(1) or "").strip()
    cleaned = _NAV_REF_WRAP_RE.sub("", raw).strip()
    cleaned = _NAV_REF_TAB_TAIL_RE.sub("", cleaned).strip()
    if len(cleaned) >= 2 and cleaned[0] in "「『\"“" and cleaned[-1] in "」』\"”":
        cleaned = cleaned[1:-1].strip()
    if 1 <= len(cleaned) <= 16 and cleaned != raw:
        return cleaned
    if 1 <= len(raw) <= 16 and " " not in raw and "\n" not in raw:
        # 「…页/页面」是口语目标不是控件文案；拿去 tap 会锚点落空。
        if raw.endswith("页面") or raw.endswith("页"):
            return ""
        return raw
    return ""


def _ref_display_variants(ref: str) -> list[str]:
    """自然语言目标（如「我的页面」）的常见写法变体，用于 Tab 直点。"""
    val = str(ref or "").strip()
    if not val:
        return []
    out: list[str] = [val]
    if val.endswith("页面") and len(val) > 2:
        out.append(val[:-2])
    if val.endswith("页") and len(val) > 1 and not val.endswith("页面"):
        out.append(val[:-1])
    dedup: list[str] = []
    seen: set[str] = set()
    for item in out:
        key = item.strip()
        if key and key not in seen:
            seen.add(key)
            dedup.append(key)
    return dedup


def resolve_state_ref(fsm: dict[str, Any], ref: str) -> str:
    """把 state_id / Tab 文案 / 短 id 解析成图里的 state_id。"""
    val = str(ref or "").strip()
    if not val or not fsm:
        return ""
    if F.state_by_id(fsm, val):
        return val
    for variant in _ref_display_variants(val):
        if variant != val and F.state_by_id(fsm, variant):
            return variant
    meta = fsm.get("meta") if isinstance(fsm.get("meta"), dict) else {}
    tab_bar = meta.get("tab_bar") if isinstance(meta.get("tab_bar"), dict) else {}
    labels = tab_bar.get("labels") if isinstance(tab_bar.get("labels"), dict) else {}
    for sid, label in labels.items():
        lab = str(label or "").strip()
        if lab == val or lab in _ref_display_variants(val):
            return str(sid)
    # 不按 entries 的 id 子串匹配：`page.tab_我的` 这种把文案编进 id 的上一代节点
    # 会凭「我的 in page.tab_我的」抢在展示名之前命中。
    #
    # 分层扫：id 后缀 → display_name → 别名 → identify。别名是采集来的噪声池
    # （一个页面常带十几条），若与 display_name 同层按 states 顺序先到先得，
    # 「我的发布」会被另一页混进来的同名别名截走。
    variants = _ref_display_variants(val)
    states = [st for st in fsm.get("states") or [] if str((st or {}).get("id") or "").strip()]

    def _state_meta(st: dict[str, Any]) -> dict[str, Any]:
        sm = st.get("meta")
        return sm if isinstance(sm, dict) else {}

    for st in states:
        sid = str(st.get("id") or "").strip()
        if sid.endswith(f".{val}") or sid.split(".")[-1] == val:
            return sid
    for st in states:
        dn = str(_state_meta(st).get("display_name") or "").strip()
        if dn and (dn == val or dn in variants):
            return str(st.get("id") or "").strip()
    alias_hits: list[str] = []
    for st in states:
        sid = str(st.get("id") or "").strip()
        for alias in _state_meta(st).get("aliases") or []:
            al = str(alias or "").strip()
            if al == val or al in variants:
                if sid not in alias_hits:
                    alias_hits.append(sid)
                break
    if len(alias_hits) == 1:
        return alias_hits[0]
    # 多页共享同一别名（常见：入口按钮文案同时出现在出发页和到达页）时不要先到先得。
    for st in states:
        identify = st.get("identify") or {}
        required = identify.get("required")
        blocks = required if isinstance(required, list) else ([required] if required else [])
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("signal") == "tab_bar":
                tab = str((block.get("match") or {}).get("selected") or "").strip()
                if tab == val or tab in variants:
                    return str(st.get("id") or "").strip()
    return val


def plan_route(
    fsm: dict[str, Any],
    *,
    from_state: str,
    to_state: str,
) -> dict[str, Any]:
    """返回最短路步骤（仅 kind=nav 的边）。"""
    src = resolve_state_ref(fsm, from_state)
    dst = resolve_state_ref(fsm, to_state)
    if not src or not dst:
        return {
            "ok": False,
            "error": "from_state / to_state 不能为空",
            "from_state": src,
            "to_state": dst,
            "steps": [],
            "edge_ids": [],
        }
    if src == dst:
        return {
            "ok": True,
            "from_state": src,
            "to_state": dst,
            "steps": [],
            "edge_ids": [],
            "hop_count": 0,
            "summary": "已在目标屏",
        }
    path = F.shortest_nav_path(fsm, src, dst)
    if not path:
        incoming = [
            str(e.get("from") or "")
            for e in F.nav_edges(fsm)
            if str(e.get("to") or "") == dst
        ]
        hint = ""
        if not incoming:
            hint = "目标页在图中无入边，需跑一条「进入该页」的用例补采集。"
        elif src not in {str(e.get("from") or "") for e in F.nav_edges(fsm)}:
            hint = "当前页无出边，可能 localize 未命中或图未连通。"
        else:
            hint = "两页分属不同连通分量，需补采中间跳转。"
        return {
            "ok": False,
            "error": f"路线图无路径：{src} → {dst}。{hint}",
            "from_state": src,
            "to_state": dst,
            "steps": [],
            "edge_ids": [],
            "hint": hint,
            "target_incoming": incoming[:8],
        }
    steps = []
    for ed in path:
        steps.append(
            {
                "edge_id": str(ed.get("id") or ""),
                "from": str(ed.get("from") or ""),
                "to": str(ed.get("to") or ""),
                "execute": dict(ed.get("execute") or {}),
                "effect_assert": dict(ed.get("effect_assert") or {}),
                "meta": dict(ed.get("meta") or {}),
            }
        )
    ids = [s["edge_id"] for s in steps if s["edge_id"]]
    return {
        "ok": True,
        "from_state": src,
        "to_state": dst,
        "steps": steps,
        "edge_ids": ids,
        "hop_count": len(steps),
        "summary": " → ".join(ids) if ids else "",
    }


def _state_ids(doc: dict[str, Any] | None) -> set[str]:
    out: set[str] = set()
    for st in (doc or {}).get("states") or []:
        if not isinstance(st, dict):
            continue
        sid = str(st.get("id") or st.get("state_id") or "").strip()
        if sid:
            out.add(sid)
    return out


def _wireframes_of(doc: dict[str, Any] | None) -> dict[str, Any]:
    meta = (doc or {}).get("meta") if isinstance((doc or {}).get("meta"), dict) else {}
    wfs = meta.get("state_wireframes") if isinstance(meta, dict) else None
    return dict(wfs) if isinstance(wfs, dict) else {}


def _has_sk_states(doc: dict[str, Any] | None) -> bool:
    return any(sid.startswith("page.sk") for sid in _state_ids(doc))


def _adopt_atlas_tab_bar(out: dict[str, Any], atlas: dict[str, Any]) -> None:
    """Tab 身份字段（home / launch / slots）跟着骨骼那一代走。"""
    ids = _state_ids(out)
    meta = dict(out.get("meta") or {})
    bar = dict(meta.get("tab_bar") or {})
    atlas_meta = atlas.get("meta") if isinstance(atlas.get("meta"), dict) else {}
    atlas_bar = atlas_meta.get("tab_bar") if isinstance(atlas_meta.get("tab_bar"), dict) else {}
    for key in ("home_state_id", "launch_state_id"):
        cand = str(atlas_bar.get(key) or "").strip()
        if cand and cand in ids:
            bar[key] = cand
    if not bar.get("slots") and atlas_bar.get("slots"):
        bar["slots"] = copy.deepcopy(atlas_bar["slots"])
    meta["tab_bar"] = bar
    out["meta"] = meta


def prune_superseded_tab_states(doc: dict[str, Any] | None) -> dict[str, Any] | None:
    """图上已有骨骼节点时，丢掉上一代 `page.tab_*` 占位节点及其边。

    两代节点之间没有任何一条边。留着的后果不是多几个孤岛，而是 `resolve_state_ref`
    优先命中旧节点（`tab_bar.labels` 里还挂着它），`shortest_nav_path` 于是恒空 ——
    `fsm_navigate` 每轮都掉进兜底，只会 BACK。
    """
    if not isinstance(doc, dict):
        return doc
    ids = _state_ids(doc)
    legacy = {sid for sid in ids if sid.startswith("page.tab_")}
    if not legacy or not any(sid.startswith("page.sk") for sid in ids):
        return doc
    out = copy.deepcopy(doc)
    out["states"] = [
        st
        for st in out.get("states") or []
        if str((st or {}).get("id") or (st or {}).get("state_id") or "").strip() not in legacy
    ]
    kept_edges: list[dict[str, Any]] = []
    for ed in out.get("edges") or []:
        if not isinstance(ed, dict):
            continue
        frm = str(ed.get("from") or ed.get("from_state") or "").strip()
        to = str(ed.get("to") or ed.get("to_state") or "").strip()
        if frm in legacy or to in legacy:
            continue
        kept_edges.append(ed)
    out["edges"] = kept_edges
    meta = dict(out.get("meta") or {})
    wfs = meta.get("state_wireframes")
    if isinstance(wfs, dict):
        meta["state_wireframes"] = {k: v for k, v in wfs.items() if str(k).strip() not in legacy}
    bar = meta.get("tab_bar")
    if isinstance(bar, dict):
        bar = dict(bar)
        bar["entries"] = [e for e in bar.get("entries") or [] if str(e).strip() not in legacy]
        labels = bar.get("labels")
        if isinstance(labels, dict):
            bar["labels"] = {k: v for k, v in labels.items() if str(k).strip() not in legacy}
        for key in ("home_state_id", "launch_state_id"):
            if str(bar.get(key) or "").strip() in legacy:
                bar.pop(key, None)
        meta["tab_bar"] = bar
    recover = meta.get("recover")
    if isinstance(recover, dict):
        recover = dict(recover)
        for key in ("default_state_id", "default_goal_state_id"):
            if str(recover.get(key) or "").strip() in legacy:
                recover.pop(key, None)
        meta["recover"] = recover
    out["meta"] = meta
    return out


def overlay_atlas_for_runtime(
    base: dict[str, Any] | None,
    atlas: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """把草稿 Atlas 的骨骼页 / wireframe / nav 边叠到已发布 Tab 图上。

    跑批读 `v1` 时常常只有 `page.tab_*` + tab_bar 文案；真正能辨页的
    `page.sk*` 与 `state_wireframes` 还停在 draft。不叠的话 localize 选不出当前页。
    叠完只留骨骼那一代节点（`prune_superseded_tab_states`）。
    """
    if not isinstance(base, dict):
        return prune_superseded_tab_states(atlas) if isinstance(atlas, dict) else base
    if not isinstance(atlas, dict):
        return prune_superseded_tab_states(base)
    atlas_wf = _wireframes_of(atlas)
    if not atlas_wf and not _has_sk_states(atlas):
        return prune_superseded_tab_states(base)
    if _has_sk_states(base) and _wireframes_of(base):
        return prune_superseded_tab_states(base)
    out = copy.deepcopy(base)
    meta = dict(out.get("meta") or {})
    base_wf = _wireframes_of(out)
    if atlas_wf:
        merged = dict(atlas_wf)
        merged.update(base_wf)
        meta["state_wireframes"] = merged
        out["meta"] = meta
    ids = _state_ids(out)
    extra_states: list[dict[str, Any]] = []
    for st in atlas.get("states") or []:
        if not isinstance(st, dict):
            continue
        sid = str(st.get("id") or st.get("state_id") or "").strip()
        if sid and sid not in ids:
            extra_states.append(copy.deepcopy(st))
            ids.add(sid)
    if extra_states:
        out["states"] = list(out.get("states") or []) + extra_states
    extra_edges: list[dict[str, Any]] = []
    seen = {
        (
            str(e.get("id") or e.get("edge_id") or ""),
            str(e.get("from") or e.get("from_state") or ""),
            str(e.get("to") or e.get("to_state") or ""),
        )
        for e in (out.get("edges") or [])
        if isinstance(e, dict)
    }
    for ed in atlas.get("edges") or []:
        if not isinstance(ed, dict):
            continue
        if str(ed.get("kind") or "nav") != "nav":
            continue
        frm = str(ed.get("from") or ed.get("from_state") or "").strip()
        to = str(ed.get("to") or ed.get("to_state") or "").strip()
        if not frm or not to or frm not in ids or to not in ids:
            continue
        key = (str(ed.get("id") or ed.get("edge_id") or ""), frm, to)
        if key in seen:
            continue
        extra_edges.append(copy.deepcopy(ed))
        seen.add(key)
    if extra_edges:
        out["edges"] = list(out.get("edges") or []) + extra_edges
    if _has_sk_states(atlas):
        _adopt_atlas_tab_bar(out, atlas)
    return prune_superseded_tab_states(out)


def load_fsm_doc(
    app_id: str,
    *,
    version: str = store.DEFAULT_VERSION,
    use_live: bool = True,
    project_id: str = "",
    app_version: str = "",
    nav_view_id: str = "",
) -> tuple[dict[str, Any] | None, str]:
    """读导航图。

    `use_live=True` 会现场 `get_live_graph` / `build_atlas`，只给导航页/编译器用。
    跑批必须 `use_live=False`，只用 `nav_fsm*` 里已有的数据。
    """
    doc: dict[str, Any] | None = None
    source = "published"
    if use_live:
        try:
            from mino_nexus.services.nav_live_graph import get_live_graph

            live = get_live_graph(app_id, project_id=project_id, sync=False)
            doc = live.get("doc") if isinstance(live.get("doc"), dict) else None
            if doc:
                source = str(live.get("source") or "live")
        except Exception:
            doc = None
    if not doc:
        requested = store.read_raw(app_id, version=version)
        draft = store.read_raw(app_id, version=store.DRAFT_VERSION)
        if (
            requested
            and str(version or store.DEFAULT_VERSION) != store.DRAFT_VERSION
            and draft
            and draft is not requested
        ):
            doc = overlay_atlas_for_runtime(requested, draft)
            source = "published+atlas" if doc is not requested else "published"
        else:
            doc = requested or draft
            source = "published" if requested else ("draft" if draft else "published")
    if doc and (app_version or nav_view_id):
        from mino_nexus.services.nav_version_views import materialize_fsm_for_runtime

        doc = materialize_fsm_for_runtime(
            doc,
            app_version=app_version,
            nav_view_id=nav_view_id,
            include_pending=False,
        )
    return doc, source


_PLACEHOLDER_TAB_LABELS = frozenset({"", "icon", "ImageView", "AppCompatImageView", "图标 Tab", "slot"})


def _fold_label(text: str) -> str:
    return re.sub(r"[\s_·\-]+", "", str(text or "").strip().lower())


def tab_slot_labels(fsm: dict[str, Any]) -> list[str]:
    """架构图底栏槽位上的真实文案（跳过纯图标占位）。"""
    meta = fsm.get("meta") if isinstance(fsm.get("meta"), dict) else {}
    tab_bar = meta.get("tab_bar") if isinstance(meta.get("tab_bar"), dict) else {}
    out: list[str] = []
    seen: set[str] = set()
    for slot in tab_bar.get("slots") or []:
        if not isinstance(slot, dict):
            continue
        label = str(slot.get("label") or slot.get("display") or "").strip()
        if label in _PLACEHOLDER_TAB_LABELS:
            continue
        key = _fold_label(label)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(label)
    labels = tab_bar.get("labels") if isinstance(tab_bar.get("labels"), dict) else {}
    for lab in labels.values():
        val = str(lab or "").strip()
        key = _fold_label(val)
        if not val or val in _PLACEHOLDER_TAB_LABELS or key in seen:
            continue
        seen.add(key)
        out.append(val)
    home = str(tab_bar.get("home_tab_label") or "").strip()
    if home and home not in _PLACEHOLDER_TAB_LABELS and _fold_label(home) not in seen:
        out.append(home)
    return out


def tab_root_label_for_state(fsm: dict[str, Any], state_id: str) -> str:
    """该节点是否为某个底栏 Tab 的根态；是则返回槽位文案。精确匹配，不用包含。"""
    sid = str(state_id or "").strip()
    if not sid or not fsm:
        return ""
    slots = tab_slot_labels(fsm)
    if not slots:
        return ""
    slot_by_fold = {_fold_label(s): s for s in slots}
    meta = fsm.get("meta") if isinstance(fsm.get("meta"), dict) else {}
    tab_bar = meta.get("tab_bar") if isinstance(meta.get("tab_bar"), dict) else {}
    labels = tab_bar.get("labels") if isinstance(tab_bar.get("labels"), dict) else {}
    mapped = str(labels.get(sid) or "").strip()
    if mapped and _fold_label(mapped) in slot_by_fold:
        return slot_by_fold[_fold_label(mapped)]
    if sid == str(tab_bar.get("home_state_id") or "").strip():
        home = str(tab_bar.get("home_tab_label") or "").strip()
        if home and _fold_label(home) in slot_by_fold:
            return slot_by_fold[_fold_label(home)]
    st = F.state_by_id(fsm, sid)
    st_meta = st.get("meta") if isinstance(st, dict) else {}
    names: list[str] = []
    if isinstance(st_meta, dict):
        names.append(str(st_meta.get("display_name") or "").strip())
        names.append(str(st_meta.get("tab") or "").strip())
        for alias in st_meta.get("aliases") or []:
            names.append(str(alias or "").strip())
    identify = (st or {}).get("identify") or {}
    required = identify.get("required")
    blocks = required if isinstance(required, list) else ([required] if required else [])
    for block in blocks:
        if isinstance(block, dict) and block.get("signal") == "tab_bar":
            names.append(str((block.get("match") or {}).get("selected") or "").strip())
    for name in names:
        key = _fold_label(name)
        if key and key in slot_by_fold:
            return slot_by_fold[key]
    return ""


def tab_label_for_state(fsm: dict[str, Any], state_id: str) -> str:
    """目标屏对应的底栏 Tab 文案（用于直接 tap_element）。"""
    sid = resolve_state_ref(fsm, state_id)
    if not sid:
        return ""
    slot = tab_root_label_for_state(fsm, sid)
    if slot:
        return slot
    meta = fsm.get("meta") if isinstance(fsm.get("meta"), dict) else {}
    tab_bar = meta.get("tab_bar") if isinstance(meta.get("tab_bar"), dict) else {}
    labels = tab_bar.get("labels") if isinstance(tab_bar.get("labels"), dict) else {}
    if sid in labels:
        return str(labels[sid] or "").strip()
    st = F.state_by_id(fsm, sid)
    if not st:
        return ""
    identify = st.get("identify") or {}
    required = identify.get("required")
    blocks = required if isinstance(required, list) else ([required] if required else [])
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("signal") == "tab_bar":
            return str((block.get("match") or {}).get("selected") or "").strip()
    return ""


def _edge_step_meta(fsm: dict[str, Any], edge_step: dict[str, Any]) -> dict[str, Any]:
    meta = edge_step.get("meta") if isinstance(edge_step.get("meta"), dict) else {}
    if meta:
        return dict(meta)
    eid = str(edge_step.get("edge_id") or "").strip()
    if eid:
        full = F.edge_by_id(fsm, eid)
        if isinstance(full, dict):
            em = full.get("meta")
            if isinstance(em, dict):
                return dict(em)
    return {}


def is_tab_shell_state(fsm: dict[str, Any], state_id: str) -> bool:
    """当前 state 是否为底栏 Tab 根态（entries / labels / 槽位文案对齐的壳页）。"""
    sid = str(state_id or "").strip()
    if not sid or not fsm:
        return False
    meta = fsm.get("meta") if isinstance(fsm.get("meta"), dict) else {}
    tab_bar = meta.get("tab_bar") if isinstance(meta.get("tab_bar"), dict) else {}
    entries = tab_bar.get("entries") or []
    if sid in {str(e).strip() for e in entries if str(e).strip()}:
        return True
    labels = tab_bar.get("labels") if isinstance(tab_bar.get("labels"), dict) else {}
    if sid in labels:
        return True
    if sid == str(tab_bar.get("home_state_id") or "").strip() and tab_slot_labels(fsm):
        return True
    return bool(tab_root_label_for_state(fsm, sid))


def pick_fsm_first_step(
    fsm: dict[str, Any],
    plan: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """fsm_navigate 本步应执行的边（Tab 根上多 hop 且终端为 Tab 时直跳最后一跳）。"""
    steps = [s for s in (plan.get("steps") or []) if isinstance(s, dict)]
    meta: dict[str, Any] = {"planned_hops": len(steps), "step_pick": "shortest_first"}
    if not steps:
        return {}, meta
    src = str(plan.get("from_state") or "").strip()
    dst = str(plan.get("to_state") or "").strip()
    first = steps[0]
    if len(steps) >= 2 and is_tab_shell_state(fsm, src):
        terminal = steps[-1]
        term_to = str(terminal.get("to") or "").strip()
        if term_to == dst and not edge_is_system_back(fsm, terminal):
            meta["step_pick"] = "tab_shell_terminal"
            return terminal, meta
    if len(steps) >= 2 and not is_tab_shell_state(fsm, src):
        terminal = steps[-1]
        term_to = str(terminal.get("to") or "").strip()
        tab_target = is_tab_shell_state(fsm, dst)
        if (
            term_to == dst
            and tab_target
            and not edge_is_system_back(fsm, terminal)
            and not edge_is_system_back(fsm, first)
        ):
            meta["step_pick"] = "deep_page_direct_tab"
            return terminal, meta
    return first, meta


def edge_is_system_back(fsm: dict[str, Any], edge_step: dict[str, Any]) -> bool:
    """Atlas 观测的返回边：系统 BACK 比文案 tap 更稳（图标返回键常无「返回」文本节点）。"""
    meta = _edge_step_meta(fsm, edge_step)
    action_type = str(meta.get("action_type") or "").strip().lower()
    if action_type == "back" or meta.get("reverse"):
        return True
    exe = edge_step.get("execute") if isinstance(edge_step.get("execute"), dict) else {}
    steps = exe.get("steps")
    if isinstance(steps, list) and "press_key" in [str(s) for s in steps]:
        return True
    return str(exe.get("key") or "").strip().upper() in ("BACK", "ESCAPE")


def dispatch_spec_for_edge(fsm: dict[str, Any], edge_step: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """fsm_navigate 本步应派发的 capability 与参数。"""
    if edge_is_system_back(fsm, edge_step):
        exe = edge_step.get("execute") if isinstance(edge_step.get("execute"), dict) else {}
        key = str(exe.get("key") or "BACK").strip().upper() or "BACK"
        return "press_key", {"key": key}
    return "tap_element", tap_params_for_edge(fsm, edge_step)


def tap_params_for_edge(fsm: dict[str, Any], edge: dict[str, Any]) -> dict[str, Any]:
    """把 nav 边编译成 tap_element 参数。"""
    exe = dict(edge.get("execute") or {})
    slot_idx = exe.get("tab_slot_index")
    if slot_idx is not None:
        try:
            idx = int(slot_idx)
        except (TypeError, ValueError):
            idx = -1
        if idx >= 0:
            meta = fsm.get("meta") if isinstance(fsm.get("meta"), dict) else {}
            tab_bar = meta.get("tab_bar") if isinstance(meta.get("tab_bar"), dict) else {}
            slots = tab_bar.get("slots") if isinstance(tab_bar.get("slots"), list) else []
            if 0 <= idx < len(slots):
                row = slots[idx] if isinstance(slots[idx], dict) else {}
                bounds = row.get("bounds") or []
                out: dict[str, Any] = {"tab_slot_index": idx}
                label = str(row.get("label") or row.get("display") or "").strip()
                if label and label not in ("icon", "图标 Tab"):
                    out["selector_text"] = label
                    out["text"] = label
                if isinstance(bounds, (list, tuple)) and len(bounds) >= 4:
                    from mino_nexus.loop.hierarchy_slots import int_list

                    bb = int_list(bounds, 4)
                    out["fallback_xy"] = [(bb[0] + bb[2]) // 2, (bb[1] + bb[3]) // 2]
                parts = row.get("parts") if isinstance(row.get("parts"), list) else []
                if idx > 0 and idx + 1 < len(slots):
                    left = slots[idx - 1]
                    right = slots[idx + 1]
                    if isinstance(left, dict) and isinstance(right, dict):
                        la = str(left.get("label") or left.get("display") or "").strip()
                        ra = str(right.get("label") or right.get("display") or "").strip()
                        if la and ra:
                            out["anchor_between"] = [la, ra]
                return out
    target_page = execute_target_page(exe)
    if target_page:
        return {"selector_text": target_page, "text": target_page}
    sel = str(exe.get("selector_text") or exe.get("text") or "").strip()
    if sel:
        return {"selector_text": sel, "text": sel}
    em = edge.get("meta") if isinstance(edge.get("meta"), dict) else {}
    action_type = str(em.get("action_type") or "").strip().lower()
    if action_type == "back" or em.get("reverse"):
        al = str(em.get("action_label") or "").strip()
        back_label = al if al and al not in ("点击", "tap") else "返回"
        return {"selector_text": back_label, "text": back_label}
    al = str(em.get("action_label") or "").strip()
    if al and "·" in al:
        part = al.split("·", 1)[-1].strip()
        if part:
            return {"selector_text": part, "text": part}
    if al and al not in ("点击", "tap"):
        return {"selector_text": al, "text": al}
    dst = str(edge.get("to") or "").strip()
    label = tab_label_for_state(fsm, dst)
    if label:
        return {"selector_text": label, "text": label}
    short = dst.split(".")[-1]
    if short and short not in ("home", "unknown"):
        return {"selector_text": short, "text": short}
    return {}


def tab_root_entry_hint(fsm: dict[str, Any], to_state_ref: str) -> str:
    """当前屏无路可走时，找一个「退栈到哪儿就有路」的入口态。

    深页 → Tab 页的两段式：先 BACK 回并列入口，再按图走。没有这句提示时模型只会
    看到「无路径」，然后继续盲调 fsm_navigate。
    """
    dst = resolve_state_ref(fsm, to_state_ref)
    if not dst or not F.state_by_id(fsm, dst):
        return ""
    from mino_nexus.services.nav_compiler import state_label

    best_sid = ""
    best_hops = 0
    for st in fsm.get("states") or []:
        sid = str((st or {}).get("id") or "").strip()
        if not sid or sid == dst:
            continue
        if not (st.get("entry") or is_tab_shell_state(fsm, sid)):
            continue
        path = F.shortest_nav_path(fsm, sid, dst)
        if not path:
            continue
        if not best_sid or len(path) < best_hops:
            best_sid, best_hops = sid, len(path)
    if not best_sid:
        return ""
    return f"退到并列入口「{state_label(fsm, best_sid)}」后 {best_hops} 步可到目标"


def direct_tab_tap_params(fsm: dict[str, Any], to_state_ref: str) -> dict[str, Any]:
    """无路可走时，尝试按目标屏展示名 / Tab 文案直点。"""
    raw_ref = str(to_state_ref or "").strip()
    label = tab_label_for_state(fsm, to_state_ref) or raw_ref
    if not tab_label_for_state(fsm, to_state_ref):
        for variant in _ref_display_variants(raw_ref):
            if variant:
                label = variant
                break
    sid = resolve_state_ref(fsm, to_state_ref)
    if sid:
        st = F.state_by_id(fsm, sid)
        if st:
            meta = st.get("meta") if isinstance(st.get("meta"), dict) else {}
            dn = str(meta.get("display_name") or "").strip()
            if dn:
                label = dn
    if label:
        return {"selector_text": label, "text": label}
    return {}


def plan_route_for_app(
    app_id: str,
    *,
    from_state: str,
    to_state: str,
    version: str = store.DEFAULT_VERSION,
    use_live: bool = True,
    project_id: str = "",
) -> dict[str, Any]:
    doc, source = load_fsm_doc(app_id, version=version, use_live=use_live, project_id=project_id)
    if not doc:
        return {"ok": False, "error": f"app_id={app_id} 没有 nav_fsm 配置", "steps": [], "edge_ids": []}
    out = plan_route(doc, from_state=from_state, to_state=to_state)
    out["app_id"] = app_id
    out["source"] = source
    out["fsm"] = doc
    return out
