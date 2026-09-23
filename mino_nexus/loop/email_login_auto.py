"""邮箱登录 UI 程序链：Email 标签 → 填邮箱 → 点发送；取码/点登录在发码后的下一轮。"""
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


def _hierarchy_nodes(ctx: Any) -> list[dict[str, Any]]:
    return [n for n in (getattr(ctx, "nav_hierarchy_nodes", None) or []) if isinstance(n, dict)]


def _ui_channel_key(ctx: Any) -> str:
    from mino_nexus.loop.ui_channel import UiChannel, ui_channel_from_ctx, ui_channel_label

    return ui_channel_label(ui_channel_from_ctx(ctx))


def _login_surface_visible(ctx: Any, nodes: list[dict[str, Any]]) -> bool:
    """登录弹窗/表单已出现（含邮箱框、Email 标签或发码区），否则应先点入口登录。"""
    if not nodes:
        return False
    from mino_nexus.loop.ui_sms_request import credential_field_for_login

    ch = _ui_channel_key(ctx)
    if credential_field_for_login(nodes, login_kind="email", channel=ch):
        return True
    from mino_nexus.loop.hierarchy_slots import match_any

    if match_any(
        nodes,
        [
            {"text_equals": "Email"},
            {"text_contains": "邮箱"},
            {"text_contains": "email"},
            {"text_contains": "验证码"},
            {"text_contains": "发送验证码"},
        ],
    ):
        return True
    return False


def _email_tab_in_hierarchy(nodes: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    from mino_nexus.loop.hierarchy_slots import match_any

    return match_any(
        nodes,
        [
            {"text_equals": "Email"},
            {"text_contains": "邮箱"},
            {"text_equals": "E-mail"},
        ],
    )


def _lease_email(ctx: Any) -> str:
    from mino_nexus.services.account_credential_text import lease_email_address

    acc = dict(getattr(ctx, "picked_account", None) or {})
    return lease_email_address(acc)


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


def run_email_login_ui_chain(
    proxy: Any,
    ctx: Any,
    *,
    turn_seq: int,
    instruction: str,
    intents_done: set[str] | None,
    history_lines: list[str],
    login_module_case: bool = False,
) -> list[dict[str, Any]]:
    """严格 UI 顺序：选 Email → input_text(邮箱) → request_sms_code。不含 get_otp。"""
    if _login_kind(ctx) != "email":
        return []
    if not instruction_allows_login_flow(instruction, login_module_case=login_module_case):
        return []
    from mino_nexus.loop.login_submit import _sms_send_done

    if _sms_send_done(history_lines, intents_done):
        return []

    nodes = _hierarchy_nodes(ctx)
    if proxy is not None:
        try:
            _dispatch_step(
                proxy,
                ctx,
                turn_seq=turn_seq,
                cap="wait_ms",
                params={"duration_ms": 450},
                label="程序：等待登录弹窗渲染",
            )
        except Exception:
            pass
        try:
            from mino_nexus.loop.hierarchy_slots import capture

            snap = capture(proxy, turn_id=int(turn_seq or 0))
            if snap.ok and snap.nodes:
                nodes = [n for n in snap.nodes if isinstance(n, dict)]
                setattr(ctx, "nav_hierarchy_nodes", nodes)
        except Exception:
            pass
    if not _login_surface_visible(ctx, nodes):
        # Web 登录弹窗常在截图里可见但不在 Playwright a11y 树；Email 已点后仍应程序填邮箱。
        if not (
            _email_tab_selected(history_lines)
            and not _email_input_done(history_lines, intents_done)
            and _ui_channel_key(ctx) == "web_dom"
        ):
            return []

    from mino_nexus.loop.login_account_lease import try_lease_on_login_surface

    case_row = getattr(ctx, "case", None) if ctx is not None else None
    leased_ok, lease_err, _lease_brief = try_lease_on_login_surface(
        ctx,
        case_row if isinstance(case_row, dict) else None,
    )
    if not leased_ok:
        return [
            {
                "ok": False,
                "status": "fail",
                "summary": lease_err or "登录界面租号失败",
                "capability_id": "lease_account",
                "params": {},
            }
        ]

    email = _lease_email(ctx)
    if not email:
        return [
            {
                "ok": False,
                "status": "fail",
                "summary": "已租号但号池行缺少可用邮箱，请检查账号 email 字段",
                "capability_id": "lease_account",
                "params": {},
            }
        ]

    out: list[dict[str, Any]] = []
    ch = _ui_channel_key(ctx)
    from mino_nexus.loop.ui_sms_request import credential_field_for_login

    email_field = credential_field_for_login(nodes, login_kind="email", channel=ch)

    if not _email_tab_selected(history_lines) and email_field is None:
        tab_node = _email_tab_in_hierarchy(nodes)
        if tab_node is None:
            return []
        from mino_nexus.loop.ui_consent import tap_params_for_control

        tab_params = tap_params_for_control(tab_node, nodes)
        tab = _dispatch_step(
            proxy,
            ctx,
            turn_seq=turn_seq,
            cap="tap_element",
            params=tab_params,
            label="程序：登录弹窗选 Email 标签",
        )
        out.append(tab)
        if not tab.get("ok"):
            return out

    if not _email_input_done(history_lines, intents_done):
        if ch == "web_dom" and proxy is not None:
            from mino_nexus.loop.web_progress import refresh_web_focus, web_editable_focus_ready
            from mino_nexus.loop.ui_consent import tap_params_for_control

            refresh_web_focus(ctx, proxy)
            if not web_editable_focus_ready(ctx):
                focus_field = email_field or credential_field_for_login(
                    nodes, login_kind="email", channel=ch
                )
                if focus_field is not None:
                    tap_focus = _dispatch_step(
                        proxy,
                        ctx,
                        turn_seq=turn_seq,
                        cap="tap_element",
                        params=tap_params_for_control(focus_field, nodes),
                        label="程序：聚焦邮箱输入框",
                    )
                    out.append(tap_focus)
                    if not tap_focus.get("ok"):
                        return out
                    refresh_web_focus(ctx, proxy)

        from mino_nexus.catalog.tool_schema import fill_input_text_from_ctx
        from mino_nexus.loop.device_execute_params import prepare_device_execute_params

        fill_params = prepare_device_execute_params(
            "input_text",
            fill_input_text_from_ctx(
                {"field": "email", "text": email},
                cap_id="input_text",
                ctx=ctx,
            ),
            ctx,
        )
        fill = _dispatch_step(
            proxy,
            ctx,
            turn_seq=turn_seq,
            cap="input_text",
            params=fill_params,
            label="程序：填入租号邮箱",
        )
        out.append(fill)
        if not fill.get("ok"):
            return out

    if "sms_send" in set(intents_done or set()):
        return out

    send = _dispatch_step(
        proxy,
        ctx,
        turn_seq=turn_seq,
        cap="request_sms_code",
        params={},
        label="程序：邮箱已填，点击发送验证码",
    )
    out.append(send)
    return out


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
    """兼容旧名：与 run_email_login_ui_chain 相同。"""
    return run_email_login_ui_chain(
        proxy,
        ctx,
        turn_seq=turn_seq,
        instruction=instruction,
        intents_done=intents_done,
        history_lines=history_lines,
        login_module_case=login_module_case,
    )
