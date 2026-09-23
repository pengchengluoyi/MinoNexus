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


def _sms_send_done(history: list[str], intents_done: set[str] | None) -> bool:
    done = set(intents_done or set())
    if "sms_send" in done or _history_has(history, "request_sms_code"):
        return True
    for line in history or []:
        if "tap_element" not in line or "pass" not in line.lower():
            continue
        if re.search(r"发送验证码|重新发送|获取验证码", line):
            return True
    return False


def _history_sms_code_filled(history: list[str]) -> bool:
    for line in history or []:
        if "input_text" not in line or "pass" not in line.lower():
            continue
        if re.search(r"验证码|sms|otp", line, re.I):
            return True
    return False


def _lines_after_sms_sent(history: list[str]) -> list[str]:
    idx = -1
    for i, line in enumerate(history or []):
        if "request_sms_code" in line and "pass" in line.lower():
            idx = i
        if "tap_element" in line and "pass" in line.lower():
            if re.search(r"发送验证码|重新发送|获取验证码", line):
                idx = i
    if idx < 0:
        return []
    return list(history[idx + 1 :])


def _otp_code_already_entered(
    history: list[str],
    intents_done: set[str] | None,
) -> bool:
    """发码之后验证码是否已填入（勿把发码前的「输入 11 字」手机号当成验证码）。"""
    done = set(intents_done or set())
    if "otp_fill" in done:
        return True
    after_sms = _lines_after_sms_sent(history)
    if not after_sms:
        return False
    for line in after_sms:
        if "input_text" not in line or "pass" not in line.lower():
            continue
        if re.search(r"验证码|sms|otp", line, re.I):
            return True
        m = re.search(r"输入\s*(\d+)\s*字", line)
        if m:
            n = int(m.group(1))
            # 短信验证码通常 4–8 位；11 位是手机号输入摘要
            if 4 <= n <= 8:
                return True
    if _history_sms_code_filled(after_sms):
        return True
    return False


def try_auto_otp_fill(
    proxy: Any,
    ctx: Any,
    *,
    turn_seq: int,
    instruction: str,
    intents_done: set[str] | None,
    history_lines: list[str],
    login_module_case: bool = False,
) -> Optional[dict[str, Any]]:
    """发码成功后程序 get_otp + 填入验证码，避免同屏重复 get_otp 触发熔断。"""
    if not instruction_allows_login_flow(instruction, login_module_case=login_module_case):
        return None
    if not _sms_send_done(history_lines, intents_done):
        return None
    if _otp_code_already_entered(history_lines, intents_done):
        return None
    done = set(intents_done or set())
    if "otp_fill" in done:
        return None

    from mino_nexus.core.protocol import EventStatus
    from mino_nexus.loop.local_executors import _dispatch_device, dispatch_local

    if not _history_has(history_lines, "get_otp"):
        res = dispatch_local(
            PlanEvent(
                seq=int(turn_seq),
                capability_id="get_otp",
                event_kind="get_otp",
                params={},
                ai_reasoning="程序：已发码，自动 get_otp",
                label="程序·取验证码",
            ),
            ctx=ctx,
            router=proxy,
            target_package=str(getattr(ctx, "target_package", "") or ""),
        )
        st = res.status.value if hasattr(res.status, "value") else str(res.status)
        if str(st) not in ("pass", EventStatus.PASS.value):
            return {
                "ok": False,
                "status": st,
                "summary": str(res.summary or res.error or "get_otp"),
                "capability_id": "get_otp",
                "ran_get_otp": False,
            }
        ran_get_otp = True
    else:
        ran_get_otp = False

    acc = dict(getattr(ctx, "picked_account", None) or {})
    code = str(acc.get("otp") or acc.get("sms_code") or "").strip()
    if not code:
        from mino_nexus.loop.local_executors import _resolve_otp

        code, _ = _resolve_otp(ctx)
    if not code:
        return None

    # 发码后 UI/短信通道略慢，稍等再填验证码框（仍在本 turn 程序链内，不走 LLM）
    if any("request_sms_code" in line and "pass" in line.lower() for line in (history_lines or [])[-6:]):
        import time

        time.sleep(0.65)

    res_in = _dispatch_device(
        proxy,
        ctx=ctx,
        seq=int(turn_seq),
        cap="input_text",
        params={"field": "sms_code", "text": code},
        label="程序：OTP 已取，自动填入验证码框",
    )
    st_in = res_in.status.value if hasattr(res_in.status, "value") else str(res_in.status)
    ok_in = str(st_in) in ("pass", EventStatus.PASS.value)
    return {
        "ok": ok_in,
        "status": st_in,
        "summary": str(res_in.summary or res_in.error or "input_text"),
        "capability_id": "input_text",
        "ran_get_otp": ran_get_otp,
    }


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
    if "login_flow" not in need and not re.search(r"点击.{0,8}登录|登录按钮", instruction):
        return None
    ok, _ = step_intents_satisfied(instruction=instruction, intents_done=intents_done)
    if ok:
        return None
    done = set(intents_done or set())
    otp_ok = _otp_code_already_entered(history_lines, done)
    cred_ok = (
        "login_phone" in done
        or "login_email" in done
        or _history_has(history_lines, "input_text")
    )
    if not otp_ok or not cred_ok:
        return None
    attempts = int(getattr(ctx, "login_auto_submit_attempts", 0) or 0)
    if attempts >= 2:
        return None
    nodes = list(hierarchy_nodes or [])
    conds = [
        {"text_equals": "登录"},
        {"content_desc_equals": "登录"},
        {"text_contains": "登录"},
        {"text_contains": "立即登录"},
        {"content_desc_contains": "登录"},
    ]
    if not nodes or not match_any(nodes, conds):
        return None
    from mino_nexus.core.protocol import EventStatus
    from mino_nexus.loop.local_executors import _dispatch_device

    res = _dispatch_device(
        proxy,
        ctx=ctx,
        seq=int(turn_seq),
        cap="tap_element",
        params={"selector_text": "登录", "content_desc": "登录"},
        label="程序：验证码已填，自动点击登录提交",
    )
    st = res.status.value if hasattr(res.status, "value") else str(res.status)
    ok_st = str(st) in ("pass", EventStatus.PASS.value)
    if ok_st and ctx is not None:
        setattr(ctx, "login_auto_submit_attempts", attempts + 1)
    return {
        "ok": ok_st,
        "status": st,
        "summary": str(res.summary or res.error or "tap_element"),
        "capability_id": "tap_element",
    }


def run_login_otp_submit_chain(
    proxy: Any,
    ctx: Any,
    *,
    turn_seq: int,
    instruction: str,
    intents_done: set[str] | None,
    history_lines: list[str],
    hierarchy_nodes: list[dict[str, Any]] | None,
    login_module_case: bool = False,
) -> list[dict[str, Any]]:
    """发码后同一轮或下轮初：程序 get_otp → 填码 → 点登录。返回已执行的步骤摘要（供 agent_loop 写 history）。"""
    out: list[dict[str, Any]] = []
    otp = try_auto_otp_fill(
        proxy,
        ctx,
        turn_seq=turn_seq,
        instruction=instruction,
        intents_done=intents_done,
        history_lines=history_lines,
        login_module_case=login_module_case,
    )
    if otp:
        out.append(dict(otp))
        if not otp.get("ok"):
            return out
        hist2 = list(history_lines or [])
        cap_o = str(otp.get("capability_id") or "input_text")
        st_o = str(otp.get("status") or "pass")
        hist2.append(f"{turn_seq}. {cap_o} → {st_o}: {otp.get('summary') or ''}")
        if otp.get("ran_get_otp"):
            hist2.append(f"{turn_seq}. get_otp → pass: auto")
        done2 = set(intents_done or set())
        done2.add("otp_fill")
        done2.add("sms_send")
        sub = try_auto_login_submit(
            proxy,
            ctx,
            turn_seq=turn_seq,
            instruction=instruction,
            hierarchy_nodes=hierarchy_nodes,
            intents_done=done2,
            history_lines=hist2,
            login_module_case=login_module_case,
        )
        if sub:
            out.append(dict(sub))
        return out
    sub_only = try_auto_login_submit(
        proxy,
        ctx,
        turn_seq=turn_seq,
        instruction=instruction,
        hierarchy_nodes=hierarchy_nodes,
        intents_done=intents_done,
        history_lines=history_lines,
        login_module_case=login_module_case,
    )
    if sub_only:
        out.append(dict(sub_only))
    return out


def run_post_sms_login_pipeline(
    proxy: Any,
    ctx: Any,
    *,
    turn_seq: int,
    instruction: str,
    intents_done: set[str] | None,
    history_lines: list[str],
    hierarchy_nodes: list[dict[str, Any]] | None,
    login_module_case: bool = False,
    retries: int = 2,
) -> list[dict[str, Any]]:
    """发码后专用：短等 + 程序 get_otp/填码/点登录，避免空转 LLM turn。"""
    last: list[dict[str, Any]] = []
    attempts = max(1, min(4, int(retries or 1)))
    for i in range(attempts):
        last = run_login_otp_submit_chain(
            proxy,
            ctx,
            turn_seq=turn_seq,
            instruction=instruction,
            intents_done=intents_done,
            history_lines=history_lines,
            hierarchy_nodes=hierarchy_nodes,
            login_module_case=login_module_case,
        )
        if not last:
            if i + 1 < attempts:
                import time

                time.sleep(0.45)
            continue
        if any(bool(x.get("ok")) for x in last):
            if _otp_code_already_entered(history_lines, intents_done) or any(
                str(x.get("capability_id") or "") == "tap_element" and x.get("ok") for x in last
            ):
                return last
        if i + 1 < attempts:
            import time

            time.sleep(0.45)
    return last
