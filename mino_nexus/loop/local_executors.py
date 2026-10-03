"""Nexus 本地 executor：问人 / 等待 / 视觉断言，不出网。"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from mino_nexus.services.account_lease import format_accounts_brief, lease_for_context
from mino_nexus.services.project_env import account_ident
from mino_nexus.loop.router_proxy import LOCAL_CAPS, LOCAL_CAP_PREFIXES, is_local_cap
from mino_nexus.core.protocol import EventStatus
from mino_nexus.core.schemas import EventResult, PlanEvent


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _result(
    event: PlanEvent,
    *,
    status: EventStatus,
    summary: str,
    elapsed_ms: int = 0,
    error: str = "",
    executor: str = "internal",
    vlm_meta: dict[str, Any] | None = None,
    raw_response: dict[str, Any] | None = None,
) -> EventResult:
    return EventResult(
        seq=event.seq,
        capability_id=event.capability_id,
        event_kind=event.event_kind or event.capability_id,
        status=status,
        executor_used=executor,
        elapsed_ms=elapsed_ms,
        summary=summary,
        error=error,
        ai_reasoning=event.ai_reasoning or "",
        plan_event=event.model_dump(exclude_none=True),
        vlm_meta=dict(vlm_meta or {}),
        raw_response=dict(raw_response or {}),
        started_at=_now(),
        finished_at=_now(),
    )


_FSM_DEGRADE_HINT = (
    "【导航降级】路线图未能从当前屏执行跳转（常见：深层页不在架构图、或当前无底栏 Tab）。"
    "若处于 onboarding 无底栏墙，请开环 tap_element 沿主流程出墙，勿重复 fsm_navigate；"
    "否则请 press_back 退出栈顶，或 recover_restart_target_app 冷启动后再点 Tab。"
)
_FSM_SAME_PAGE_HINT = (
    "【导航】路线图判定当前已在目标节点，本次未执行点击。"
    "若屏上已是目标页请立即 signal_done；若实际不是，请 tap_element 直点目标入口或 press_back，"
    "禁止用相同 from/to 重复 fsm_navigate。"
)


def _visible_bottom_tab_slots(ctx: Any) -> list[dict[str, Any]]:
    nodes = getattr(ctx, "nav_hierarchy_nodes", None) if ctx is not None else None
    if not isinstance(nodes, list) or not nodes:
        return []
    from mino_nexus.services.nav_tab_slots import find_bottom_tab_slots

    return find_bottom_tab_slots(nodes)


def _skip_tab_fallback(_localized: dict[str, Any], ctx: Any = None) -> bool:
    """当前屏看不见底栏时，直点 Tab 必败 —— 与 localize 是否已认出页面无关。"""
    return len(_visible_bottom_tab_slots(ctx)) < 2


def _fold_tap_label(text: str) -> str:
    return "".join(str(text or "").split()).lower()


def _params_tap_label(params: dict[str, Any]) -> str:
    return _fold_tap_label(params.get("selector_text") or params.get("text") or "")


def _tap_params_has_point(params: dict[str, Any]) -> bool:
    if params.get("x") is not None and params.get("y") is not None:
        return True
    raw = params.get("fallback_xy")
    if isinstance(raw, (list, tuple)) and len(raw) >= 2:
        try:
            int(raw[0])
            int(raw[1])
            return True
        except (TypeError, ValueError):
            return False
    return False


def _tap_selector_likely_on_screen(params: dict[str, Any], nodes: list[dict[str, Any]]) -> bool:
    from mino_nexus.loop.tap_enrich import _match_node, _hint_labels

    labels = _hint_labels(params, str(params.get("selector_text") or ""))
    if not labels:
        return False
    for label in labels:
        if _match_node(nodes, label, hint="底栏"):
            return True
    return False


def _visible_tab_labels(ctx: Any) -> list[str]:
    from mino_nexus.services.nav_tab_slots import find_bottom_tab_slots

    nodes = getattr(ctx, "nav_hierarchy_nodes", None) if ctx is not None else None
    if not isinstance(nodes, list):
        return []
    out: list[str] = []
    for slot in find_bottom_tab_slots(nodes):
        if not isinstance(slot, dict):
            continue
        lab = str(slot.get("label") or slot.get("display") or "").strip()
        if lab and lab not in out:
            out.append(lab)
    return out


def dispatch_local(
    event: PlanEvent,
    *,
    shot: Any = None,
    ctx: Any = None,
    router: Any = None,
    target_package: str = "",
) -> EventResult:
    cap = event.capability_id
    t0 = time.time()
    from mino_nexus.catalog.skill_channel import capability_dispatch_ok

    if not capability_dispatch_ok(cap):
        return _result(
            event,
            status=EventStatus.FAIL,
            summary=f"能力未启用或当前不可覆盖: {cap}",
            error="capability_disabled",
            elapsed_ms=int((time.time() - t0) * 1000),
        )
    if cap == "signal_nav_calib_step":
        key = str((event.params or {}).get("step_key") or "").strip()
        summary = f"校准里程碑 {key}" if key else "校准里程碑（未给 step_key）"
        return _result(
            event,
            status=EventStatus.PASS if key else EventStatus.FAIL,
            summary=summary,
            error="" if key else "missing step_key",
            elapsed_ms=int((time.time() - t0) * 1000),
        )
    if cap == "wait_ms":
        ms = int((event.params or {}).get("duration_ms") or (event.params or {}).get("ms") or 500)
        ms = max(0, min(ms, 60_000))
        if ms:
            time.sleep(ms / 1000.0)
        return _result(event, status=EventStatus.PASS, summary=f"等待 {ms}ms", elapsed_ms=int((time.time() - t0) * 1000))
    if cap == "wait_screen_ready":
        return _wait_screen_ready(
            event,
            shot=shot,
            ctx=ctx,
            router=router,
            target_package=target_package,
            t0=t0,
        )
    if cap.startswith("signal_"):
        return _result(
            event,
            status=EventStatus.PASS,
            summary=cap,
            elapsed_ms=int((time.time() - t0) * 1000),
        )
    if cap.startswith("human_"):
        return _result(
            event,
            status=EventStatus.BLOCKED,
            summary="需要人在回路，HITL 界面尚未接线",
            error="HITL 尚未搬迁。请在 Studio 人工处理这一步，或改用例避开问人。",
            executor="hitl",
        )
    if cap == "assert_visual":
        params = event.params or {}
        expectation = str(params.get("expectation") or params.get("expected") or "").strip()
        has_image = bool(shot is not None and getattr(shot, "has_image", lambda: False)())
        if not has_image:
            return _result(
                event,
                status=EventStatus.FAIL,
                summary="视觉断言没有截图，不能记为通过",
                error="no screenshot",
                executor="vlm",
                elapsed_ms=int((time.time() - t0) * 1000),
            )
        from mino_nexus.ai.planner import verify_step_expected

        knowledge_ctx = str(params.get("knowledge_context") or "").strip()
        if knowledge_ctx:
            knowledge_ctx = f"==== 相关知识（供断言参考）====\n{knowledge_ctx}"

        res = verify_step_expected(
            expectation=expectation,
            image_base64=shot.image_base64,
            image_mime=getattr(shot, "image_mime", None) or "image/png",
            context_block=knowledge_ctx,
        )
        ok = bool(res.passed)
        summary = (res.evidence or res.ai_reasoning or "").strip() or (
            "预期成立" if ok else "预期未成立"
        )
        vlm_meta: dict[str, Any] = {}
        if res.screen_layout:
            vlm_meta["screen_layout"] = res.screen_layout
        return _result(
            event,
            status=EventStatus.PASS if ok else EventStatus.FAIL,
            summary=summary,
            error="" if ok else summary,
            executor="vlm",
            elapsed_ms=int((time.time() - t0) * 1000),
            vlm_meta=vlm_meta,
        )
    if cap == "relogin":
        from mino_nexus.loop.login_state_probe import observe_login_state, remember_session
        from mino_nexus.runtime.session_gate import required_session as required_session_enum

        scene = getattr(ctx, "case_scene", None) if ctx is not None else None
        req = required_session_enum(scene=scene)
        session, reason = observe_login_state(ctx, router, shot)
        summary = reason or f"session={session}"
        if req == "guest" and session == "logged_in":
            from mino_nexus.loop.session_ensure import try_logout_via_nav

            ok, logout_msg = try_logout_via_nav(ctx, router, seq=event.seq)
            if ok:
                remember_session(ctx, "logged_out", "logout")
                summary = f"已登出（原 session=logged_in）；{logout_msg}"
            else:
                summary = f"资源与要求不一致：仍为已登录。{logout_msg}"
        elif req == "logged_in" and session != "logged_in":
            summary = f"当前 session={session}，与要求的已登录不一致；已写入上下文，后续步骤继续登录。{reason}"
        src = str(getattr(ctx, "device_session_source", "") or "")
        if src == "tool_api":
            executor = "playwright"
        elif src == "screen":
            executor = "vlm"
        else:
            executor = "internal"
        return _result(
            event,
            status=EventStatus.PASS,
            summary=summary[:500],
            executor=executor,
            elapsed_ms=int((time.time() - t0) * 1000),
        )
    if cap == "check_run_env":
        return _check_run_env(event, ctx=ctx, t0=t0)
    if cap in ("fsm_navigate", "recover_fsm_navigate"):
        return _fsm_navigate(event, ctx=ctx, t0=t0, router=router)
    if cap == "lease_account":
        params = dict(event.params or {})
        from mino_nexus.loop.session_ensure import account_need_from_case

        scene = getattr(ctx, "case_scene", None) if ctx is not None else None
        case_row = getattr(ctx, "case", None) if ctx is not None else None
        need = account_need_from_case(
            case_row if isinstance(case_row, dict) else {},
            scene if isinstance(scene, dict) else {},
        )
        tid = str(params.get("template_id") or params.get("account_template_id") or "").strip()
        if tid:
            need["template_id"] = tid
        if isinstance(params.get("requirements"), dict):
            from mino_nexus.services.account_pool_templates import merge_need_requirements

            need["requirements"] = merge_need_requirements(
                need.get("requirements") if isinstance(need.get("requirements"), dict) else {},
                params.get("requirements"),
            )
        from mino_nexus.services.account_lease import INTERACTIVE_ACQUIRE_WAIT_MS

        row, err = lease_for_context(
            ctx,
            params,
            ai_reasoning=str(event.ai_reasoning or ""),
            need_facets=need,
            wait_ms=INTERACTIVE_ACQUIRE_WAIT_MS,
        )
        if row:
            brief = format_accounts_brief(row)
            return _result(
                event,
                status=EventStatus.PASS,
                summary=brief or f"已租账号 {account_ident(row)}",
                executor="internal",
                elapsed_ms=int((time.time() - t0) * 1000),
            )
        return _result(
            event,
            status=EventStatus.FAIL,
            summary=err or "租号失败",
            error=err or "lease failed",
            executor="internal",
            elapsed_ms=int((time.time() - t0) * 1000),
        )
    if cap == "get_otp":
        return _get_otp(event, ctx=ctx, t0=t0, router=router)
    if cap == "accept_legal_consent":
        return _accept_legal_consent(event, ctx=ctx, router=router, t0=t0)
    if cap == "request_sms_code":
        return _request_sms_code(event, ctx=ctx, router=router, t0=t0)
    if cap == "dismiss_ime":
        return _dismiss_ime(event, ctx=ctx, router=router, t0=t0)
    if cap == "persona_subtask":
        return _result(
            event,
            status=EventStatus.DECLINED,
            summary="拟人化编排尚未搬迁",
            error="persona_subtask 本地 executor 尚未接线",
            executor="ai_persona",
        )
    return _result(
        event,
        status=EventStatus.DECLINED,
        summary=f"未知本地能力 {cap}",
        error=f"cap={cap} 标为本地但没有 executor",
    )


def _hierarchy_nodes(ctx: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    nodes = getattr(ctx, "nav_hierarchy_nodes", None) if ctx is not None else None
    if isinstance(nodes, list):
        out.extend(n for n in nodes if isinstance(n, dict))
    vlm = getattr(ctx, "nav_vlm_hierarchy", None) if ctx is not None else None
    if isinstance(vlm, dict):
        out.extend(n for n in (vlm.get("nodes") or []) if isinstance(n, dict))
    return out


def _dispatch_device(
    router: Any,
    *,
    ctx: Any,
    seq: int,
    cap: str,
    params: dict[str, Any],
    label: str,
) -> Any:
    from mino_nexus.core.schemas import PlanEvent
    from mino_nexus.loop.web.web_env import agent_step_idx

    from mino_nexus.loop.device_execute_params import prepare_device_execute_params

    merged = prepare_device_execute_params(cap, dict(params or {}), ctx)
    if ctx is not None:
        setattr(ctx, "last_device_execute_params", dict(merged))
    event = PlanEvent(
        seq=seq,
        capability_id=cap,
        event_kind=cap,
        params=merged,
        ai_reasoning=label,
        label=label[:80],
    )
    scout_run_id = str(getattr(ctx, "scout_run_id", "") or getattr(ctx, "run_id", "") or "") if ctx else ""
    case_seq = int(getattr(ctx, "case_seq", 0) or 0) if ctx else 0
    return router.dispatch(event, run_id=scout_run_id, step_idx=agent_step_idx(case_seq, seq))


def _exec_ok(result: Any) -> bool:
    st = result.status.value if hasattr(result.status, "value") else str(result.status)
    return st in ("pass", "done")


def _otp_context(ctx: Any) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """(env_doc, effective_secrets, otp_slot) — 失败时返回空 dict。"""
    try:
        from mino_nexus.services import project_store as ps
        from mino_nexus.services.otp_resolve import resolve_effective_secrets

        app_id = str(getattr(ctx, "app_id", "") or "").strip() if ctx is not None else ""
        if not app_id:
            return {}, {}, {}
        app = ps.require_app(app_id)
        project_id = str(app.get("project_id") or "").strip()
        if not project_id:
            return {}, {}, {}
        env_doc = ps.project_env(project_id)
        env_key = str(getattr(ctx, "env_profile", "") or "test") if ctx is not None else "test"
        surface = str(getattr(ctx, "env_surface", "") or "").strip()
        secrets = resolve_effective_secrets(
            env_doc,
            env_profile=env_key,
            env_surface=surface,
        )
        otp = secrets.get("otp") if isinstance(secrets, dict) else {}
        if not isinstance(otp, dict):
            otp = {}
        return env_doc, secrets, otp
    except Exception:
        return {}, {}, {}


def _resolve_otp(ctx: Any) -> tuple[str, str]:
    acc = getattr(ctx, "picked_account", None) if ctx is not None else None
    if isinstance(acc, dict):
        for key in ("otp", "sms_code", "code"):
            val = str(acc.get(key) or "").strip()
            if val:
                return val, "account"
    env_doc, _secrets, otp = _otp_context(ctx)
    if not otp:
        return "", "missing"
    fixed = str(otp.get("fixed") or "").strip()
    mode = str(otp.get("mode") or "auto").strip().lower()
    if mode == "hitl":
        return "", "hitl"
    if fixed and mode in ("fixed", "auto"):
        return fixed, "env_fixed"
    if mode == "gmail":
        return "", "gmail"
    if mode == "auto":
        from mino_nexus.services.otp_resolve import gmail_inbox_address

        uid = str(getattr(ctx, "plugin_user_id", "") or "").strip() if ctx is not None else ""
        if gmail_inbox_address(env_doc, plugin_user_id=uid):
            return "", "gmail"
    if mode == "fixed" and not fixed:
        return "", "missing"
    return "", "missing"


def _lease_email_for_otp(ctx: Any) -> str:
    acc = getattr(ctx, "picked_account", None) if ctx is not None else None
    if isinstance(acc, dict):
        for key in ("email", "login_email"):
            val = str(acc.get(key) or "").strip()
            if val:
                return val
    return ""


def _fetch_gmail_otp(ctx: Any, router: Any) -> str:
    from mino_nexus.core.schemas import PlanEvent
    from mino_nexus.services.gmail_otp import GmailOtpError

    hint = "请在这台 Scout 节点的「邮箱 / Gmail」里填写收件箱和应用专用密码"
    if router is None:
        raise GmailOtpError(hint)
    _env_doc, _secrets, otp = _otp_context(ctx)
    since = float(getattr(ctx, "otp_sent_at", 0) or 0) or None
    deadline = float(getattr(ctx, "case_deadline_ts", 0) or 0) or None
    params = {
        "to_address": _lease_email_for_otp(ctx),
        "since_ts": since,
        "from_allowlist": list(otp.get("from_allowlist") or []),
        "subject_contains": str(otp.get("subject_contains") or ""),
        "poll_interval_ms": int(otp.get("poll_interval_ms") or 3000),
        "max_wait_ms": int(otp.get("max_wait_ms") or 90_000),
        "deadline_ts": deadline,
    }
    event = PlanEvent(
        seq=0,
        capability_id="plugin.gmail.fetch_otp",
        event_kind="plugin.gmail.fetch_otp",
        params=params,
        label="Gmail 取码",
    )
    result = router.dispatch(
        event,
        run_id=str(getattr(ctx, "scout_run_id", "") or getattr(ctx, "run_id", "") or "") if ctx else "",
        step_idx=-1,
        ctx=ctx,
    )
    status = result.status.value if hasattr(result.status, "value") else str(result.status)
    raw = dict(getattr(result, "raw_response", None) or {})
    if status not in ("pass", "done"):
        raise GmailOtpError(str(getattr(result, "summary", "") or getattr(result, "error", "") or hint))
    code = str(raw.get("code") or "").strip()
    if not code:
        raise GmailOtpError(hint)
    return code


def _get_otp(event: PlanEvent, *, ctx: Any, t0: float, router: Any = None) -> EventResult:
    code, source = _resolve_otp(ctx)
    if not code and source == "gmail":
        if float(getattr(ctx, "otp_sent_at", 0) or 0) <= 0:
            from mino_nexus.loop.login_verification import otp_not_sent_executor_summary

            return _result(
                event,
                status=EventStatus.FAIL,
                summary=otp_not_sent_executor_summary(ctx),
                error="otp not sent",
                executor="internal",
                elapsed_ms=int((time.time() - t0) * 1000),
            )
        try:
            code = _fetch_gmail_otp(ctx, router)
            source = "gmail"
        except Exception as exc:
            from mino_nexus.services.gmail_otp import GmailOtpError

            summary = str(exc) if isinstance(exc, GmailOtpError) else f"Gmail 取码失败：{exc}"
            if "已取消" in summary or "超过用例时间" in summary:
                return _result(
                    event,
                    status=EventStatus.FAIL,
                    summary=summary,
                    error="otp wait stopped",
                    executor="internal",
                    elapsed_ms=int((time.time() - t0) * 1000),
                )
            if ctx is not None:
                setattr(
                    ctx,
                    "otp_fetch_fail_streak",
                    int(getattr(ctx, "otp_fetch_fail_streak", 0) or 0) + 1,
                )
            return _result(
                event,
                status=EventStatus.FAIL,
                summary=summary,
                error="gmail otp failed",
                executor="internal",
                elapsed_ms=int((time.time() - t0) * 1000),
            )
    if not code:
        if source == "hitl":
            summary = "当前环境接码为人工（hitl），请 signal_ask_human 或填写账号 otp"
        else:
            summary = "未配置验证码：请在账号 otp、环境固定码、或该 Scout 节点的邮箱 / Gmail 中配置"
        return _result(
            event,
            status=EventStatus.FAIL,
            summary=summary,
            error="otp not configured",
            executor="internal",
            elapsed_ms=int((time.time() - t0) * 1000),
        )
    if ctx is not None:
        acc = dict(getattr(ctx, "picked_account", None) or {})
        acc["otp"] = code
        ctx.picked_account = acc
        setattr(ctx, "otp_fetch_fail_streak", 0)
    return _result(
        event,
        status=EventStatus.PASS,
        summary=f"已取验证码（{source}）otp={code}",
        executor="internal",
        elapsed_ms=int((time.time() - t0) * 1000),
        raw_response={"otp": code, "source": source},
    )


def _accept_legal_consent(
    event: PlanEvent,
    *,
    ctx: Any,
    router: Any,
    t0: float,
) -> EventResult:
    from mino_nexus.loop.hierarchy_slots import node_flag
    from mino_nexus.loop.ui_consent import (
        any_focused_input,
        find_consent_control,
        tap_params_for_control,
    )

    nodes = _hierarchy_nodes(ctx)
    ctrl = find_consent_control(nodes)
    if ctrl is None:
        return _result(
            event,
            status=EventStatus.FAIL,
            summary="未找到可勾选控件（长文案左侧小方框）。请 tap_element 点勾选框本身，不要点长文案。",
            error="consent control not found",
            executor="internal",
            elapsed_ms=int((time.time() - t0) * 1000),
        )
    if node_flag(ctrl, "checked"):
        return _result(
            event,
            status=EventStatus.PASS,
            summary="同意框已勾选，无需再点",
            executor="internal",
            elapsed_ms=int((time.time() - t0) * 1000),
        )
    if router is None:
        return _result(
            event,
            status=EventStatus.FAIL,
            summary="未连接设备，无法勾选同意框",
            error="no router",
            executor="internal",
            elapsed_ms=int((time.time() - t0) * 1000),
        )
    if any_focused_input(nodes):
        back = _dispatch_device(
            router,
            ctx=ctx,
            seq=event.seq,
            cap="press_key",
            params={"key": "BACK"},
            label="收起输入法后再勾选",
        )
        if not _exec_ok(back):
            return _result(
                event,
                status=EventStatus.FAIL,
                summary=f"收起输入法失败：{back.summary or back.error}",
                error=str(back.error or back.summary or "press_key failed"),
                executor="internal",
                elapsed_ms=int((time.time() - t0) * 1000),
            )
    tap = _dispatch_device(
        router,
        ctx=ctx,
        seq=event.seq,
        cap="tap_element",
        params=tap_params_for_control(ctrl, nodes),
        label="勾选同意框",
    )
    if _exec_ok(tap):
        return _result(
            event,
            status=EventStatus.PASS,
            summary="已点击同意框（长文案左侧控件）",
            executor="internal+adb",
            elapsed_ms=int((time.time() - t0) * 1000),
        )
    return _result(
        event,
        status=EventStatus.FAIL,
        summary=f"勾选同意框失败：{tap.summary or tap.error}",
        error=str(tap.error or tap.summary or "tap failed"),
        executor="internal",
        elapsed_ms=int((time.time() - t0) * 1000),
    )


_WEB_SEND_LABELS = (
    "发送验证码",
    "获取验证码",
    "发送",
    "Send code",
    "Send",
    "Get code",
    "Verify",
)


def _web_tap_send_code(
    event: PlanEvent,
    *,
    ctx: Any,
    router: Any,
    t0: float,
) -> EventResult:
    if router is None:
        return _result(
            event,
            status=EventStatus.FAIL,
            summary="未连接 Web 执行节点，无法点发送",
            error="no router",
            executor="internal",
            elapsed_ms=int((time.time() - t0) * 1000),
        )
    for lab in _WEB_SEND_LABELS:
        tap = _dispatch_device(
            router,
            ctx=ctx,
            seq=event.seq,
            cap="tap_element",
            params={"selector_text": lab, "text": lab},
            label=f"Web 发码：点「{lab}」",
        )
        if _exec_ok(tap):
            from mino_nexus.loop.login_verification import record_verification_send

            record_verification_send(ctx)
            return _result(
                event,
                status=EventStatus.PASS,
                summary=f"已点击 Web 发送控件「{lab}」",
                executor="internal+playwright",
                elapsed_ms=int((time.time() - t0) * 1000),
            )
    return _result(
        event,
        status=EventStatus.FAIL,
        summary="Web 未找到发送验证码按钮（DOM/文案）。请 tap_element 点发送或检查页面。",
        error="web send control not found",
        executor="internal",
        elapsed_ms=int((time.time() - t0) * 1000),
    )


def _request_sms_code(
    event: PlanEvent,
    *,
    ctx: Any,
    router: Any,
    t0: float,
) -> EventResult:
    from mino_nexus.loop.ui_channel import UiChannel, ui_channel_from_ctx, ui_channel_label
    from mino_nexus.loop.ui_consent import any_focused_input
    from mino_nexus.services.otp_resolve import login_kind_from_secrets

    from mino_nexus.loop.ui_sms_request import (
        credential_field_filled,
        credential_field_for_login,
        find_send_code_button,
        tap_params_for_send_button,
    )

    nodes = _hierarchy_nodes(ctx)
    _env, secrets, _otp = _otp_context(ctx)
    login_kind = login_kind_from_secrets(secrets)
    channel = ui_channel_from_ctx(ctx)
    ch_key = ui_channel_label(channel)
    anchor = credential_field_for_login(nodes, login_kind=login_kind, channel=ch_key)
    filled = credential_field_filled(nodes, login_kind=login_kind, channel=ch_key)
    if channel == UiChannel.WEB and filled and anchor is None:
        return _web_tap_send_code(event, ctx=ctx, router=router, t0=t0)
    if anchor is None:
        if login_kind == "email":
            msg = "未找到邮箱输入框。请先 input_text(field=email) 填入租号邮箱。"
            err = "email field not found"
        else:
            msg = "未找到宽手机号输入框。请先 input_text 填入手机号，或 tap_element 聚焦输入框。"
            err = "phone field not found"
        return _result(
            event,
            status=EventStatus.FAIL,
            summary=msg,
            error=err,
            executor="internal",
            elapsed_ms=int((time.time() - t0) * 1000),
        )
    if not filled:
        if login_kind == "email":
            msg = "邮箱框尚无有效地址。请先 input_text(field=email) 填租号邮箱。"
            err = "email not filled"
        else:
            msg = "手机号输入框尚无 11 位号码。请先 input_text(field=phone) 或粘贴已租账号手机号。"
            err = "phone not filled"
        return _result(
            event,
            status=EventStatus.FAIL,
            summary=msg,
            error=err,
            executor="internal",
            elapsed_ms=int((time.time() - t0) * 1000),
        )
    btn = find_send_code_button(nodes, anchor)
    if btn is None:
        if channel == UiChannel.WEB:
            return _web_tap_send_code(event, ctx=ctx, router=router, t0=t0)
        return _result(
            event,
            status=EventStatus.FAIL,
            summary="未找到手机号同行右侧的发送控件。请 tap_element 点右侧短文案按钮。",
            error="send control not found",
            executor="internal",
            elapsed_ms=int((time.time() - t0) * 1000),
        )
    if router is None:
        return _result(
            event,
            status=EventStatus.FAIL,
            summary="未连接设备，无法点击发送控件",
            error="no router",
            executor="internal",
            elapsed_ms=int((time.time() - t0) * 1000),
        )
    if channel != UiChannel.WEB and any_focused_input(nodes):
        back = _dispatch_device(
            router,
            ctx=ctx,
            seq=event.seq,
            cap="press_key",
            params={"key": "BACK"},
            label="收起输入法后再点发送",
        )
        if not _exec_ok(back):
            return _result(
                event,
                status=EventStatus.FAIL,
                summary=f"收起输入法失败：{back.summary or back.error}",
                error=str(back.error or back.summary or "press_key failed"),
                executor="internal",
                elapsed_ms=int((time.time() - t0) * 1000),
            )
    tap = _dispatch_device(
        router,
        ctx=ctx,
        seq=event.seq,
        cap="tap_element",
        params=tap_params_for_send_button(btn, nodes),
        label="点发送验证码控件",
    )
    if _exec_ok(tap):
        from mino_nexus.loop.login_verification import record_verification_send

        record_verification_send(ctx)
        return _result(
            event,
            status=EventStatus.PASS,
            summary="已点击手机号同行右侧发送控件",
            executor="internal+adb",
            elapsed_ms=int((time.time() - t0) * 1000),
        )
    return _result(
        event,
        status=EventStatus.FAIL,
        summary=f"点击发送控件失败：{tap.summary or tap.error}",
        error=str(tap.error or tap.summary or "tap failed"),
        executor="internal",
        elapsed_ms=int((time.time() - t0) * 1000),
    )


def _dismiss_ime(
    event: PlanEvent,
    *,
    ctx: Any,
    router: Any,
    t0: float,
) -> EventResult:
    from mino_nexus.loop.ui_consent import any_focused_input

    nodes = _hierarchy_nodes(ctx)
    if not any_focused_input(nodes):
        return _result(
            event,
            status=EventStatus.PASS,
            summary="未检测到聚焦输入框，未发 BACK",
            executor="internal",
            elapsed_ms=int((time.time() - t0) * 1000),
        )
    if router is None:
        return _result(
            event,
            status=EventStatus.FAIL,
            summary="未连接设备，无法收起输入法",
            error="no router",
            executor="internal",
            elapsed_ms=int((time.time() - t0) * 1000),
        )
    back = _dispatch_device(
        router,
        ctx=ctx,
        seq=event.seq,
        cap="press_key",
        params={"key": "BACK"},
        label="收起输入法",
    )
    if _exec_ok(back):
        return _result(
            event,
            status=EventStatus.PASS,
            summary="已 BACK 收起输入法",
            executor="internal+adb",
            elapsed_ms=int((time.time() - t0) * 1000),
        )
    return _result(
        event,
        status=EventStatus.FAIL,
        summary=f"收起输入法失败：{back.summary or back.error}",
        error=str(back.error or back.summary or "press_key failed"),
        executor="internal",
        elapsed_ms=int((time.time() - t0) * 1000),
    )


def _fsm_navigate(
    event: PlanEvent,
    *,
    ctx: Any,
    t0: float,
    router: Any = None,
) -> EventResult:
    """当前屏 → 目标屏：规划最短路并执行第一步 tap（非仅 advise）。"""
    from mino_nexus.services import nav_route
    from mino_nexus.services.nav_state_resolve import plan_route_resolved

    params = dict(event.params or {})
    from_raw = str(
        params.get("from_state")
        or params.get("current_state")
        or params.get("from")
        or ""
    ).strip()
    to_raw = str(
        params.get("to_state")
        or params.get("expected_state")
        or params.get("goal_state")
        or params.get("to")
        or ""
    ).strip()
    localized = getattr(ctx, "nav_localized", None)
    if not isinstance(localized, dict):
        localized = {}
    conf = float(getattr(ctx, "nav_localized_confidence", 0) or 0)
    chosen = str(getattr(ctx, "nav_localized_state", "") or "").strip()
    if not from_raw and chosen and conf >= 0.35:
        from_raw = chosen
    app_id = str(params.get("app_id") or getattr(ctx, "app_id", "") or "").strip()
    project_id = str(
        params.get("project_id")
        or getattr(ctx, "nav_project_id", "")
        or ""
    ).strip()

    def _attempt(**kw: Any) -> dict[str, Any]:
        base = {
            "from": from_raw,
            "to": to_raw,
            "degraded": False,
        }
        base.update(kw)
        return base

    def _degrade(summary: str, error: str, attempt: dict[str, Any]) -> EventResult:
        attempt["degraded"] = True
        return _result(
            event,
            status=EventStatus.DECLINED,
            summary=summary,
            error=error,
            executor="internal",
            elapsed_ms=int((time.time() - t0) * 1000),
            raw_response={
                "local_reason": "fsm_degraded",
                "correction_hint": _FSM_DEGRADE_HINT,
                "nav_attempt": attempt,
            },
        )

    if not app_id:
        return _result(
            event,
            status=EventStatus.FAIL,
            summary="缺少 app_id，无法查路线图",
            error="missing app_id",
            elapsed_ms=int((time.time() - t0) * 1000),
        )
    if not to_raw:
        return _result(
            event,
            status=EventStatus.FAIL,
            summary="需要 to_state/expected_state（目标逻辑页或 state_id）",
            error="missing to_state",
            elapsed_ms=int((time.time() - t0) * 1000),
        )
    fsm_doc, _ = nav_route.load_fsm_doc(
        app_id,
        project_id=project_id,
        use_live=False,
        app_version=str(getattr(ctx, "app_version", "") or ""),
    )
    fsm = fsm_doc or {}
    if not fsm:
        return _degrade(
            "没有 NavFSM 配置，无法规划路线",
            "missing fsm",
            _attempt(plan_ok=False, plan_error="missing fsm"),
        )

    from mino_nexus.services.nav_route import coerce_oral_nav_ref

    to_raw = coerce_oral_nav_ref(fsm, to_raw)
    if from_raw:
        from_raw = coerce_oral_nav_ref(fsm, from_raw)

    fork = getattr(ctx, "nav_guest_tab_fork", None)
    if fork is not None:
        from mino_nexus.loop.nav_session_fork import fsm_goal_conflicts_with_guest_fork

        nodes_fork = list(getattr(ctx, "nav_hierarchy_nodes", None) or [])
        block, fork_hint = fsm_goal_conflicts_with_guest_fork(
            fork=fork,
            to_raw=to_raw,
            hierarchy_nodes=nodes_fork,
            localized=localized,
        )
        if block:
            return _result(
                event,
                status=EventStatus.DECLINED,
                summary=fork_hint,
                error="guest_tab_login_fork",
                executor="internal",
                elapsed_ms=int((time.time() - t0) * 1000),
                raw_response={
                    "local_reason": "nav_guest_fork",
                    "correction_hint": fork_hint,
                    "nav_attempt": _attempt(plan_ok=False, plan_error="guest_tab_login_fork"),
                },
            )

    scene = getattr(ctx, "case_scene", None) if hasattr(ctx, "case_scene") else {}
    if not isinstance(scene, dict):
        scene = {}
    cur_instr = str(getattr(getattr(ctx, "cursor", None), "instruction", "") or "")
    cur_exp = str(getattr(getattr(ctx, "cursor", None), "expected", "") or "")
    from mino_nexus.loop.nav_session_fork import (
        fsm_blocked_logged_in_session_drift,
        required_session_from_scene,
    )

    req_sess = required_session_from_scene(scene)
    nodes_drift = list(getattr(ctx, "nav_hierarchy_nodes", None) or [])
    drift_block, drift_hint = fsm_blocked_logged_in_session_drift(
        required_session=req_sess,
        instruction=cur_instr,
        expected=cur_exp,
        to_raw=to_raw,
        hierarchy_nodes=nodes_drift,
        localized=localized,
    )
    if drift_block:
        return _result(
            event,
            status=EventStatus.DECLINED,
            summary=drift_hint,
            error="logged_in_session_drift",
            executor="internal",
            elapsed_ms=int((time.time() - t0) * 1000),
            raw_response={
                "local_reason": "logged_in_session_drift",
                "correction_hint": drift_hint,
                "nav_attempt": _attempt(plan_ok=False, plan_error="logged_in_session_drift"),
            },
        )

    plan_msg = ""
    step_cap = "tap_element"
    step_params: dict[str, Any] = {}
    nav_attempt = _attempt()
    first: dict[str, Any] = {}

    if from_raw:
        plan = plan_route_resolved(
            fsm,
            from_ref=from_raw,
            to_ref=to_raw,
            localized=localized,
        )
        resolve = plan.get("resolve") if isinstance(plan.get("resolve"), dict) else {}
        nav_attempt.update(
            {
                "resolved_from": resolve.get("resolved_from") or "",
                "resolved_to": resolve.get("resolved_to") or "",
                "name_score": {
                    "from": resolve.get("from_name_score"),
                    "to": resolve.get("to_name_score"),
                },
                "screen_score": resolve.get("from_screen_score"),
            }
        )
        if plan.get("ok"):
            hops = int(plan.get("hop_count") or 0)
            summary = str(plan.get("summary") or "")
            nav_attempt["plan_ok"] = True
            if hops == 0:
                click_label = nav_route.click_label_from_nav_ref(to_raw)
                if click_label:
                    plan_msg = (
                        f"图上已在 {plan.get('to_state')}，仍按目标文案点击「{click_label}」"
                    )
                    step_cap = "tap_element"
                    step_params = {"selector_text": click_label, "text": click_label}
                    nav_attempt["plan_error"] = ""
                    nav_attempt["step_pick"] = "same_page_click_label"
                    nav_attempt["planned_hops"] = 0
                    nav_attempt["fallback_tab"] = click_label
                else:
                    plan_msg = f"已在目标屏 {plan.get('to_state')}"
                    nav_attempt["plan_error"] = ""
                    nav_attempt["arrived_at_target"] = True
                    nav_attempt["hops_remaining"] = 0
                    hits = getattr(ctx, "_fsm_same_page_hits", None)
                    if not isinstance(hits, dict):
                        hits = {}
                        if ctx is not None:
                            setattr(ctx, "_fsm_same_page_hits", hits)
                    same_key = (
                        f"{nav_attempt.get('resolved_from') or from_raw}"
                        f"->{nav_attempt.get('resolved_to') or to_raw}"
                    )
                    n = int(hits.get(same_key, 0) or 0) + 1
                    hits[same_key] = n
                    if n > 1:
                        return _degrade(
                            f"{plan_msg}。{_FSM_SAME_PAGE_HINT}",
                            "same_page_repeat",
                            nav_attempt,
                        )
                    return _result(
                        event,
                        status=EventStatus.PASS,
                        summary=f"{plan_msg}。{_FSM_SAME_PAGE_HINT}",
                        executor="internal",
                        elapsed_ms=int((time.time() - t0) * 1000),
                        raw_response={
                            "nav_attempt": nav_attempt,
                            "correction_hint": _FSM_SAME_PAGE_HINT,
                        },
                    )
            else:
                first, pick_meta = nav_route.pick_fsm_first_step(fsm, plan)
                nav_attempt["planned_hops"] = int(pick_meta.get("planned_hops") or hops)
                nav_attempt["step_pick"] = str(pick_meta.get("step_pick") or "")
                step_cap, step_params = nav_route.dispatch_spec_for_edge(fsm, first)
                if step_cap == "press_key" and str(step_params.get("key") or "").upper() == "BACK":
                    if not nav_route.edge_is_system_back(fsm, first):
                        return _degrade(
                            f"{summary}；首 hop 非系统返回边，禁止 BACK（D1-C）。"
                            "请再次 fsm_navigate 或 tap 目标 Tab/入口。",
                            "back_not_on_route",
                            nav_attempt,
                        )
                    nav_attempt["allow_back"] = True
                plan_msg = (
                    f"规划 {hops} 步：{summary}；本步 {first.get('edge_id') or ''} "
                    f"→ {first.get('to') or ''}"
                )
                dest_tab = nav_route.tab_root_label_for_state(
                    fsm, str(nav_attempt.get("resolved_to") or plan.get("to_state") or "")
                )
                if not dest_tab and nav_route.is_oral_home_ref(to_raw):
                    dest_tab = nav_route.oral_home_tab_label(fsm)
                if not dest_tab:
                    want = _fold_tap_label(to_raw)
                    dest_tab = next(
                        (lab for lab in nav_route.tab_slot_labels(fsm) if _fold_tap_label(lab) == want),
                        "",
                    )
                if dest_tab:
                    already_tab = _params_tap_label(step_params) == _fold_tap_label(dest_tab)
                    tabs_visible = not _skip_tab_fallback(localized, ctx)
                    if tabs_visible and not already_tab:
                        step_cap = "tap_element"
                        step_params = {"selector_text": dest_tab, "text": dest_tab}
                        nav_attempt["step_pick"] = "tab_bar_visible_direct"
                        plan_msg = f"{plan_msg}；底栏可见，本步直点 Tab「{dest_tab}」"
                    elif (not tabs_visible) and step_cap == "tap_element" and not already_tab:
                        if first and nav_route.edge_is_system_back(fsm, first):
                            step_cap = "press_key"
                            step_params = {"key": "BACK"}
                            nav_attempt["step_pick"] = "tab_target_system_back"
                            plan_msg = (
                                f"{plan_msg}；路线图首 hop 为系统返回，"
                                "本步 press_key BACK 后再 fsm_navigate"
                            )
                        else:
                            nav_attempt["allow_back"] = False
                            return _degrade(
                                f"{plan_msg}；目标 Tab「{dest_tab}」但无底栏且首 hop 非返回边，"
                                "请 tap_element 沿 onboarding 主流程出墙或探索补边（D1-C·开环）。",
                                "tab_not_visible_no_back_edge",
                                nav_attempt,
                            )
        else:
            err = str(plan.get("error") or "无路径")
            nav_attempt["plan_ok"] = False
            nav_attempt["plan_error"] = err
            if _skip_tab_fallback(localized, ctx):
                two_stage = nav_route.tab_root_entry_hint(
                    fsm, resolve.get("resolved_to") or to_raw
                )
                hint = f"{err}；{two_stage or '无 nav 路线（L1 降级），请 tap 探索或补 Atlas 边。'}"
                nav_attempt["degrade_level"] = 1
                return _degrade(hint, err, nav_attempt)
            else:
                step_params = nav_route.direct_tab_tap_params(
                    fsm, resolve.get("resolved_to") or to_raw
                )
                step_cap = "tap_element"
                if step_params:
                    plan_msg = (
                        f"{err}；尝试直接点击 Tab「{step_params.get('selector_text') or ''}」"
                    )
                    nav_attempt["fallback_tab"] = step_params.get("selector_text") or ""
                else:
                    return _degrade(err, err, nav_attempt)
    else:
        plan_msg = f"未提供当前屏，直接尝试点击目标 Tab「{to_raw}」"
        step_params = nav_route.direct_tab_tap_params(fsm, to_raw)
        step_cap = "tap_element"
        nav_attempt["plan_ok"] = False
        nav_attempt["plan_error"] = "missing from_state"
        if not step_params:
            return _degrade(
                "需要 from_state/current_state，或提供可识别的目标 Tab 文案",
                "missing from_state",
                nav_attempt,
            )

    if not step_params:
        msg = f"{plan_msg or '无路径'}（边未配置 target_page，无法执行）"
        nav_attempt["plan_ok"] = False
        nav_attempt.setdefault("plan_error", "missing tap params")
        return _degrade(msg, "missing tap params", nav_attempt)

    if step_cap == "tap_element":
        nodes = getattr(ctx, "nav_hierarchy_nodes", None)
        if isinstance(nodes, list) and nodes:
            from mino_nexus.loop.tap_enrich import enrich_tap_params

            step_params = enrich_tap_params(
                step_params,
                nodes,
                hint=str(step_params.get("selector_text") or ""),
            )
            from mino_nexus.ai.coords import lift_selector_target

            lift_selector_target(step_params)

    if step_cap == "tap_element" and isinstance(nodes, list) and nodes:
        if not _tap_params_has_point(step_params) and not _tap_selector_likely_on_screen(
            step_params, nodes
        ):
            sel = str(step_params.get("selector_text") or step_params.get("text") or "").strip()
            tabs = _visible_tab_labels(ctx)
            hint_tabs = f"可见底栏：{' / '.join(tabs[:6])}" if tabs else "当前屏无底栏 Tab 匹配"
            return _degrade(
                f"目标「{sel or to_raw}」不在当前层级，跳过盲目 tap。{hint_tabs}。"
                f"{_FSM_DEGRADE_HINT}",
                "tap_target_not_on_screen",
                nav_attempt,
            )

    if router is None:
        return _result(
            event,
            status=EventStatus.PASS,
            summary=f"{plan_msg}；未连接设备，仅返回规划",
            executor="internal",
            elapsed_ms=int((time.time() - t0) * 1000),
            raw_response={"nav_attempt": nav_attempt},
        )

    from mino_nexus.loop.web.web_env import agent_step_idx

    exec_label = step_params.get("selector_text") or step_params.get("key") or step_cap
    exec_event = PlanEvent(
        seq=event.seq,
        capability_id=step_cap,
        event_kind=step_cap,
        params=step_params,
        ai_reasoning=str(event.ai_reasoning or plan_msg or "fsm_navigate"),
        label=f"FSM→{exec_label}",
    )
    scout_run_id = str(getattr(ctx, "scout_run_id", "") or getattr(ctx, "run_id", "") or "")
    case_seq = int(getattr(ctx, "case_seq", 0) or 0)
    nav_attempt["exec_cap"] = step_cap
    exec_result = router.dispatch(
        exec_event,
        run_id=scout_run_id,
        step_idx=agent_step_idx(case_seq, event.seq),
    )
    exec_st = exec_result.status.value if hasattr(exec_result.status, "value") else str(exec_result.status)
    exec_summary = str(exec_result.summary or exec_result.error or "")
    nav_attempt["tap_status"] = exec_st
    if step_cap == "tap_element":
        nav_attempt["fallback_tab"] = step_params.get("selector_text") or ""
    if exec_st in ("pass", "done"):
        verb = "已按键" if step_cap == "press_key" else f"已点击「{step_params.get('selector_text') or ''}」"
        resolved_to = str(nav_attempt.get("resolved_to") or "").strip()
        first_to = ""
        if from_raw and nav_attempt.get("plan_ok"):
            first_to = str((first or {}).get("to") or "").strip()
        planned_hops = int(nav_attempt.get("planned_hops") or 0)
        arrived = bool(resolved_to and first_to and first_to == resolved_to)
        nav_attempt["arrived_at_target"] = arrived
        nav_attempt["hops_remaining"] = 0 if arrived else max(0, planned_hops - 1)
        raw: dict[str, Any] = {"nav_attempt": nav_attempt}
        summary = f"{plan_msg}；{verb}"
        recover_hint = str(nav_attempt.get("recover_hint") or "")
        if recover_hint:
            raw["correction_hint"] = recover_hint
        elif not arrived and planned_hops > 1:
            progress = (
                f"【导航进行中】本步为路线图第 1/{planned_hops} 步，未到目标「{to_raw}」；"
                f"请再次 fsm_navigate（当前为 Tab 根时可直点目标 Tab）。"
            )
            summary = f"{summary}（{progress}）"
            raw["correction_hint"] = progress
        return _result(
            event,
            status=EventStatus.PASS,
            summary=summary,
            executor="internal+adb",
            elapsed_ms=int((time.time() - t0) * 1000),
            raw_response=raw,
        )
    fail_verb = "按键失败" if step_cap == "press_key" else "点击失败"
    return _degrade(
        f"{plan_msg}；{fail_verb}：{exec_summary}",
        exec_summary or "exec failed",
        nav_attempt,
    )


def _check_run_env(event: PlanEvent, *, ctx: Any, t0: float) -> EventResult:
    """确认本任务运行环境（每 run 一次），摘要供后续用例引用。"""
    from mino_nexus.catalog import registry as catalog_reg
    from mino_nexus.runtime.env_names import canon_run_env
    from mino_nexus.services import run_store

    run_id = str(getattr(ctx, "run_id", "") or "").strip() if ctx is not None else ""
    doc = run_store.get(run_id) if run_id else None
    existing = str((doc or {}).get("env_brief") or getattr(ctx, "env_label", "") or "").strip()
    if existing:
        if ctx is not None:
            ctx.env_label = existing
            ctx.env_fact = {"brief": existing, "confirmed": True}
        return _result(
            event,
            status=EventStatus.PASS,
            summary=existing,
            executor="internal",
            elapsed_ms=int((time.time() - t0) * 1000),
        )

    env_raw = str((doc or {}).get("env_profile") or getattr(ctx, "env_profile", "") or "test")
    env = canon_run_env(env_raw) or "test"
    platform = str(getattr(ctx, "platform", "") or "android") if ctx is not None else "android"
    flags = getattr(ctx, "connectivity_flags", None) if ctx is not None else {}
    has_otp = any(
        cap.id == "get_otp"
        for cap in catalog_reg.filter_capabilities(
            flags or {"internal": True},
            kinds=["generic", "recovery"],
            platform=platform,
        )
    )
    parts = [f"env={env}", f"platform={platform}", f"channel={platform}"]
    parts.append("otp_via=get_otp" if has_otp else "otp_via=manual")
    sn = str(getattr(ctx, "sn", "") or "").strip() if ctx is not None else ""
    if sn:
        parts.append(f"sn={sn}")
    brief = "; ".join(parts)
    if ctx is not None:
        ctx.env_profile = env
        ctx.env_label = brief
        ctx.env_fact = {
            "brief": brief,
            "env": env,
            "platform": platform,
            "otp_via": "get_otp" if has_otp else "manual",
            "confirmed": True,
        }
    if run_id and doc is not None:
        patched = dict(doc)
        patched["env_brief"] = brief
        run_store.put(patched)
    return _result(
        event,
        status=EventStatus.PASS,
        summary=brief,
        executor="internal",
        elapsed_ms=int((time.time() - t0) * 1000),
    )


def _wait_screen_ready(
    event: PlanEvent,
    *,
    shot: Any,
    ctx: Any,
    router: Any,
    target_package: str,
    t0: float,
) -> EventResult:
    from mino_nexus.loop.recovery import recover_if_needed
    from mino_nexus.loop.screen_capture import analyze_shot, shot_usable

    elapsed = lambda: int((time.time() - t0) * 1000)
    facts = analyze_shot(shot)
    need_recovery = facts.get("capture_ok") == "no" or facts.get("capture_black") == "yes"

    if not need_recovery:
        return _result(
            event,
            status=EventStatus.PASS,
            summary="屏幕内容可读",
            elapsed_ms=elapsed(),
        )

    if ctx is not None and router is not None:
        out = recover_if_needed(ctx, router, target_package=target_package, shot=shot)
        if out and out.recovered and hasattr(router, "observe"):
            fresh = router.observe("screenshot", force_fresh=True)
            if shot_usable(fresh):
                summary = out.summary() if callable(getattr(out, "summary", None)) else out.rule_id
                return _result(
                    event,
                    status=EventStatus.PASS,
                    summary=f"恢复后屏幕可读：{summary}",
                    elapsed_ms=elapsed(),
                )

    if router is not None and hasattr(router, "observe"):
        ms = min(int((event.params or {}).get("timeout_ms") or 3000), 15_000)
        if ms > 0:
            time.sleep(ms / 1000.0)
            fresh = router.observe("screenshot", force_fresh=True)
            if shot_usable(fresh):
                return _result(
                    event,
                    status=EventStatus.PASS,
                    summary=f"等待 {ms}ms 后屏幕可读",
                    elapsed_ms=elapsed(),
                )

    detail = (
        f"capture_ok={facts.get('capture_ok')} capture_black={facts.get('capture_black')}"
    )
    return _result(
        event,
        status=EventStatus.FAIL,
        summary=f"截图不可用或全黑，recovery 未能恢复（{detail}）",
        error="screen not readable",
        elapsed_ms=elapsed(),
    )


__all__ = ["LOCAL_CAPS", "LOCAL_CAP_PREFIXES", "dispatch_local", "is_local_cap"]
