"""登录链末段：OTP 已齐后程序点「登录」，避免 LLM snap 空烧步数。"""
from __future__ import annotations

import re
from typing import Any, Optional

from mino_nexus.core.schemas import PlanEvent
from mino_nexus.loop.hierarchy_slots import match_any
from mino_nexus.loop.step_contract import instruction_allows_login_flow
from mino_nexus.loop.step_intent import instruction_required_intents, step_intents_satisfied


def _history_has(history: list[str], cap: str, status: str = "pass") -> bool:
    needle = f"{cap} → {status}"
    return any(needle in line for line in (history or []))


def try_auto_login_submit(
    proxy: Any,
    ctx: Any,
    *,
    turn_seq: int,
    instruction: str,
    hierarchy_nodes: list[dict[str, Any]] | None,
    intents_done: set[str] | None,
    history_lines: list[str],
    login_module_case: bool = False,
) -> Optional[dict[str, Any]]:
    """发码/填码已完成且 instruction 含登录时，自动 tap 登录按钮。"""
    if not instruction_allows_login_flow(instruction, login_module_case=login_module_case):
        return None
    need = instruction_required_intents(instruction)
    if "login_flow" not in need:
        return None
    ok, _ = step_intents_satisfied(instruction=instruction, intents_done=intents_done)
    if ok:
        return None
    done = set(intents_done or set())
    otp_ok = "otp_fill" in done or _history_has(history_lines, "get_otp")
    phone_ok = "login_phone" in done or _history_has(history_lines, "input_text")
    if not otp_ok or not phone_ok:
        return None
    nodes = list(hierarchy_nodes or [])
    conds = [
        {"text_equals": "登录"},
        {"content_desc_equals": "登录"},
        {"text_contains": "登录"},
    ]
    if not nodes or not match_any(nodes, conds):
        return None
    event = PlanEvent(
        seq=int(turn_seq),
        capability_id="tap_element",
        event_kind="tap_element",
        params={"selector_text": "登录", "content_desc": "登录"},
        ai_reasoning="程序：验证码已填，自动点击登录提交",
        label="程序·登录提交",
    )
    from mino_nexus.core.protocol import EventStatus
    from mino_nexus.loop.local_executors import dispatch_local

    res = dispatch_local(
        event,
        ctx=ctx,
        router=proxy,
        target_package=str(getattr(ctx, "target_package", "") or ""),
    )
    st = res.status.value if hasattr(res.status, "value") else str(res.status)
    ok_st = str(st) in ("pass", EventStatus.PASS.value)
    return {
        "ok": ok_st,
        "status": st,
        "summary": str(res.summary or res.error or "tap_element"),
        "capability_id": "tap_element",
    }
