"""RouteAssist 编译：把 NavPlan 编成注入 prompt 的一段文本。

设计稿 docs/NAVIGATION_ATLAS.md §5、§6。
文案必须让人和大模型都能读懂：用 Tab/屏面中文名，不要裸 state_id / edge_id。
"""
from __future__ import annotations

from typing import Any

from mino_nexus.services.nav_fsm import NavPlan, state_by_id

NEED_CAPS: dict[str, int] = {
    "howto": 800,
    "judge": 300,
    "judge_selected": 300,
    "exception": 500,
}
DEFAULT_NEED_CAP = 400

_WIKI_PREFIX = "knowledge:"
_MAX_WIKI_ENTRIES = 2


def entry_ids(wiki_refs: list[str]) -> list[str]:
    out: list[str] = []
    for ref in wiki_refs or []:
        raw = str(ref or "").strip()
        if raw.startswith(_WIKI_PREFIX):
            kid = raw[len(_WIKI_PREFIX) :].strip()
            if kid:
                out.append(kid)
    return out


def need_cap(item: dict[str, Any]) -> int:
    from mino_nexus.services.knowledge_situation import item_for_situation_match

    try:
        enriched = item_for_situation_match(dict(item or {}))
    except Exception:  # noqa: BLE001
        enriched = dict(item or {})
    sit = enriched.get("situation") if isinstance(enriched.get("situation"), dict) else {}
    need = str((sit or {}).get("need") or "").strip().lower()
    return NEED_CAPS.get(need, DEFAULT_NEED_CAP)


def wiki_block(wiki_refs: list[str], *, app_id: str = "", project_id: str = "") -> str:
    ids = entry_ids(wiki_refs)[:_MAX_WIKI_ENTRIES]
    if not ids:
        return ""
    try:
        from mino_nexus.services.knowledge_match import items_by_ids

        items = items_by_ids(ids, app_id=app_id, project_id=project_id)
    except Exception:  # noqa: BLE001
        return ""
    lines: list[str] = []
    for item in items:
        body = str(item.get("content") or "").strip()
        if not body:
            continue
        cap = need_cap(item)
        clipped = body[:cap] + ("…（详见 Console 知识详情）" if len(body) > cap else "")
        title = str(item.get("title") or "").strip()
        lines.append(f"- {title}：{clipped}" if title else f"- {clipped}")
    return "\n".join(lines)


def state_label(fsm: dict[str, Any] | None, state_id: str) -> str:
    """屏面中文/可读名（配置真源在 NavFSM，不硬编码 App 文案）。"""
    sid = str(state_id or "").strip()
    if not sid:
        return "未知屏"
    fsm = fsm if isinstance(fsm, dict) else {}
    st = state_by_id(fsm, sid)
    meta = (st or {}).get("meta") if isinstance(st, dict) else {}
    if isinstance(meta, dict):
        dn = str(meta.get("display_name") or meta.get("page_title") or "").strip()
        if dn:
            return dn
    tab_bar = (fsm.get("meta") or {}).get("tab_bar") if isinstance(fsm.get("meta"), dict) else {}
    labels = tab_bar.get("labels") if isinstance(tab_bar, dict) else {}
    if isinstance(labels, dict) and sid in labels:
        lab = str(labels[sid] or "").strip()
        if lab:
            return lab
    if st:
        identify = st.get("identify") or {}
        blocks = identify.get("required") if isinstance(identify.get("required"), list) else []
        for block in blocks or []:
            if not isinstance(block, dict):
                continue
            if block.get("signal") == "tab_bar":
                tab = str((block.get("match") or {}).get("selected") or "").strip()
                if tab:
                    return tab
    short = sid.replace("page.", "").replace("tab_", "").replace("_", " · ")
    return short or sid


def edge_action_label(fsm: dict[str, Any] | None, edge: dict[str, Any] | None) -> str:
    if not edge:
        return ""
    ui = str(edge.get("ui_action") or "").strip()
    if ui:
        return ui
    ex = edge.get("execute") if isinstance(edge.get("execute"), dict) else {}
    tab = str(ex.get("target_tab") or ex.get("selector_text") or "").strip()
    if tab:
        return f"点击「{tab}」"
    label = str(ex.get("target_label") or "").strip()
    if label:
        return f"点击「{label}」"
    steps = ex.get("steps") or []
    if steps:
        return str(steps[0])
    return "执行跳转"


def compile_assist(
    plan: NavPlan,
    *,
    fsm: dict[str, Any] | None = None,
    app_id: str = "",
    project_id: str = "",
    run_type: str = "manual",
    include_wiki: bool = True,
) -> str:
    if plan is None or not plan.state_id:
        return ""
    if str(run_type or "").lower() == "explore":
        return ""

    if not getattr(plan, "case_navigation_goal", False):
        return _head(plan, fsm).strip()

    parts: list[str] = [_head(plan, fsm)]

    route_line = _route_line(plan, fsm)
    if route_line:
        parts.append(route_line)

    allow = _allow_block(plan, run_type=run_type)
    if allow:
        parts.append(allow)

    forbid = _forbid_block(plan)
    if forbid:
        parts.append(forbid)

    if plan.anchor_missing and plan.scroll_hint:
        parts.append(_scroll_block(plan))

    if include_wiki:
        wiki = wiki_block(plan.wiki_refs, app_id=app_id, project_id=project_id)
        if wiki:
            parts.append("【守卫说明】\n" + wiki)

    return "\n\n".join(p for p in parts if p).strip()


def _head(plan: NavPlan, fsm: dict[str, Any] | None) -> str:
    here = state_label(fsm, plan.state_id)
    band_note = {
        "high": "",
        "explore": "（定位置信度中等：先确认是否在当前屏，勿走远路）",
        "recover": "（定位置信度低：优先恢复/问人，勿乱点）",
    }.get(plan.band, "")
    conf = f"{plan.confidence:.0%}" if plan.confidence <= 1 else f"{plan.confidence:.2f}"
    return f"【导航】当前屏：{here}（id={plan.state_id}，置信 {conf}）{band_note}"


def _route_line(plan: NavPlan, fsm: dict[str, Any] | None) -> str:
    goal = str(plan.goal_state_id or "").strip()
    goal_name = state_label(fsm, goal) if goal else ""

    if plan.route_uncovered or (
        goal
        and goal != str(plan.state_id or "").strip()
        and not plan.edge
        and "路线图未覆盖" in str(plan.edge_reason or "")
    ):
        return (
            f"【路线】路线图未覆盖：从当前屏无法沿已发布跳转到达用例目标「{goal_name}」"
            f"（{goal}）。不要编造 Tab/返回路径；按用例步骤点击，或 signal_ask_human / signal_give_up。"
        )

    if goal and goal == str(plan.state_id or "").strip():
        return f"【路线】已在用例导航目标屏「{goal_name}」。按用例步骤继续，无需为赶路再切 Tab。"

    if not plan.edge:
        if plan.band == "recover":
            if goal_name:
                return f"【路线】置信低，暂不推荐跳转边。用例目标为「{goal_name}」；请先 recover 或问人。"
            return ""
        reason = str(plan.edge_reason or "当前没有可执行的跳转边").strip()
        return f"【路线】本步无推荐跳转（{reason}）"

    edge = plan.edge
    action = edge_action_label(fsm, edge)
    dest = state_label(fsm, str(edge.get("to") or ""))
    dest_id = str(edge.get("to") or "")
    tail = f" → 进入「{dest}」" if dest else ""
    if goal_name and dest_id and dest_id != goal:
        tail += f"（用例目标为「{goal_name}」，全程可能还需后续步骤）"
    elif goal_name and dest_id == goal:
        tail += "（下一步即到用例目标屏）"
    return f"【路线】下一步：{action}{tail}"


def _allow_block(plan: NavPlan, *, run_type: str = "manual") -> str:
    rows = [f"- {cap}" for cap in plan.allow if cap not in ("tap_element",)]
    if any(c == "tap_element" for c in plan.allow) or plan.edge:
        rows.insert(0, "- tap_element（仅用于执行【路线】中的点击，或按用例步骤点对应控件）")
    if str(run_type or "manual").lower() in ("manual", "copilot"):
        rows.append("- signal_ask_human")
    rows.append("- signal_give_up")
    if plan.recommend:
        rows.append(f"- 推荐优先：{'、'.join(plan.recommend)}")
    if not rows:
        rows.append("- （无推荐动作：先恢复或问人）")
    return "【本步允许】\n" + "\n".join(rows)


def _forbid_block(plan: NavPlan) -> str:
    rows: list[str] = []
    for item in plan.forbid:
        match = item.get("match") if isinstance(item.get("match"), dict) else {}
        cond = "、".join(f"{k}={v}" for k, v in match.items()) or "该元素"
        reason = str(item.get("reason") or "").strip()
        widget = str(item.get("widget") or "")
        rows.append(f"- 禁止 tap {cond}（{widget} 命中守卫{'：' + reason if reason else ''}）")
    if not rows:
        return ""
    rows.append("- 除【本步允许】外，其它点击类动作可能被守卫拦截。")
    return "【禁止】\n" + "\n".join(rows)


def _scroll_block(plan: NavPlan) -> str:
    hint = plan.scroll_hint or {}
    terms = "、".join(str(t) for t in (hint.get("anchor_terms") or []))
    direction = str(hint.get("direction") or "up")
    return (
        f"【滚动】用例要找的「{terms}」不在当前屏。请先向{'上' if direction == 'up' else '下'}滑"
        f"（最多 {hint.get('max_swipes') or 8} 次），再执行【路线】中的动作。"
    )
