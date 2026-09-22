"""无底栏 onboarding 墙：FSM Tab 导航不可用时的开环降级（结构推断，不写 App 词表）。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

from mino_nexus.loop.step_nav_plan import instruction_nav_target

FSM_OPEN_LOOP_ERRORS = frozenset(
    {
        "tab_not_visible_no_back_edge",
        "back_not_on_route",
        "missing tap params",
        "missing from_state",
    }
)

_OPEN_LOOP_HINT = (
    "【导航开环·无底栏 onboarding】当前屏看不见底栏 Tab，路线图 Tab 跳转已拒绝。"
    "禁止对同一 Tab 目标重复 fsm_navigate。"
    "请用 tap_element 点 hierarchy 中主流程按钮（短文案、可点、靠近内容区底部或主 CTA），"
    "必要时 press_key BACK（仅当 recovery 已允许 BACK）；"
    "出墙出现底栏后再 fsm_navigate / 点 Tab。"
)


@dataclass(frozen=True)
class NavOpenLoopState:
    active: bool
    reason: str = ""
    blocked_nav_target: str = ""


def _fold(text: str) -> str:
    return re.sub(r"[\s_·\-]+", "", str(text or "").strip().lower())


def precondition_expects_avatar_unconfigured(precondition: str) -> bool:
    pre = str(precondition or "")
    if re.search(r"未配置形象", pre, re.I):
        return True
    return bool(re.search(r"形象.{0,6}未", pre, re.I))


def bottom_tab_slot_count(ctx: Any) -> int:
    nodes = getattr(ctx, "nav_hierarchy_nodes", None) if ctx is not None else None
    if not isinstance(nodes, list) or not nodes:
        return 0
    from mino_nexus.services.nav_tab_slots import find_bottom_tab_slots

    return len(find_bottom_tab_slots(nodes))


def should_consider_onboarding_wall(
    *,
    ctx: Any,
    precondition: str,
    session_fact_session: str = "",
) -> bool:
    if bottom_tab_slot_count(ctx) >= 2:
        return False
    sess = str(session_fact_session or "").strip().lower()
    if not sess:
        fact = getattr(ctx, "session_fact", None) or {}
        sess = str(fact.get("session") or "").strip().lower()
    if sess not in ("logged_in", "registered"):
        if not re.search(r"已登录", str(precondition or ""), re.I):
            return False
    return precondition_expects_avatar_unconfigured(precondition) or bool(
        getattr(ctx, "nav_open_loop_active", False)
    )


def static_onboarding_wall_nav_hint(*, precondition: str, instruction: str) -> str:
    """步骤要点 Tab 且前置未配置形象时，预先提示开环（不依赖 ctx）。"""
    from mino_nexus.loop.step_intent import instruction_required_intents

    if not precondition_expects_avatar_unconfigured(precondition):
        return ""
    if "nav_tab" not in instruction_required_intents(instruction) and not instruction_nav_target(
        instruction
    ):
        return ""
    return _OPEN_LOOP_HINT


def open_loop_state(ctx: Any) -> NavOpenLoopState:
    active = bool(getattr(ctx, "nav_open_loop_active", False))
    return NavOpenLoopState(
        active=active,
        reason=str(getattr(ctx, "nav_open_loop_reason", "") or ""),
        blocked_nav_target=str(getattr(ctx, "nav_open_loop_blocked_target", "") or ""),
    )


def note_fsm_degrade_for_open_loop(
    ctx: Any,
    *,
    raw_resp: dict[str, Any],
    nav_target_hint: str = "",
) -> None:
    """fsm_navigate declined（fsm_degraded）后激活开环模式。"""
    if str(raw_resp.get("local_reason") or "") != "fsm_degraded":
        return
    err = str(raw_resp.get("error") or "").strip()
    nav_attempt = raw_resp.get("nav_attempt")
    if isinstance(nav_attempt, dict):
        err = err or str(nav_attempt.get("plan_error") or "")
    if err and err not in FSM_OPEN_LOOP_ERRORS:
        if err not in ("same_page_repeat",):
            return
    if err in FSM_OPEN_LOOP_ERRORS or err == "same_page_repeat":
        setattr(ctx, "nav_open_loop_active", True)
        setattr(ctx, "nav_open_loop_reason", err or "fsm_degraded")
        to_raw = nav_target_hint
        if not to_raw and isinstance(nav_attempt, dict):
            to_raw = str(nav_attempt.get("resolved_to") or nav_attempt.get("to") or "")
        if to_raw:
            setattr(ctx, "nav_open_loop_blocked_target", _fold(to_raw))


def format_open_loop_hint(
    *,
    ctx: Any,
    precondition: str,
    instruction: str = "",
) -> str:
    if not should_consider_onboarding_wall(ctx=ctx, precondition=precondition):
        st = open_loop_state(ctx)
        if not st.active:
            return ""
    else:
        setattr(ctx, "nav_open_loop_active", True)
    parts = [_OPEN_LOOP_HINT]
    st = open_loop_state(ctx)
    if st.blocked_nav_target and instruction:
        want = _fold(instruction_nav_target(instruction) or instruction)
        if want and want == _fold(st.blocked_nav_target):
            parts.append(
                f"【导航开环】步骤目标「{instruction_nav_target(instruction) or instruction[:16]}」"
                f"在当前 onboarding 墙内不可用；请先出墙再执行该 Tab 步骤。"
            )
    return "\n".join(parts)


def fsm_params_blocked_in_open_loop(ctx: dict[str, Any]) -> Optional[str]:
    if not bool(ctx.get("nav_open_loop_active")):
        return None
    cap = str(ctx.get("cap_id") or "")
    if cap not in ("fsm_navigate", "recover_fsm_navigate"):
        return None
    params = ctx.get("params") if isinstance(ctx.get("params"), dict) else {}
    to_state = _fold(str(params.get("to_state") or params.get("to") or ""))
    oral = _fold(str(params.get("selector_text") or params.get("text") or ""))
    blocked = _fold(str(ctx.get("nav_open_loop_blocked_target") or ""))
    if not blocked and not to_state and not oral:
        return (
            "【导航开环】无底栏 onboarding 墙内禁止 fsm_navigate。"
            "请 tap_element 沿主流程出墙后再导航。"
        )
    if blocked and (to_state == blocked or oral == blocked or blocked in to_state or blocked in oral):
        return (
            "【导航开环】已拒绝重复 fsm_navigate：同一 Tab 目标在 onboarding 墙内不可达。"
            "请 tap_element 完成屏上主流程，勿再 fsm。"
        )
    return None


def clear_open_loop_if_tabs_visible(ctx: Any) -> None:
    if bottom_tab_slot_count(ctx) >= 2:
        setattr(ctx, "nav_open_loop_active", False)
        setattr(ctx, "nav_open_loop_reason", "")
        setattr(ctx, "nav_open_loop_blocked_target", "")
