"""RouteAssist 编译：把 NavPlan 编成注入 prompt 的一段文本。

设计稿 docs/NAVIGATION_ATLAS.md §5、§6。

两条克制原则：
  1. **每轮只注入一段 assist，不灌整条路径** —— 灌路径等于让模型照抄，屏一变就全错；
  2. **菜单不硬裁剪**（首期）—— 只在 prompt 里写【本步允许 / 禁止】，
     真拦截交给 GuardGate。硬裁菜单一旦配错，模型连恢复边都没得选。

Wiki 走「链接 + 编译式摘要」：FSM 上挂 `wiki_ref: knowledge:<entry_id>`，
按 `knowledge_situation.need` **分档截断**后注入，超长部分留在 Console 详情页。
不部署独立 llm-wiki 服务，正文留在 sqlite（§6）。

本文件的所有文案都是**框架措辞**，被测 App 的字样一律来自 DB 配置。
"""
from __future__ import annotations

from typing import Any

from mino_nexus.services.nav_fsm import NavPlan

# §6 分档截断。key 是 knowledge_situation 的 need
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
    """`knowledge:<id>` → `<id>`。不是这个前缀的引用忽略（未来可能有别的真源）。"""
    out: list[str] = []
    for ref in wiki_refs or []:
        raw = str(ref or "").strip()
        if raw.startswith(_WIKI_PREFIX):
            kid = raw[len(_WIKI_PREFIX) :].strip()
            if kid:
                out.append(kid)
    return out


def need_cap(item: dict[str, Any]) -> int:
    """按条目的 `situation.need` 取截断长度。取不到就用默认档，不一刀切。"""
    from mino_nexus.services.knowledge_situation import item_for_situation_match

    try:
        enriched = item_for_situation_match(dict(item or {}))
    except Exception:  # noqa: BLE001 — 分档失败退默认档，不该拖垮注入
        enriched = dict(item or {})
    sit = enriched.get("situation") if isinstance(enriched.get("situation"), dict) else {}
    need = str((sit or {}).get("need") or "").strip().lower()
    return NEED_CAPS.get(need, DEFAULT_NEED_CAP)


def wiki_block(wiki_refs: list[str], *, app_id: str = "", project_id: str = "") -> str:
    """取 wiki 正文并按 need 分档截断。取不到就返回空串，绝不阻断本 turn。"""
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


def compile_assist(
    plan: NavPlan,
    *,
    app_id: str = "",
    project_id: str = "",
    run_type: str = "manual",
    include_wiki: bool = True,
) -> str:
    """产出注入 `nav_assist` 槽的整段文本。plan 为空或没结论时返回空串（槽会整块跳过）。"""
    if plan is None or not plan.state_id:
        return ""

    parts: list[str] = [_head(plan)]

    edge_line = _edge_line(plan)
    if edge_line:
        parts.append(edge_line)

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


def _head(plan: NavPlan) -> str:
    band_note = {
        "high": "",
        "explore": "（置信中等：优先确认当前屏，别急着走远路）",
        "recover": "（置信低：可调 recover_fsm_navigate 规划路径，或 go_home/press_back，不要盲点）",
    }.get(plan.band, "")
    return f"【导航 assist】loc={plan.state_id} ({plan.confidence:.2f}){band_note}"


def _edge_line(plan: NavPlan) -> str:
    if not plan.edge:
        if plan.band == "recover":
            goal = str(plan.goal_state_id or "").strip()
            if goal:
                return f"【导航目标】{goal}（置信低：先恢复，再按路线图最短路前往）"
            return ""
        reason = plan.edge_reason or "当前守卫下没有可走的边"
        return f"【建议边】无（{reason}）"
    edge = plan.edge
    steps = [str(s) for s in ((edge.get("execute") or {}).get("steps") or [])]
    tail = f" → {'、'.join(steps)}" if steps else ""
    route = ""
    if len(plan.route_edge_ids or []) > 1:
        route = f"；全程 {' → '.join(plan.route_edge_ids)}"
    goal = ""
    if plan.goal_state_id and plan.goal_state_id != plan.state_id:
        goal = f"；目标 {plan.goal_state_id}"
    return f"【建议边】{edge.get('id') or ''}{tail}（本步到 {edge.get('to') or ''}{goal}{route}）"


def _allow_block(plan: NavPlan, *, run_type: str = "manual") -> str:
    rows = [f"- {cap}" for cap in plan.allow]
    if str(run_type or "manual").lower() in ("manual", "copilot"):
        rows.append("- signal_ask_human")
    rows.append("- signal_give_up")
    if plan.recommend:
        rows.append(f"- 推荐优先：{'、'.join(plan.recommend)}")
    if not plan.allow:
        rows.insert(0, "- （本步没有推荐动作，先用恢复动作或问人）")
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
    rows.append("- 点击类动作请只从【本步允许】里选；点别的元素守卫会拦（恢复动作与 signal_* 不受此限）。")
    return "【禁止】\n" + "\n".join(rows)


def _scroll_block(plan: NavPlan) -> str:
    hint = plan.scroll_hint or {}
    terms = "、".join(str(t) for t in (hint.get("anchor_terms") or []))
    direction = str(hint.get("direction") or "up")
    return (
        f"【锚点】目标「{terms}」不在当前屏。请先向{'上' if direction == 'up' else '下'}滑动"
        f"（最多 {hint.get('max_swipes') or 8} 次）把它滚出来，再执行建议边；"
        "滚到之后守卫会重新判定，不要跳过。"
    )
