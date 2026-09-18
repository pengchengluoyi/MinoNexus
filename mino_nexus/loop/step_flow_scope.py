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
