"""未登录 + 点 Tab 触发登录页：导航分叉（图上 Tab 终态 vs 本步 expected 登录页）。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

from mino_nexus.loop.step_contract import instruction_allows_login_flow
from mino_nexus.loop.step_intent import instruction_required_intents
from mino_nexus.loop.step_nav_plan import instruction_nav_target
from mino_nexus.services.nav_route import click_label_from_nav_ref

_LOGIN_DEST_EXPECT_RE = re.compile(
    r"登录页|登录界面|验证码登录|全屏.{0,8}登录|进入.{0,12}登录",
    re.I,
)
_PROFILE_DEST_EXPECT_RE = re.compile(r"个人页|个人中心|我的主页", re.I)
_LOGIN_SCREEN_LOCALIZED_RE = re.compile(r"登录", re.I)


@dataclass(frozen=True)
class GuestTabLoginFork:
    """本步：点 Tab 后应落到登录页，而非已登录 Tab 终态。"""

    tab_label: str
    expected_ref: str


def _session_is_guest(required_session: str) -> bool:
    rs = str(required_session or "").strip().lower()
    return rs in ("guest", "logged_out")


def expected_indicates_login_destination(expected: str) -> bool:
    raw = str(expected or "").strip()
    if not raw:
        return False
    if _PROFILE_DEST_EXPECT_RE.search(raw) and not _LOGIN_DEST_EXPECT_RE.search(raw):
        return False
    return bool(_LOGIN_DEST_EXPECT_RE.search(raw))


def detect_guest_tab_login_fork(
    *,
    instruction: str,
    expected: str,
    required_session: str = "any",
) -> Optional[GuestTabLoginFork]:
    """未登录点 Tab、本步验收登录页（非个人页）。"""
    if not _session_is_guest(required_session):
        return None
    if instruction_allows_login_flow(instruction, login_module_case=False):
        return None
    need = instruction_required_intents(instruction)
    if "nav_tab" not in need and not instruction_nav_target(instruction):
        return None
    if not expected_indicates_login_destination(expected):
        return None
    tab = instruction_nav_target(instruction) or click_label_from_nav_ref(instruction)
    if not tab:
        return None
    exp_ref = _expected_nav_ref(expected)
    if not exp_ref:
        return None
    return GuestTabLoginFork(tab_label=str(tab).strip(), expected_ref=exp_ref)


def _expected_nav_ref(expected: str) -> str:
    raw = str(expected or "").strip()
    if not raw:
        return ""
    for chunk in re.split(r"[、,/；;|｜]", raw):
        c = str(chunk or "").strip()
        if _LOGIN_DEST_EXPECT_RE.search(c):
            return c
    if _LOGIN_DEST_EXPECT_RE.search(raw):
        return raw
    return ""


def step_nav_plan_to_ref(
    *,
    instruction: str,
    expected: str = "",
    required_session: str = "any",
) -> str:
    fork = detect_guest_tab_login_fork(
        instruction=instruction,
        expected=expected,
        required_session=required_session,
    )
    if fork:
        return fork.expected_ref
    return instruction_nav_target(instruction)


def format_guest_tab_fork_hint(fork: GuestTabLoginFork) -> str:
    return (
        f"【导航分叉·未登录】本步点 Tab「{fork.tab_label}」后应进入「{fork.expected_ref}」，"
        f"不是已登录个人页。到位后 signal_done；"
        f"禁止 fsm_navigate 目标「{fork.tab_label}」/个人页，勿重复点 Tab。"
    )


def hierarchy_indicates_login_screen(nodes: list[dict[str, Any]] | None) -> bool:
    if not nodes:
        return False
    from mino_nexus.loop.ui_sms_request import (
        find_phone_field,
        find_send_code_button,
        phone_field_filled,
    )

    phone = find_phone_field(nodes)
    if phone is not None:
        if find_send_code_button(nodes, phone) is not None:
            return True
        if phone_field_filled(nodes):
            return True
    from mino_nexus.loop.ui_consent import find_consent_control

    if find_consent_control(nodes) is not None:
        from mino_nexus.loop.ui_consent import any_focused_input

        if any_focused_input(nodes):
            return True
    return False


def localized_indicates_login_screen(localized: dict[str, Any] | None) -> bool:
    loc = localized if isinstance(localized, dict) else {}
    try:
        conf = float(loc.get("confidence") or 0)
    except (TypeError, ValueError):
        conf = 0.0
    if conf < 0.35:
        return False
    label = str(loc.get("display_name") or loc.get("label") or "").strip()
    return bool(label and _LOGIN_SCREEN_LOCALIZED_RE.search(label))


def on_login_destination_screen(
    *,
    hierarchy_nodes: list[dict[str, Any]] | None,
    localized: dict[str, Any] | None,
) -> bool:
    return hierarchy_indicates_login_screen(hierarchy_nodes) or localized_indicates_login_screen(
        localized
    )


def login_flow_interrupt_allowed_for_step(
    *,
    instruction: str,
    expected: str,
    required_session: str = "any",
) -> bool:
    """仅进入登录页的分叉步：不跑 fb.global.login 宏（下一步才发码）。"""
    return detect_guest_tab_login_fork(
        instruction=instruction,
        expected=expected,
        required_session=required_session,
    ) is None


def _fold_label(text: str) -> str:
    return re.sub(r"[\s_·\-]+", "", str(text or "").strip().lower())


def fsm_goal_conflicts_with_guest_fork(
    *,
    fork: GuestTabLoginFork,
    to_raw: str,
    hierarchy_nodes: list[dict[str, Any]] | None,
    localized: dict[str, Any] | None,
) -> tuple[bool, str]:
    """已在登录页却仍以 Tab/个人页为 fsm 目标 → 应拒绝规划。"""
    if not on_login_destination_screen(hierarchy_nodes=hierarchy_nodes, localized=localized):
        return False, ""
    want = _fold_label(to_raw)
    tab = _fold_label(fork.tab_label)
    exp = _fold_label(fork.expected_ref)
    if not want:
        return False, ""
    if want in (tab,) or tab in want:
        return True, format_guest_tab_fork_hint(fork)
    if "个人" in to_raw or "我的页" in to_raw:
        return True, format_guest_tab_fork_hint(fork)
    if exp and want not in exp and "登录" not in to_raw:
        # 口语目标仍是 Tab 名或个人页类
        from mino_nexus.services.nav_route import click_label_from_nav_ref

        oral = _fold_label(click_label_from_nav_ref(to_raw) or to_raw)
        if oral == tab:
            return True, format_guest_tab_fork_hint(fork)
    return False, ""


def fsm_blocked_logged_in_session_drift(
    *,
    required_session: str,
    instruction: str,
    expected: str,
    to_raw: str,
    hierarchy_nodes: list[dict[str, Any]] | None,
    localized: dict[str, Any] | None,
) -> tuple[bool, str]:
    """前置已登录、本步验收个人页，却在登录屏 — 禁止 fsm 回 Tab/我的（防 fuse 连坐）。"""
    rs = str(required_session or "").strip().lower()
    if rs != "logged_in":
        return False, ""
    exp = str(expected or "")
    instr = str(instruction or "")
    if not _PROFILE_DEST_EXPECT_RE.search(exp) and "我的" not in instr:
        return False, ""
    if not on_login_destination_screen(hierarchy_nodes=hierarchy_nodes, localized=localized):
        return False, ""
    want = _fold_label(to_raw)
    tab = _fold_label(click_label_from_nav_ref(instr) or "我的")
    if want and (want == tab or tab in want or "个人" in to_raw or "我的" in to_raw):
        msg = (
            "【登录态漂移】前置要求已登录，但当前在登录页；"
            "无法通过 fsm 进入「我的/个人页」。请先 recover 或确认设备登录态与租号账号。"
        )
        return True, msg
    return False, ""


def required_session_from_scene(scene: dict[str, Any] | None) -> str:
    from mino_nexus.runtime.session_gate import required_session

    return required_session(scene=scene if isinstance(scene, dict) else None)
