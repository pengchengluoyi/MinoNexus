"""NavFSM 运行时：在满足守卫的前提下，下一步转移是什么 + 本步允许哪些动作。

设计稿 docs/NAVIGATION_ATLAS.md §3、§5、§10.4。

**配置全部来自 DB**（`nav_fsm_store`），本文件不含任何被测 App 的文案、包名、resource-id。
guards 是单一真源（§0.1「guards 单一真源」），不另建 Rule Cards。

widget 状态的判定模型
---------------------
设计稿 §3.2 把 `detect` 画在 widget 层，§8.3 的 guard 表却是**一行一个 widget 状态**
（filled / outline 各有自己的 `guard_id` 与 `strength`）。这里按后者实现：
每个 widget 状态可以有自己的 `detect`，widget 层的 `detect` 作为默认值合并下去。
widget 的当前状态 = 第一个 `detect` 命中的状态。两种写法都能读。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from mino_nexus.loop.hierarchy_slots import match_any, match_none

# guard 强度（§8.3）。medium / weak 永远不得进 block/stop 计数。
STRENGTH_STRONG_ID = "strong_id"
STRENGTH_STRONG_TEXT = "strong_text"
STRENGTH_MEDIUM = "medium"
STRENGTH_WEAK = "weak"
BLOCKABLE_STRENGTHS = frozenset({STRENGTH_STRONG_ID, STRENGTH_STRONG_TEXT})

_TEMPLATE_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_.]+)\s*\}\}")


@dataclass
class GuardHit:
    """一个 widget 状态的判定结果。"""

    guard_id: str
    widget: str
    widget_state: str
    strength: str = STRENGTH_WEAK
    detected: bool = False
    on_miss: str = "steer_only"
    tap_allowed: list[str] = field(default_factory=list)
    tap_forbidden: list[dict[str, Any]] = field(default_factory=list)
    tap_recommended: list[str] = field(default_factory=list)
    wiki_ref: str = ""
    # 这个态是 VLM 判出来的而不是 hierarchy 判出来的。§8.2：guard 违反只认 hierarchy + 规则，
    # 所以 VLM 结论**永远不得进 block/stop**，只能用于推荐与选边。
    via_vlm: bool = False
    vlm_confidence: float = 0.0

    def blockable(self) -> bool:
        """够不够格进 GuardGate 的 block/stop 阶梯（§8.2 硬约束）。"""
        if self.via_vlm:
            return False
        return self.detected and self.strength in BLOCKABLE_STRENGTHS


@dataclass
class NavPlan:
    """本 turn 交给 compiler 的全部结论。"""

    state_id: str = ""
    confidence: float = 0.0
    band: str = "recover"
    edge: Optional[dict[str, Any]] = None
    edge_reason: str = ""
    guards: list[GuardHit] = field(default_factory=list)
    allow: list[str] = field(default_factory=list)
    forbid: list[dict[str, Any]] = field(default_factory=list)
    recommend: list[str] = field(default_factory=list)
    wiki_refs: list[str] = field(default_factory=list)
    anchor_missing: bool = False
    scroll_hint: dict[str, Any] = field(default_factory=dict)
    goal_state_id: str = ""
    route_edge_ids: list[str] = field(default_factory=list)


# ---------------- 取配置 ----------------


def state_by_id(fsm: dict[str, Any], state_id: str) -> Optional[dict[str, Any]]:
    for st in fsm.get("states") or []:
        if str(st.get("id") or st.get("state_id") or "") == str(state_id or ""):
            return st
    return None


def recover_edges(fsm: dict[str, Any]) -> list[dict[str, Any]]:
    """全局恢复边（§2.3）。`from` 为空或 `*` 表示从任意状态可走。"""
    out = []
    for ed in fsm.get("edges") or []:
        if str(ed.get("kind") or "nav") != "recover":
            continue
        src = str(ed.get("from") or ed.get("from_state") or "").strip()
        if src in ("", "*"):
            out.append(ed)
    return out


def recover_target_state(fsm: dict[str, Any], case: dict[str, Any] | None = None) -> str:
    """迷路后的目标页（运行时路径规划终点，不画在路线图里）。"""
    return navigation_goal_state(fsm, case)


def navigation_goal_state(fsm: dict[str, Any], case: dict[str, Any] | None = None) -> str:
    """本用例导航目标：case 字段优先，否则应用默认首页。"""
    meta = fsm.get("meta") if isinstance(fsm.get("meta"), dict) else {}
    recover = meta.get("recover") if isinstance(meta.get("recover"), dict) else {}
    for field in (
        str(recover.get("case_goal_field") or "nav_goal_state").strip(),
        str(recover.get("case_override_field") or "nav_recover_state").strip(),
        "nav_entry_state",
    ):
        if case and field:
            val = str(case.get(field) or "").strip()
            if val:
                return val
    goal = str(recover.get("default_goal_state_id") or recover.get("default_state_id") or "").strip()
    if goal:
        return goal
    for ed in recover_edges(fsm):
        to = str(ed.get("to") or ed.get("to_state") or "").strip()
        if to:
            return to
    return ""


def recover_capabilities(fsm: dict[str, Any]) -> list[str]:
    """恢复动作列表。`meta.recover.runtime_only` 时不走 edges 表。"""
    meta = fsm.get("meta") if isinstance(fsm.get("meta"), dict) else {}
    recover = meta.get("recover") if isinstance(meta.get("recover"), dict) else {}
    if recover.get("runtime_only"):
        caps = [str(c).strip() for c in (recover.get("capabilities") or []) if str(c).strip()]
        if caps:
            return caps
    out: list[str] = []
    for ed in recover_edges(fsm):
        out.extend(str(s) for s in ((ed.get("execute") or {}).get("steps") or []))
    return [c for c in out if c]


def recover_edges_for_case(fsm: dict[str, Any], case: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """遗留：带固定 to 的 recover 边。新配置请用 recover_capabilities + shortest_nav_path。"""
    if recover_capabilities(fsm) and (fsm.get("meta") or {}).get("recover", {}).get("runtime_only"):
        return []
    target = recover_target_state(fsm, case)
    out: list[dict[str, Any]] = []
    for ed in recover_edges(fsm):
        row = dict(ed)
        if target:
            row["to"] = target
        out.append(row)
    return out


def nav_edges(fsm: dict[str, Any]) -> list[dict[str, Any]]:
    return [ed for ed in (fsm.get("edges") or []) if str(ed.get("kind") or "nav") == "nav"]


def edge_by_id(fsm: dict[str, Any], edge_id: str) -> Optional[dict[str, Any]]:
    eid = str(edge_id or "").strip()
    if not eid:
        return None
    for ed in fsm.get("edges") or []:
        if str(ed.get("id") or ed.get("edge_id") or "") == eid:
            return ed
    return None


def shortest_nav_path(
    fsm: dict[str, Any],
    from_state: str,
    to_state: str,
) -> list[dict[str, Any]]:
    """在 nav 边图上 BFS 最短路（仅运行时，recover 不参与构图）。"""
    start = str(from_state or "").strip()
    goal = str(to_state or "").strip()
    if not start or not goal or start == goal:
        return []
    adj: dict[str, list[dict[str, Any]]] = {}
    for ed in nav_edges(fsm):
        src = str(ed.get("from") or ed.get("from_state") or "").strip()
        dst = str(ed.get("to") or ed.get("to_state") or "").strip()
        if src and dst:
            adj.setdefault(src, []).append(ed)
    if start not in adj and start != goal:
        return []
    queue: list[tuple[str, list[dict[str, Any]]]] = [(start, [])]
    seen = {start}
    while queue:
        cur, path = queue.pop(0)
        if cur == goal:
            return path
        for ed in adj.get(cur, []):
            nxt = str(ed.get("to") or ed.get("to_state") or "").strip()
            if not nxt or nxt in seen:
                continue
            seen.add(nxt)
            queue.append((nxt, [*path, ed]))
    return []


def choose_edge_toward_goal(
    fsm: dict[str, Any],
    *,
    state_id: str,
    goal_state_id: str,
    hits: list[GuardHit],
    nodes: list[dict[str, Any]],
    case: dict[str, Any] | None = None,
    success_rate: dict[str, float] | None = None,
) -> tuple[Optional[dict[str, Any]], list[dict[str, Any]], bool, str]:
    """朝目标页选本步 nav 边：最短路第一步；无路则退回 choose_edge。"""
    goal = str(goal_state_id or "").strip()
    if goal and goal != str(state_id or "").strip():
        path = shortest_nav_path(fsm, state_id, goal)
        if path:
            edge = path[0]
            ok, missing, why = edge_guard_ok(edge, hits, nodes, case)
            if ok:
                return edge, path, missing, ""
            return None, path, missing, why or "最短路第一步 guard 未满足"
    edge, missing, reason = choose_edge(
        fsm,
        state_id=state_id,
        hits=hits,
        nodes=nodes,
        case=case,
        success_rate=success_rate,
    )
    return edge, ([edge] if edge else []), missing, reason


def edges_from(fsm: dict[str, Any], state_id: str) -> list[dict[str, Any]]:
    sid = str(state_id or "")
    return [
        ed
        for ed in fsm.get("edges") or []
        if str(ed.get("kind") or "nav") == "nav"
        and str(ed.get("from") or ed.get("from_state") or "") == sid
    ]


# ---------------- guard 判定 ----------------


def evaluate_guards(state: dict[str, Any] | None, nodes: list[dict[str, Any]]) -> list[GuardHit]:
    """跑一遍这一屏所有 widget 的 detect。hierarchy 为空时返回全 `detected=False`。"""
    hits: list[GuardHit] = []
    guards = (state or {}).get("guards") if isinstance((state or {}).get("guards"), dict) else {}
    for widget, cfg in (guards or {}).items():
        if not isinstance(cfg, dict):
            continue
        base_detect = cfg.get("detect") if isinstance(cfg.get("detect"), dict) else {}
        for wstate, spec in (cfg.get("states") or {}).items():
            if not isinstance(spec, dict):
                continue
            detect = {**base_detect, **(spec.get("detect") or {})}
            hits.append(
                GuardHit(
                    guard_id=str(spec.get("guard_id") or f"{widget}.{wstate}"),
                    widget=str(widget),
                    widget_state=str(wstate),
                    strength=str(detect.get("strength") or STRENGTH_WEAK),
                    detected=_detect_hit(detect, nodes),
                    on_miss=str(detect.get("on_miss") or "steer_only"),
                    tap_allowed=[str(x) for x in (spec.get("tap_allowed") or [])],
                    tap_forbidden=[x for x in (spec.get("tap_forbidden") or []) if isinstance(x, dict)],
                    tap_recommended=[str(x) for x in (spec.get("tap_recommended") or [])],
                    wiki_ref=str(spec.get("wiki_ref") or ""),
                )
            )
    return hits


def _detect_hit(detect: dict[str, Any], nodes: list[dict[str, Any]]) -> bool:
    if not detect or not nodes:
        return False
    any_conds = detect.get("match_any") or []
    if not any_conds:
        return False
    if match_any(nodes, any_conds) is None:
        return False
    return match_none(nodes, detect.get("match_none") or [])


def widget_state_of(hits: list[GuardHit], widget: str) -> str:
    for hit in hits:
        if hit.widget == widget and hit.detected:
            return hit.widget_state
    return ""


# ---------------- 选边 ----------------


def render_template(value: Any, case: dict[str, Any] | None) -> Any:
    """把 `{{case.nav_anchor.author_name}}` 换成用例里的值。取不到留空串。"""
    if isinstance(value, dict):
        return {k: render_template(v, case) for k, v in value.items()}
    if isinstance(value, list):
        return [render_template(v, case) for v in value]
    if not isinstance(value, str):
        return value

    def repl(m: re.Match[str]) -> str:
        path = m.group(1).split(".")
        cur: Any = {"case": dict(case or {})}
        for seg in path:
            if isinstance(cur, dict) and seg in cur:
                cur = cur[seg]
            else:
                return ""
        return str(cur) if cur is not None else ""

    return _TEMPLATE_RE.sub(repl, value)


def anchor_terms(edge: dict[str, Any], case: dict[str, Any] | None) -> list[str]:
    guard = edge.get("guard") if isinstance(edge.get("guard"), dict) else {}
    anchor = guard.get("anchor") if isinstance(guard.get("anchor"), dict) else {}
    rendered = render_template(anchor, case)
    return [str(v).strip() for v in (rendered or {}).values() if str(v or "").strip()]


def edge_guard_ok(
    edge: dict[str, Any],
    hits: list[GuardHit],
    nodes: list[dict[str, Any]],
    case: dict[str, Any] | None,
) -> tuple[bool, bool, str]:
    """边的 guard 是否满足。返回 `(ok, anchor_missing, reason)`。

    anchor 与 widget 态是**独立**的两件事（§10.4）：anchor 没命中只说明目标行还没滚出来，
    不代表 widget 态不成立，所以单独回一个 `anchor_missing`，由上层去 steer 滚动。
    """
    guard = edge.get("guard") if isinstance(edge.get("guard"), dict) else {}
    for key, want in (guard or {}).items():
        if key == "anchor":
            continue
        if not str(key).startswith("widget."):
            continue
        actual = widget_state_of(hits, str(key))
        if str(want) != actual:
            return False, False, f"{key} 当前={actual or '未判定'}，需要={want}"

    terms = anchor_terms(edge, case)
    if terms:
        if not nodes:
            return False, True, "anchor 无法判定（本轮没有 hierarchy）"
        if match_any(nodes, terms) is None:
            return False, True, f"anchor 未在当前屏命中：{'、'.join(terms)}"
    return True, False, ""


def choose_edge(
    fsm: dict[str, Any],
    *,
    state_id: str,
    hits: list[GuardHit],
    nodes: list[dict[str, Any]],
    case: dict[str, Any] | None = None,
    success_rate: dict[str, float] | None = None,
) -> tuple[Optional[dict[str, Any]], bool, str]:
    """在线选边（§0.1「规划是条件转移」）。返回 `(edge, anchor_missing, reason)`。"""
    rates = success_rate or {}
    ready: list[tuple[float, dict[str, Any]]] = []
    anchor_missing = False
    reasons: list[str] = []
    for edge in edges_from(fsm, state_id):
        ok, missing, why = edge_guard_ok(edge, hits, nodes, case)
        if ok:
            ready.append((float(rates.get(str(edge.get("id") or ""), 0.5)), edge))
        else:
            anchor_missing = anchor_missing or missing
            if why:
                reasons.append(f"{edge.get('id')}: {why}")
    if not ready:
        return None, anchor_missing, "；".join(reasons[:3])
    ready.sort(key=lambda pair: (-pair[0], str(pair[1].get("id") or "")))
    return ready[0][1], anchor_missing, ""


# ---------------- 允许动作集 ----------------


def build_plan(
    fsm: dict[str, Any],
    *,
    state_id: str,
    confidence: float,
    band: str,
    nodes: list[dict[str, Any]],
    case: dict[str, Any] | None = None,
    recover_caps: list[str] | None = None,
    success_rate: dict[str, float] | None = None,
    hits: list[GuardHit] | None = None,
) -> NavPlan:
    """把「在哪 + guard 判定 + 选边」拼成 compiler 要的一份结论。

    低置信档（§2.2）只给恢复边与探索，不推荐 nav 边 —— 都不知道在哪就别指路。
    """
    state = state_by_id(fsm, state_id)
    if hits is None:
        hits = evaluate_guards(state, nodes)
    plan = NavPlan(state_id=state_id, confidence=confidence, band=band, guards=hits)

    for hit in hits:
        if not hit.detected:
            continue
        plan.forbid.extend(
            {**item, "guard_id": hit.guard_id, "widget": hit.widget} for item in hit.tap_forbidden
        )
        plan.allow.extend(hit.tap_allowed)
        plan.recommend.extend(hit.tap_recommended)
        if hit.wiki_ref:
            plan.wiki_refs.append(hit.wiki_ref)

    goal = navigation_goal_state(fsm, case)
    plan.goal_state_id = goal

    if band != "recover":
        edge, path, anchor_missing, reason = choose_edge_toward_goal(
            fsm,
            state_id=state_id,
            goal_state_id=goal,
            hits=hits,
            nodes=nodes,
            case=case,
            success_rate=success_rate,
        )
        plan.edge = edge
        plan.anchor_missing = anchor_missing
        plan.edge_reason = reason
        plan.route_edge_ids = [str(e.get("id") or "") for e in path if e and e.get("id")]
        if edge:
            plan.allow.extend(str(s) for s in ((edge.get("execute") or {}).get("steps") or []))
        elif anchor_missing:
            plan.scroll_hint = _scroll_hint(fsm, state_id, case)

    plan.allow.extend(str(c) for c in (recover_caps or []))
    plan.allow = _dedupe(plan.allow)
    plan.recommend = _dedupe(plan.recommend)
    plan.wiki_refs = _dedupe(plan.wiki_refs)
    return plan


def _scroll_hint(fsm: dict[str, Any], state_id: str, case: dict[str, Any] | None) -> dict[str, Any]:
    """anchor 没命中时，从边上的 `scroll_into_view` 取滚动参数（§10.4）。"""
    for edge in edges_from(fsm, state_id):
        cfg = edge.get("scroll_into_view") if isinstance(edge.get("scroll_into_view"), dict) else {}
        if not cfg:
            continue
        terms = anchor_terms(edge, case)
        if not terms:
            continue
        return {
            "edge_id": str(edge.get("id") or ""),
            "direction": str(cfg.get("direction") or "up"),
            "max_swipes": int(cfg.get("max_swipes") or 8),
            "anchor_terms": terms,
        }
    return {}


def _dedupe(rows: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for row in rows:
        key = str(row or "").strip()
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


# ---------------- effect_assert（§10.1） ----------------


def evaluate_effect_assert(
    spec: dict[str, Any] | None,
    *,
    nodes: list[dict[str, Any]],
    chosen_state: str = "",
    state_kind: str = "",
) -> tuple[str, str]:
    """边执行后的**强断言**。返回 `(result, reason)`，result ∈ pass / fail / skip。

    刻意**不依赖 localize 高置信**（§7 的循环依赖：localize 不准 → 判不了成功 → 更不敢自动改图）。
    主判据是 hierarchy 里稳定存在的元素；`state_delta` 只作附加条件。
    """
    if not spec:
        return "skip", "边没有配 effect_assert"
    if not nodes:
        return "skip", "本轮没有 hierarchy，不评估"

    require_any = spec.get("require_any") or []
    if require_any and not any(_assert_cond(c, nodes) for c in require_any):
        return "fail", "require_any 全部未命中"

    for cond in spec.get("require_none") or []:
        if _assert_cond(cond, nodes):
            return "fail", f"require_none 命中了：{cond}"

    delta = spec.get("state_delta") if isinstance(spec.get("state_delta"), dict) else {}
    for key, want in (delta or {}).items():
        if key == "state":
            if want and not _state_matches(str(chosen_state), str(want)):
                return "fail", f"state_delta.state 期望 {want}，实际 {chosen_state or '未定位'}"
        elif key == "dialog":
            has_dialog = state_kind == "dialog"
            if want in (None, "", "null") and has_dialog:
                return "fail", "state_delta.dialog 期望消失，但当前仍是弹窗"
            if want and not has_dialog:
                return "fail", f"state_delta.dialog 期望 {want}，当前不是弹窗"
    return "pass", ""


def _assert_cond(cond: Any, nodes: list[dict[str, Any]]) -> bool:
    if isinstance(cond, str):
        return match_any(nodes, [cond]) is not None
    if not isinstance(cond, dict):
        return False
    if "text_landmarks" in cond:
        return match_any(nodes, cond.get("text_landmarks") or []) is not None
    if "tab_bar" in cond:
        # hierarchy 不暴露 selected（§17.1），退化为文案存在性
        label = str((cond.get("tab_bar") or {}).get("selected") or "").strip()
        return bool(label) and match_any(nodes, [{"text_eq": label}]) is not None
    return match_any(nodes, [cond]) is not None


def _state_matches(actual: str, pattern: str) -> bool:
    """支持 `page.profile.*` 这种尾部通配。"""
    pat = str(pattern or "")
    if pat.endswith("*"):
        return str(actual or "").startswith(pat[:-1])
    return str(actual or "") == pat


# ---------------- VLM 兜底（§2.2、§8.3） ----------------


def vlm_fallback_cfg(fsm: dict[str, Any]) -> dict[str, Any]:
    """全局开关。**默认关** —— 主链稳定前不该让 VLM 进每 turn 的路径（§2.2「VLM 克制」）。"""
    meta = fsm.get("meta") if isinstance(fsm.get("meta"), dict) else {}
    cfg = meta.get("vlm_fallback") if isinstance(meta.get("vlm_fallback"), dict) else {}
    return {
        "enabled": cfg.get("enabled") is True,
        "max_calls_per_turn": int(cfg.get("max_calls_per_turn") or 1),
        "min_confidence": float(cfg.get("min_confidence") or 0.6),
    }


def vlm_candidates(state: dict[str, Any] | None, hits: list[GuardHit]) -> list[dict[str, Any]]:
    """哪些 widget 值得问 VLM：hierarchy **一个态都没判出来**，且配置里声明了兜底 job。

    判出来了就不问 —— 规则信号比 VLM 稳，问了只会引入噪声和延迟。
    """
    guards = (state or {}).get("guards") if isinstance((state or {}).get("guards"), dict) else {}
    out: list[dict[str, Any]] = []
    for widget, cfg in (guards or {}).items():
        if not isinstance(cfg, dict):
            continue
        if any(h.widget == widget and h.detected for h in hits):
            continue
        states = [str(s) for s in (cfg.get("states") or {}) if str(s)]
        if not states:
            continue
        job = str((cfg.get("detect") or {}).get("vlm_fallback") or "").strip()
        if not job:
            for spec in (cfg.get("states") or {}).values():
                if isinstance(spec, dict):
                    job = str((spec.get("detect") or {}).get("vlm_fallback") or "").strip()
                    if job:
                        break
        if job:
            out.append({"widget": str(widget), "states": states, "job": job})
    return out


def apply_vlm_state(
    hits: list[GuardHit], *, widget: str, widget_state: str, confidence: float
) -> bool:
    """把 VLM 的结论写回对应的 GuardHit。返回是否命中了某一条。

    强度不改，但 `via_vlm=True` 让 `blockable()` 恒为 False —— 这是 §8.2 的硬约束落点。
    """
    for hit in hits:
        if hit.widget == str(widget) and hit.widget_state == str(widget_state):
            hit.detected = True
            hit.via_vlm = True
            hit.vlm_confidence = float(confidence)
            return True
    return False


# ---------------- 跑前状态校验（§11.2） ----------------
#
# 设计稿把它叫 `check_follow_state`，那是按某个具体 App 的语义起的名。这里用中性名字，
# 因为「期望哪个控件处于哪个态」完全由 `test_data.precondition` 配置说了算，代码不认识业务。


def precondition_spec(fsm: dict[str, Any]) -> dict[str, Any]:
    test_data = fsm.get("test_data") if isinstance(fsm.get("test_data"), dict) else {}
    spec = test_data.get("precondition") if isinstance(test_data.get("precondition"), dict) else {}
    return dict(spec or {})


def check_precondition(
    fsm: dict[str, Any],
    *,
    state_id: str,
    nodes: list[dict[str, Any]],
    hits: list[GuardHit] | None = None,
) -> dict[str, Any]:
    """跑前确认租号处于用例要求的初态（§11.2）。

    结果只有三种：`ok` / `mismatch` / `skip`。**首期不自动恢复账号状态**（§11.1「跑后默认
    不自动回滚」），发现不符就把事实摆出来让人换租号或先恢复 —— 自动改账号状态比不改危险得多。
    """
    spec = precondition_spec(fsm)
    if not spec:
        return {"result": "skip", "reason": "未配置 test_data.precondition"}
    want_state = str(spec.get("state_id") or "").strip()
    widget = str(spec.get("widget") or "").strip()
    expect = str(spec.get("expect_state") or "").strip()
    if not widget or not expect:
        return {"result": "skip", "reason": "precondition 缺 widget / expect_state"}
    if not nodes:
        return {"result": "skip", "reason": "本轮没有 hierarchy，判不了"}
    if want_state and want_state != str(state_id or ""):
        return {
            "result": "skip",
            "reason": f"当前在 {state_id or '未定位'}，要在 {want_state} 上才判得准",
        }

    rows = hits if hits is not None else evaluate_guards(state_by_id(fsm, state_id), nodes)
    actual = widget_state_of(rows, widget)
    if actual == expect:
        return {"result": "ok", "widget": widget, "expect": expect, "actual": actual, "reason": ""}
    return {
        "result": "mismatch",
        "widget": widget,
        "expect": expect,
        "actual": actual,
        "reason": (
            f"{widget} 当前={actual or '未判定'}，用例要求={expect}。"
            "请先恢复租号初态或换一个租号再跑，否则本条用例的结果不可复现（§11.1）"
        ),
    }
