"""邮箱登录：Email 标签后程序填邮箱 + 发码，降低 LLM 反复点 Email tab。"""
from __future__ import annotations

import re
from typing import Any, Optional

from mino_nexus.core.schemas import PlanEvent
from mino_nexus.loop.step_contract import instruction_allows_login_flow
from mino_nexus.services.otp_resolve import login_kind_from_secrets


def _login_kind(ctx: Any) -> str:
    from mino_nexus.loop.local_executors import _otp_context

    _doc, secrets, _otp = _otp_context(ctx)
    return login_kind_from_secrets(secrets)


def _history_has(history: list[str], cap: str, status: str = "pass") -> bool:
    needle = f"{cap} → {status}"
    return any(needle in line for line in (history or []))


def _email_tab_selected(history: list[str]) -> bool:
    for line in history or []:
        if "tap_element" not in line or "pass" not in line.lower():
            continue
        if re.search(r"\bEmail\b|邮箱", line, re.I):
            return True
    return False


def _email_input_done(history: list[str], intents_done: set[str] | None) -> bool:
    done = set(intents_done or set())
    if "login_email" in done:
        return True
    for line in history or []:
        if "input_text" not in line or "pass" not in line.lower():
            continue
        if re.search(r"field=email|邮箱|@", line, re.I):
            return True
    return False


def _lease_email(ctx: Any) -> str:
    acc = dict(getattr(ctx, "picked_account", None) or {})
    for key in ("email", "login_email"):
        val = str(acc.get(key) or "").strip()
        if val and "@" in val:
            return val
    return ""


def _dispatch_step(
    proxy: Any,
    ctx: Any,
    *,
    turn_seq: int,
    cap: str,
    params: dict[str, Any],
    label: str,
) -> dict[str, Any]:
    from mino_nexus.core.protocol import EventStatus
    from mino_nexus.loop.local_executors import _dispatch_device, dispatch_local

    if cap in ("get_otp", "request_sms_code"):
        event = PlanEvent(
            seq=int(turn_seq),
            capability_id=cap,
            event_kind=cap,
            params=params,
            ai_reasoning=label,
            label=label,
        )
        res = dispatch_local(
            event,
            ctx=ctx,
            router=proxy,
            target_package=str(getattr(ctx, "target_package", "") or ""),
        )
    else:
        res = _dispatch_device(
            proxy,
            ctx=ctx,
            seq=int(turn_seq),
            cap=cap,
            params=params,
            label=label,
        )
    st = res.status.value if hasattr(res.status, "value") else str(res.status)
    ok = str(st) in ("pass", EventStatus.PASS.value)
    return {
        "ok": ok,
        "status": st,
        "summary": str(res.summary or res.error or cap),
        "capability_id": cap,
        "params": dict(params),
    }


def run_email_login_preflight(
    proxy: Any,
    ctx: Any,
    *,
    turn_seq: int,
    instruction: str,
    intents_done: set[str] | None,
    history_lines: list[str],
    login_module_case: bool = False,
) -> list[dict[str, Any]]:
    """Email 登录页：填邮箱 → request_sms_code。返回与 login_submit 链相同的摘要列表。"""
    if _login_kind(ctx) != "email":
        return []
    if not instruction_allows_login_flow(instruction, login_module_case=login_module_case):
        return []
    if not _email_tab_selected(history_lines):
        return []
    out: list[dict[str, Any]] = []
    email = _lease_email(ctx)
    if not email:
        return []

    if not _email_input_done(history_lines, intents_done):
        fill = _dispatch_step(
            proxy,
            ctx,
            turn_seq=turn_seq,
            cap="input_text",
            params={"field": "email", "text": email},
            label="程序：Email 标签已选，自动填入租号邮箱",
        )
        out.append(fill)
        if not fill.get("ok"):
            return out

    if _history_has(history_lines, "request_sms_code"):
        return out
    if "sms_send" in set(intents_done or set()):
        return out

    send = _dispatch_step(
        proxy,
        ctx,
        turn_seq=turn_seq,
        cap="request_sms_code",
        params={},
        label="程序：邮箱已填，自动 request_sms_code",
    )
    out.append(send)
    return out
