"""check 阶段：程序校验点执行 + 终裁（非对称 pass/fail）。"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from mino_nexus.loop.check_plan import CheckPlan, CheckPoint, build_check_plan
from mino_nexus.loop.hierarchy_slots import match_any

_COUNTER_RE = re.compile(r"\d+")
_VISUAL_CHECK_KINDS = frozenset(
    {"visual", "visual_assert", "layout", "screenshot", "assert_visual"}
)
_LIT_CLASS_TOKENS = ("active", "selected", "current", "checked", "on")


def _node_blob(node: dict[str, Any]) -> str:
    return " ".join(
        str(node.get(k) or "")
        for k in (
            "text",
            "content_desc",
            "aria_label",
            "name",
            "label",
            "class",
            "className",
            "resource_id",
        )
    )


def _node_lit_or_selected(node: dict[str, Any]) -> bool:
    if node.get("selected") is True or node.get("checked") is True:
        return True
    for key in ("aria_selected", "aria-selected", "aria_checked", "aria-checked"):
        val = str(node.get(key) or "").strip().lower()
        if val in ("true", "selected", "checked"):
            return True
    cls = str(node.get("class") or node.get("className") or "").lower()
    if any(tok in cls for tok in _LIT_CLASS_TOKENS):
        return True
    rid = str(node.get("resource_id") or node.get("resource-id") or "").lower()
    if any(tok in rid for tok in _LIT_CLASS_TOKENS):
        return True
    return False


def _selected_or_lit_for_selector(
    nodes: list[dict[str, Any]], selector: str
) -> tuple[str, float, str]:
    """返回 (status, confidence, summary)。"""
    term = str(selector or "").strip()
    if not term:
        return "inconclusive", 0.3, "selected_or_lit 缺少 selector"
    hits: list[dict[str, Any]] = []
    for node in nodes or []:
        if not isinstance(node, dict):
            continue
        if term in _node_blob(node):
            hits.append(node)
    if not hits:
        return "fail", 0.85, f"屏上无「{term[:24]}」"
    for node in hits:
        if _node_lit_or_selected(node):
            return "pass", 0.9, f"「{term[:24]}」呈选中/高亮态"
    return "fail", 0.88, f"有「{term[:24]}」但未检出选中/高亮（{len(hits)} 个节点）"


def _counter_near_selector(nodes: list[dict[str, Any]], anchor: str) -> Optional[int]:
    """在含 anchor 文案的节点上解析首个整数（P5 counter_delta）。"""
    anchor = str(anchor or "").strip()
    if not anchor:
        return None
    for node in nodes or []:
        if not isinstance(node, dict):
            continue
        blob = " ".join(
            str(node.get(k) or "") for k in ("text", "content_desc", "aria_label", "name", "label")
        )
        if anchor not in blob:
            continue
        m = _COUNTER_RE.search(blob)
        if m:
            try:
                return int(m.group(0))
            except ValueError:
                continue
    return None


@dataclass
class CheckEvidence:
    point_id: str
    kind: str
    status: str
    confidence: float
    executor: str
    summary: str
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class CheckVerdict:
    status: str
    confidence: float
    evidences: list[CheckEvidence] = field(default_factory=list)
    summary: str = ""


def _evidence(
    pt: CheckPoint,
    *,
    status: str,
    confidence: float,
    executor: str,
    summary: str,
    raw: dict[str, Any] | None = None,
) -> CheckEvidence:
    return CheckEvidence(
        point_id=pt.id,
        kind=pt.kind,
        status=status,
        confidence=confidence,
        executor=executor,
        summary=summary,
        raw=dict(raw or {}),
    )


def run_programmatic_checks(
    plan: CheckPlan,
    *,
    nodes: list[dict[str, Any]],
    nav_localized: dict[str, Any] | None,
    session_block: str = "",
    overlay_blocked: bool = False,
    cursor: Any = None,
) -> list[CheckEvidence]:
    out: list[CheckEvidence] = []
    loc = nav_localized if isinstance(nav_localized, dict) else {}
    chosen = str(loc.get("chosen") or "").strip()
    conf = float(loc.get("confidence") or 0.0)
    display = str(loc.get("display_name") or loc.get("label") or "").strip()

    for pt in plan.points:
        if pt.kind == "page_state":
            if chosen and conf >= 0.35:
                out.append(
                    _evidence(
                        pt,
                        status="pass",
                        confidence=min(0.9, conf + 0.1),
                        executor="nav_localize",
                        summary=f"当前逻辑页 {chosen}" + (f"（{display}）" if display else ""),
                        raw={"chosen": chosen, "confidence": conf},
                    )
                )
            else:
                out.append(
                    _evidence(
                        pt,
                        status="inconclusive",
                        confidence=conf,
                        executor="nav_localize",
                        summary="未能高置信定位逻辑页",
                        raw={"chosen": chosen, "confidence": conf},
                    )
                )
        elif pt.kind in ("element_exists", "hierarchy"):
            term = str(pt.natural_language or "").strip()
            from mino_nexus.loop.step_effect import (
                expected_profile_shape_config,
                probe_profile_shape_config,
            )

            if expected_profile_shape_config(plan.raw_expected or term):
                hit, kws = probe_profile_shape_config(plan.raw_expected or term, nodes)
                out.append(
                    _evidence(
                        pt,
                        status="pass" if hit else "fail",
                        confidence=0.93 if hit else 0.88,
                        executor="hierarchy",
                        summary=(
                            f"形象配置页命中 {','.join(kws[:4])}"
                            if hit
                            else "非形象配置向导（可能仍在相机/取景页）"
                        ),
                        raw={"keywords": kws},
                    )
                )
            else:
                conds = [{"text_contains": term}, {"content_desc_contains": term}]
                hit = bool(nodes and match_any(nodes, conds))
                out.append(
                    _evidence(
                        pt,
                        status="pass" if hit else "fail",
                        confidence=0.92 if hit else 0.85,
                        executor="hierarchy",
                        summary=f"屏上{'有' if hit else '无'}「{term[:24]}」",
                    )
                )
        elif pt.kind == "session":
            blob = str(session_block or "")
            nl = str(pt.natural_language or "")
            want_login = "登录成功" in nl or "已登录" in nl
            want_guest = "未登录" in nl or "弹出登录" in nl
            st = "inconclusive"
            if want_login:
                st = "pass" if ("logged_in" in blob.lower() or "已登录" in blob) else "fail"
            elif want_guest:
                st = "pass" if ("guest" in blob.lower() or "未登录" in blob) else "fail"
            out.append(
                _evidence(
                    pt,
                    status=st,
                    confidence=0.8 if st != "inconclusive" else 0.4,
                    executor="session_block",
                    summary=(blob or "无 session 块")[:120],
                )
            )
        elif pt.kind == "overlay":
            st = "fail" if overlay_blocked else "pass"
            out.append(
                _evidence(
                    pt,
                    status=st,
                    confidence=0.88,
                    executor="overlay",
                    summary="检测到系统挡屏/非目标前台" if overlay_blocked else "无挡屏信号",
                )
            )
        elif pt.kind in _VISUAL_CHECK_KINDS:
            hint = str(pt.natural_language or plan.raw_expected or "")[:200]
            out.append(
                _evidence(
                    pt,
                    status="inconclusive",
                    confidence=0.25,
                    executor="assert_visual",
                    summary=f"视觉校验点，需 assert_visual：{hint[:80]}",
                )
            )
        elif pt.kind == "selected_or_lit":
            st, conf, summ = _selected_or_lit_for_selector(nodes, str(pt.natural_language or ""))
            out.append(
                _evidence(
                    pt,
                    status=st,
                    confidence=conf,
                    executor="hierarchy",
                    summary=summ,
                )
            )
        elif pt.kind == "counter_delta":
            anchor = str(pt.natural_language or "").strip()
            key = str(pt.baseline_key or pt.id or "").strip() or pt.id
            need = int(pt.delta or 1)
            current = _counter_near_selector(nodes, anchor)
            if current is None:
                out.append(
                    _evidence(
                        pt,
                        status="inconclusive",
                        confidence=0.35,
                        executor="hierarchy",
                        summary=f"未在屏上找到与「{anchor[:20]}」相关的计数",
                    )
                )
            elif cursor is not None:
                store = getattr(cursor, "check_counter_baselines", None)
                if not isinstance(store, dict):
                    store = {}
                baseline = store.get(key)
                if baseline is None:
                    store[key] = current
                    setattr(cursor, "check_counter_baselines", store)
                    out.append(
                        _evidence(
                            pt,
                            status="inconclusive",
                            confidence=0.5,
                            executor="counter_delta",
                            summary=f"已记录基线 {current}（key={key}），同屏复验或下一步比对 delta≥{need}",
                            raw={"baseline": current, "key": key},
                        )
                    )
                elif current - int(baseline) >= need:
                    out.append(
                        _evidence(
                            pt,
                            status="pass",
                            confidence=0.9,
                            executor="counter_delta",
                            summary=f"计数 {baseline}→{current}，增量 {current - int(baseline)}≥{need}",
                            raw={"baseline": baseline, "current": current},
                        )
                    )
                else:
                    out.append(
                        _evidence(
                            pt,
                            status="fail",
                            confidence=0.85,
                            executor="counter_delta",
                            summary=f"计数 {baseline}→{current}，增量 {current - int(baseline)}<{need}",
                            raw={"baseline": baseline, "current": current},
                        )
                    )
            else:
                out.append(
                    _evidence(
                        pt,
                        status="inconclusive",
                        confidence=0.4,
                        executor="counter_delta",
                        summary=f"读到计数 {current}，无 cursor 基线，需 LLM 或二次观测",
                    )
                )
    return out


def synthesize_verdict(
    plan: CheckPlan,
    evidences: list[CheckEvidence],
    *,
    llm_self_pass: bool = False,
    llm_self_confidence: float = 0.0,
    fail_claim: bool = False,
) -> CheckVerdict:
    """§2.3 非对称：自证 pass 有条件；fail 需 required 工具 fail。"""
    required = [e for e in evidences if _is_required(plan, e.point_id)]
    tool_fail = [e for e in required if e.status == "fail"]
    tool_pass = required and all(e.status == "pass" for e in required)

    if fail_claim and tool_pass:
        return CheckVerdict(
            status="pass",
            confidence=0.85,
            evidences=evidences,
            summary="模型 fail_claim 被工具证据推翻",
        )
    if tool_fail:
        return CheckVerdict(
            status="fail",
            confidence=max(e.confidence for e in tool_fail),
            evidences=evidences,
            summary="；".join(e.summary for e in tool_fail[:4]),
        )
    if llm_self_pass and not plan.has_ambiguity and llm_self_confidence >= 0.72:
        return CheckVerdict(
            status="pass",
            confidence=llm_self_confidence,
            evidences=evidences,
            summary="模型自证通过（无疑义点）",
        )
    if tool_pass:
        return CheckVerdict(
            status="pass",
            confidence=sum(e.confidence for e in required) / max(1, len(required)),
            evidences=evidences,
            summary="工具校验点通过",
        )
    return CheckVerdict(
        status="inconclusive",
        confidence=0.4,
        evidences=evidences,
        summary="证据不足，需 assert_visual 或 ask_human",
    )


def _is_required(plan: CheckPlan, point_id: str) -> bool:
    for pt in plan.points:
        if pt.id == point_id:
            if pt.kind in _VISUAL_CHECK_KINDS:
                return False
            return bool(pt.required)
    return False


def hierarchy_fail_is_layout_prose(plan: CheckPlan, evidences: list[CheckEvidence]) -> bool:
    """层级里找不到预期原文时不算终裁，交给 agent-vision-assert。"""
    fails = [e for e in evidences if e.status == "fail" and _is_required(plan, e.point_id)]
    if not fails:
        return False
    for ev in fails:
        if ev.executor != "hierarchy" or "屏上无" not in str(ev.summary or ""):
            return False
    return True


def should_assert_visual_for_check(
    plan: CheckPlan,
    evidences: list[CheckEvidence],
    verdict: CheckVerdict,
) -> bool:
    """里程碑 check：程序点未齐或含视觉/歧义点时，走 assert_visual 兜底（§2.9）。"""
    if verdict.status == "pass" and verdict.confidence >= 0.72:
        return False
    for pt in plan.points:
        if pt.kind in _VISUAL_CHECK_KINDS and bool(pt.required):
            return True
    if plan.has_ambiguity:
        return True
    req_inconclusive = False
    for ev in evidences:
        if ev.status != "inconclusive":
            continue
        for pt in plan.points:
            if pt.id == ev.point_id and pt.kind not in _VISUAL_CHECK_KINDS and bool(pt.required):
                req_inconclusive = True
                break
    if req_inconclusive:
        return True
    if verdict.status == "inconclusive":
        return True
    if not plan.points and str(plan.raw_expected or "").strip():
        return True
    return False


def plan_for_step(expected: str, *, instruction: str = "") -> CheckPlan:
    return build_check_plan(expected, instruction=instruction)
