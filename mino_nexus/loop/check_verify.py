"""check 阶段：程序校验点执行 + 终裁（非对称 pass/fail）。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from mino_nexus.loop.check_plan import CheckPlan, CheckPoint, build_check_plan
from mino_nexus.loop.hierarchy_slots import match_any


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
        elif pt.kind == "hierarchy":
            term = str(pt.natural_language or "").strip()
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
            return bool(pt.required) and pt.kind != "visual"
    return False


def plan_for_step(expected: str, *, instruction: str = "") -> CheckPlan:
    return build_check_plan(expected, instruction=instruction)
