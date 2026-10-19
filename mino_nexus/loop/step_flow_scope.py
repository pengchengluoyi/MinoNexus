"""逻辑块作用域：后续步骤才要求的登录弹窗，当前步/前置禁止走登录宏。"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Optional

from mino_nexus.loop.step_contract import instruction_allows_login_flow
from mino_nexus.loop.step_pointer import SeqNode

if TYPE_CHECKING:
    from mino_nexus.loop.step_pointer import StepCursor

_LOGIN_POPUP_RE = re.compile(
    r"弹出.{0,16}登录|半屏.{0,10}登录|拉起.{0,16}登录|登录弹窗|弹.{0,8}登录页",
    re.I,
)
_FULLSCREEN_LOGIN_EXPECT_RE = re.compile(r"全屏.{0,8}登录|进入.{0,12}登录页", re.I)


def prep_login_preservation_reason(ctx: object) -> str:
    """A frozen do-phase login cannot authorize preparing its target account early."""
    from mino_nexus.loop.step_contract import case_plan_has_login
    from mino_nexus.runtime.session_gate import session_prep_intent

    case = getattr(ctx, "case", None)
    scene = getattr(ctx, "case_scene", None)
    if (not isinstance(case, dict) or not case_plan_has_login(case)
            or session_prep_intent(scene if isinstance(scene, dict) else None) == "relogin"):
        return ""
    return (
        "【初始会话门槛】原操作计划包含登录，且前置未声明重新登录；"
        "目标租约账号必须留到原登录步骤使用，不能在前置提前登录、配置或消费其业务初态。"
        "请准备满足前置的独立初始登录会话；若必须前置重登，须明确其账号角色与重登合同。"
    )


def prep_initial_session_block_reason(ctx: object, session: str) -> str:
    """Block only a known missing prep session; do-phase login and read-only probes keep their scope."""
    from mino_nexus.runtime.session_gate import required_session

    cursor = getattr(ctx, "_step_cursor", None)
    scene = getattr(ctx, "case_scene", None)
    if (str(getattr(cursor, "phase", "") or "") != "prep"
            or required_session(scene=scene if isinstance(scene, dict) else None) != "logged_in"
            or session not in {"logged_out", "guest"}):
        return ""
    return prep_login_preservation_reason(ctx)


def step_text_requires_login_popup(text: str) -> bool:
    """半屏/弹出类登录预期；全屏登录页不算「延后弹窗」。"""
    raw = str(text or "").strip()
    if not raw:
        return False
    if _FULLSCREEN_LOGIN_EXPECT_RE.search(raw) and not _LOGIN_POPUP_RE.search(raw):
        return False
    return bool(_LOGIN_POPUP_RE.search(raw))


def first_login_popup_step(nodes: list[SeqNode]) -> Optional[int]:
    best: Optional[int] = None
    for node in nodes or []:
        for blob in (node.instruction, node.expected):
            if step_text_requires_login_popup(blob):
                n = int(node.n)
                if best is None or n < best:
                    best = n
                break
    return best


def login_flow_allowed(
    *,
    cursor: StepCursor,
    phase: str,
    instruction: str,
    expected: str,
) -> tuple[bool, str]:
    """是否允许登录宏 / 发码 / 自动点登录 / LLM 走登录链。"""
    popup_n = first_login_popup_step(list(getattr(cursor, "nodes", None) or []))
    ph = str(phase or "").strip().lower()
    cur = cursor.current()
    cur_n = int(cur.n) if cur and ph == "do" else 0

    if instruction_allows_login_flow(instruction, login_module_case=False):
        return True, ""

    if popup_n is None:
        return True, ""

    if ph == "prep":
        return False, (
            f"步骤 {popup_n} 才要求弹出/半屏登录；"
            "前置阶段请勿登录、勿发验证码、勿点「我的」进登录页收工。"
        )

    if cur_n < popup_n:
        return False, (
            f"步骤 {popup_n} 才要求登录弹窗；"
            f"本步（{cur_n}）请只做 instruction，勿登录、勿发码、勿点登录按钮。"
        )

    if cur_n == popup_n:
        return True, ""

    return True, ""


def login_flow_block_message(
    *,
    cursor: StepCursor,
    phase: str,
    instruction: str,
    expected: str,
) -> str:
    allowed, msg = login_flow_allowed(
        cursor=cursor,
        phase=phase,
        instruction=instruction,
        expected=expected,
    )
    return "" if allowed else str(msg or "").strip()
