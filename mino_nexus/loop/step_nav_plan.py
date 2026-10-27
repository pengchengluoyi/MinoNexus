"""用例单步导航计划（assist 级摘要，不替代 fsm_navigate 每 turn 重规划）。"""
from __future__ import annotations

import re
from typing import Any

from mino_nexus.navigation.atlas.nav_route import click_label_from_nav_ref

_NAV_TARGET_RE = re.compile(
    r"(?:进入|打开|切换到|跳转至|前往|切到|点到)"
    r"(?:底部|上面|中间)?"
    r"(?:Tab|tab|标签)?"
    r"[「『\"“]?([^」』\"”\n，。；;]{1,24})[」』\"”]?",
)


def instruction_nav_target(instruction: str, expected: str = "") -> str:
    """从步骤原文/预期抽导航目标口语（不写 App 词表）。"""
    text = str(instruction or "").strip()
    if not text and not str(expected or "").strip():
        return ""
    from mino_nexus.navigation.entries.text import parse_step

    for raw in (text, str(expected or "")):
        if not raw:
            continue
        parsed = parse_step(raw)
        target = str(parsed.get("target") or "").strip()
        if target:
            return target
    if not text:
        return ""
    quoted = click_label_from_nav_ref(text)
    if quoted:
        return quoted
    m = _NAV_TARGET_RE.search(text)
    if m:
        return str(m.group(1) or "").strip()
    return ""


def step_needs_nav_plan(instruction: str, expected: str = "") -> bool:
    from mino_nexus.loop.step_intent import instruction_required_intents

    need = instruction_required_intents(instruction)
    if "nav_tab" in need:
        return True
    return bool(instruction_nav_target(instruction, expected))


def build_step_nav_plan_hint(
    *,
    instruction: str,
    app_id: str,
    project_id: str = "",
    localized: dict[str, Any] | None = None,
    app_version: str = "",
    expected: str = "",
    required_session: str = "any",
    precondition: str = "",
    ctx: Any = None,
) -> str:
    """规划最短路并格式化为 prompt 一行；失败则查页面入口档案。"""
    from mino_nexus.loop.nav_session_fork import (
        detect_guest_tab_login_fork,
        format_guest_tab_fork_hint,
        on_login_destination_screen,
        step_nav_plan_to_ref,
    )
    from mino_nexus.loop.nav_onboarding_open_loop import static_onboarding_wall_nav_hint

    fork = detect_guest_tab_login_fork(
        instruction=instruction,
        expected=expected,
        required_session=required_session,
    )
    to_ref = step_nav_plan_to_ref(
        instruction=instruction,
        expected=expected,
        required_session=required_session,
    )
    if not to_ref:
        from mino_nexus.loop.step_intent import instruction_required_intents

        to_ref = instruction_nav_target(instruction, expected)
        if not to_ref and "nav_tab" not in instruction_required_intents(instruction):
            return ""
        if not to_ref:
            to_ref = instruction.strip()[:24]

    if not str(app_id or "").strip():
        return "【本步导航】缺少 app_id，无法查路线图；请按当前导航计划，用 tap_element 点击可见入口进入目标页。"

    from mino_nexus.navigation.atlas import nav_route
    from mino_nexus.navigation.atlas.nav_state_resolve import plan_route_resolved

    fsm_doc, _ = nav_route.load_fsm_doc(
        str(app_id),
        project_id=str(project_id or ""),
        use_live=False,
        app_version=str(app_version or ""),
    )
    fsm = fsm_doc or {}
    if not fsm:
        from mino_nexus.navigation.entries.plan import hint_for_step

        entry_hint = hint_for_step(
            app_id=str(app_id),
            project_id=str(project_id or ""),
            ctx=ctx,
            instruction=instruction,
            expected=expected,
        )
        if entry_hint:
            return entry_hint
        return "【本步导航·入口缺口】尚未配置 NavFSM，也没有目标页入口；禁止在页面上盲点，只允许返回/等待或补入口。"

    loc = localized if isinstance(localized, dict) else {}
    chosen = str(loc.get("chosen") or "").strip()
    from_ref = chosen if chosen else ""

    fork_line = format_guest_tab_fork_hint(fork) if fork else ""
    open_pre = static_onboarding_wall_nav_hint(
        precondition=precondition,
        instruction=instruction,
    )
    if open_pre:
        fork_line = f"{fork_line}\n{open_pre}".strip() if fork_line else open_pre

    plan = plan_route_resolved(fsm, from_ref=from_ref, to_ref=to_ref, localized=loc)
    if plan.get("ok"):
        hops = int(plan.get("hop_count") or 0)
        summary = str(plan.get("summary") or "").strip()
        to_st = str(plan.get("to_state") or to_ref)
        if fork:
            if hops <= 0 and on_login_destination_screen(
                hierarchy_nodes=None,
                localized=loc,
            ):
                return (
                    f"{fork_line}【本步导航】已在登录相关屏 {to_st}；"
                    f"若 expected 已满足请 signal_done。"
                )
            if hops <= 0:
                return (
                    f"{fork_line}【本步导航】先 tap Tab「{fork.tab_label}」进入登录页，"
                    f"勿继续导航到个人页。"
                )
            return (
                f"{fork_line}【本步导航】目标「{to_ref}」→ {to_st}，图上 {hops} 步：{summary}。"
                f"到位后 signal_done。"
            )
        if hops <= 0:
            return f"【本步导航】已在或接近目标页 {to_st}；若仍须点入口请用 tap_element。"
        return (
            f"【本步导航】目标「{to_ref}」→ {to_st}，图上 {hops} 步：{summary}。"
            f"请按当前导航计划，用 tap_element / press_key 每轮执行一步，未到目标勿 signal_done。"
        )
    err = str(plan.get("error") or "无连通 nav 边").strip()
    from mino_nexus.navigation.entries.plan import hint_for_step

    entry_hint = hint_for_step(
        app_id=str(app_id),
        project_id=str(project_id or ""),
        ctx=ctx,
        instruction=instruction,
        expected=expected,
    )
    if entry_hint:
        return entry_hint
    prefix = f"{fork_line}" if fork else ""
    if fork:
        return (
            f"{prefix}【本步导航·分叉】图上未标注登录页节点时："
            f"请 tap Tab「{fork.tab_label}」，屏上出现登录控件后 signal_done。"
        )
    return (
        f"{prefix}【本步导航·入口缺口】{err}。"
        f"没有可执行的入口档案，禁止盲点；只允许返回/等待或补入口后再试。"
    )
