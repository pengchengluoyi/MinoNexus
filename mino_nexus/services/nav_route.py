"""NavFSM 路径规划：给定起止屏态，在 nav 边图上求最短路（运行时，不画恢复边）。"""
from __future__ import annotations

from typing import Any

from mino_nexus.services import nav_fsm as F
from mino_nexus.services import nav_fsm_store as store


def resolve_state_ref(fsm: dict[str, Any], ref: str) -> str:
    """把 state_id / Tab 文案 / 短 id 解析成图里的 state_id。"""
    val = str(ref or "").strip()
    if not val or not fsm:
        return ""
    if F.state_by_id(fsm, val):
        return val
    meta = fsm.get("meta") if isinstance(fsm.get("meta"), dict) else {}
    tab_bar = meta.get("tab_bar") if isinstance(meta.get("tab_bar"), dict) else {}
    labels = tab_bar.get("labels") if isinstance(tab_bar.get("labels"), dict) else {}
    for sid, label in labels.items():
        if str(label or "").strip() == val:
            return str(sid)
    for sid in tab_bar.get("entries") or []:
        if str(sid).endswith(val) or val in str(sid):
            return str(sid)
    for st in fsm.get("states") or []:
        sid = str(st.get("id") or "").strip()
        if not sid:
            continue
        if sid.endswith(f".{val}") or sid.split(".")[-1] == val:
            return sid
        identify = st.get("identify") or {}
        required = identify.get("required")
        blocks = required if isinstance(required, list) else ([required] if required else [])
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("signal") == "tab_bar":
                tab = str((block.get("match") or {}).get("selected") or "").strip()
                if tab == val:
                    return sid
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


def plan_route_for_app(
    app_id: str,
    *,
    from_state: str,
    to_state: str,
    version: str = store.DEFAULT_VERSION,
    use_live: bool = True,
    project_id: str = "",
) -> dict[str, Any]:
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
        doc = store.read_raw(app_id, version=version) or store.read_raw(app_id, version=store.DRAFT_VERSION)
        source = "published"
    if not doc:
        return {"ok": False, "error": f"app_id={app_id} 没有 nav_fsm 配置", "steps": [], "edge_ids": []}
    out = plan_route(doc, from_state=from_state, to_state=to_state)
    out["app_id"] = app_id
    out["source"] = source
    return out
