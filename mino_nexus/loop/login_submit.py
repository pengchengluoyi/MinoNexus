"""登录链末段：OTP 已齐后程序点「登录」，避免 LLM snap 空烧步数。"""
from __future__ import annotations

import re
from typing import Any, Optional

from mino_nexus.core.schemas import PlanEvent
from mino_nexus.loop.hierarchy_slots import match_any
from mino_nexus.loop.step_contract import instruction_allows_login_flow
from mino_nexus.loop.step_intent import instruction_required_intents


def _history_has(history: list[str], cap: str, status: str = "pass") -> bool:
    needle = f"{cap} → {status}"
    return any(needle in line for line in (history or []))


# Web 英文发码按钮与中文文案统一识别（须与 agent_loop 发码 tap 后处理一致）
SMS_SEND_TAP_RE = re.compile(
    r"发送验证码|重新发送|获取验证码|"
    r"(?:^|[^a-z])(Send(?:\s+(?:code|verification|otp))?|Get\s+code|Resend)(?:[^a-z]|$)",
    re.I,
)


def sms_send_tap_matches(text: str) -> bool:
    return bool(SMS_SEND_TAP_RE.search(str(text or "")))


LOGIN_SUBMIT_TAP_RE = re.compile(
    r"\bContinue\b|Verify(?:\s+code)?|确认|提交|完成登录|"
    r"(?:^|[^a-z])(log\s*in|sign\s*in)(?:[^a-z]|$)",
    re.I,
)


def login_submit_tap_matches(text: str) -> bool:
    return bool(LOGIN_SUBMIT_TAP_RE.search(str(text or "")))


def account_session_logged_in(ctx: Any | None) -> bool:
    if ctx is None:
        return False
    try:
        from mino_nexus.loop.observe.session_persist import effective_session_block

        block = str(effective_session_block(ctx, "") or "")
    except Exception:
        block = str(getattr(ctx, "session_block", "") or "")
    return bool(re.search(r"session\s*=\s*logged_in", block, re.I))


def _sms_send_done(history: list[str], intents_done: set[str] | None) -> bool:
    """request_sms_code / Web Send tap，或已记录的 sms_send 意图（避免 history 滑窗丢发码行）。"""
    done = set(intents_done or set())
    if "sms_send" in done:
        return True
    if _history_has(history, "request_sms_code"):
        return True
    for line in history or []:
        if "tap_element" not in line or "pass" not in line.lower():
            continue
        if sms_send_tap_matches(line):
            return True
    return False


def _email_login_kind(ctx: Any) -> bool:
    try:
        from mino_nexus.loop.local_executors import _otp_context
        from mino_nexus.services.otp_resolve import login_kind_from_secrets

        _doc, secrets, _otp = _otp_context(ctx)
        return login_kind_from_secrets(secrets) == "email"
    except Exception:
        return False


def _email_ready_for_otp(history: list[str], intents_done: set[str] | None) -> bool:
    done = set(intents_done or set())
    if "login_email" in done:
        for line in history or []:
            if "input_text" not in line or "pass" not in line.lower():
                continue
            if re.search(r"焦点未确认|focus\s*not\s*confirmed", line, re.I):
                continue
            if re.search(r"field=email|邮箱|@", line, re.I):
                return True
        return False
    for line in history or []:
        if "input_text" not in line or "pass" not in line.lower():
            continue
        if re.search(r"焦点未确认|focus\s*not\s*confirmed", line, re.I):
            continue
        if re.search(r"field=email|邮箱|@", line, re.I):
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
            if sms_send_tap_matches(line):
                idx = i
    if idx < 0:
        return []
    return list(history[idx + 1 :])


def instruction_requires_login_completion(instruction: str) -> bool:
    return bool(re.search(r"完成登录|登录成功", str(instruction or ""), re.I))


def _submit_tap_line_matches(line: str, *, ctx: Any | None = None) -> bool:
    if not re.search(r"tap_element", line, re.I) or "pass" not in line.lower():
        return False
    if re.search(r"登录|log\s*in|sign\s*in", line, re.I):
        return True
    if ctx is not None:
        from mino_nexus.loop.login_verification import is_web_email_login

        if is_web_email_login(ctx) and re.search(
            r"\bContinue\b|Verify|确认|提交", line, re.I
        ):
            return True
    return False


def _last_otp_fill_index(lines: list[str]) -> int:
    """lines 中最后一次验证码 input_text(pass) 的下标，无则 -1。"""
    last = -1
    for i, line in enumerate(lines or []):
        if "input_text" not in line or "pass" not in line.lower():
            continue
        if re.search(r"sms_code|验证码|\(sms_code\)", line, re.I):
            last = i
            continue
        m = re.search(r"输入\s*(\d+)\s*字", line)
        if m and 4 <= int(m.group(1)) <= 8:
            last = i
    return last


def _final_login_submit_done(history: list[str], *, ctx: Any | None = None) -> bool:
    """发码且填码之后是否已点过提交登录（与入口「点登录」区分：只看发码后的 tap）。"""
    after = _lines_after_sms_sent(history)
    if not after:
        return False
    otp_i = _last_otp_fill_index(after)
    if otp_i < 0:
        return False
    for line in after[otp_i + 1 :]:
        if _submit_tap_line_matches(line, ctx=ctx):
            return True
    return False


def login_flow_do_may_finish(
    *,
    instruction: str,
    history_lines: list[str],
    ctx: Any | None,
) -> tuple[bool, str]:
    """「完成登录」类 do 步：意图齐了也不等于已登录，须 session=logged_in。"""
    if not instruction_requires_login_completion(instruction):
        return True, ""
    from mino_nexus.loop.observe.session_persist import effective_session_block, parse_session_value

    block = effective_session_block(ctx, "") if ctx is not None else ""
    sess = parse_session_value(block)
    if sess == "logged_in":
        return True, ""
    if not _final_login_submit_done(history_lines, ctx=ctx):
        return (
            False,
            "未完成登录提交：发码并填验证码后须点登录/Continue 等提交钮，勿 signal_done。",
        )
    return (
        False,
        "已点提交但仍为未登录(session≠logged_in)：继续登录流程或检查验证码是否正确，勿 signal_done。",
    )


def _otp_code_already_entered(
    history: list[str],
    intents_done: set[str] | None,
    cursor: Any | None = None,
) -> bool:
    """发码之后验证码是否已填入（勿把发码前的「输入 11 字」手机号当成验证码）。"""
    if cursor is not None:
        try:
            from mino_nexus.loop.milestones import otp_fill_device_complete

            if otp_fill_device_complete(cursor, history):
                return True
        except Exception:
            pass
    after_sms = _lines_after_sms_sent(history)
    if not after_sms:
        return False
    for line in after_sms:
        if "input_text" not in line or "pass" not in line.lower():
            continue
        if re.search(r"焦点未确认|focus\s*not\s*confirmed", line, re.I):
            continue
        if re.search(r"验证码|sms_code|\(sms_code\)", line, re.I):
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
    if not otp_fetch_allowed(history_lines, intents_done, ctx):
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

    if proxy is not None and ctx is not None:
        try:
            from mino_nexus.loop.hierarchy_slots import capture

            snap = capture(proxy, turn_id=int(turn_seq or 0))
            if snap.ok and snap.nodes:
                setattr(ctx, "nav_hierarchy_nodes", [n for n in snap.nodes if isinstance(n, dict)])
        except Exception:
            pass
        try:
            from mino_nexus.loop.web.web_progress import refresh_web_focus

            refresh_web_focus(ctx, proxy)
        except Exception:
            pass
        try:
            from mino_nexus.loop.device_execute_params import cache_web_sms_code_tap_from_ctx

            nodes = list(getattr(ctx, "nav_hierarchy_nodes", None) or [])
            cache_web_sms_code_tap_from_ctx(ctx, hierarchy_nodes=nodes)
        except Exception:
            pass

    from mino_nexus.loop.device_execute_params import enrich_web_input_text_params

    fill_params = enrich_web_input_text_params({"field": "sms_code", "text": code}, ctx)
    res_in = _dispatch_device(
        proxy,
        ctx=ctx,
        seq=int(turn_seq),
        cap="input_text",
        params=fill_params,
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
    if "login_flow" not in need and not re.search(
        r"点击.{0,12}登录|登录按钮|完成登录", instruction, re.I
    ):
        return None
    if _final_login_submit_done(history_lines, ctx=ctx):
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
    from mino_nexus.loop.device_execute_params import _tap_node_pool, web_tap_params_for_selector

    nodes = _tap_node_pool(ctx, list(hierarchy_nodes or []))
    conds = [
        {"text_equals": "登录"},
        {"content_desc_equals": "登录"},
        {"text_contains": "立即登录"},
        {"text_equals": "Log in"},
        {"text_equals": "Sign in"},
        {"text_contains": "Log in"},
        {"text_contains": "Sign in"},
        {"content_desc_contains": "登录"},
    ]
    from mino_nexus.loop.login_verification import is_web_email_login

    if is_web_email_login(ctx):
        conds.extend(
            [
                {"text_equals": "Continue"},
                {"text_contains": "Continue"},
                {"content_desc_equals": "Continue"},
                {"text_equals": "Verify"},
            ]
        )
    from mino_nexus.core.protocol import EventStatus
    from mino_nexus.loop.local_executors import _dispatch_device

    hit = match_any(nodes, conds) if nodes else None
    if hit is None and is_web_email_login(ctx):
        tap_params = web_tap_params_for_selector(ctx, "Continue", hierarchy_nodes=nodes)
        res = _dispatch_device(
            proxy,
            ctx=ctx,
            seq=int(turn_seq),
            cap="tap_element",
            params=tap_params,
            label="程序：验证码已填，按视觉坐标/文案点 Continue 提交",
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
    if hit is None:
        # 避免误点「右上角登录」入口：优先提交钮（通常在表单下方）
        for node in nodes or []:
            blob = " ".join(
                str(node.get(k) or "")
                for k in ("text", "content_desc", "aria_label", "name")
            )
            if not re.search(r"^登录$|log\s*in|sign\s*in", blob.strip(), re.I):
                continue
            if re.search(r"退出|注册|忘记", blob, re.I):
                continue
            hit = node
            break
    if hit is None:
        return None
    from mino_nexus.loop.ui_consent import tap_params_for_control

    tap_params = tap_params_for_control(hit, nodes)
    res = _dispatch_device(
        proxy,
        ctx=ctx,
        seq=int(turn_seq),
        cap="tap_element",
        params=tap_params,
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


def otp_fetch_allowed(
    history_lines: list[str],
    intents_done: set[str] | None,
    ctx: Any,
    cursor: Any = None,
) -> bool:
    if not _sms_send_done(history_lines, intents_done):
        return False
    if _email_login_kind(ctx) and not _email_ready_for_otp(history_lines, intents_done):
        if cursor is not None:
            from mino_nexus.loop.milestones import _milestone_passed, milestone_by_id, read_state

            ef = milestone_by_id(read_state(cursor), "account_fill")
            if not (ef and _milestone_passed(ef)):
                return False
        else:
            return False
    sent_at = float(getattr(ctx, "otp_sent_at", 0) or 0)
    if sent_at <= 0:
        # 历史里的 Send / request_sms_code 已经证明发过。前置阶段点 Send 不会写入 sms_send 意图。
        from mino_nexus.loop.login_verification import record_verification_send

        record_verification_send(ctx)
    return True


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
    """发码后的下一轮：get_otp → 填验证码 → 点登录（须已真实发码且邮箱已填入）。"""
    if not otp_fetch_allowed(history_lines, intents_done, ctx):
        return []
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
