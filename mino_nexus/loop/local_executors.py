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
    from mino_nexus.services.local_tool_dependencies import evaluate
    dependencies = evaluate(cap, ctx=ctx, shot=shot, execution=True)
    if dependencies:
        from mino_nexus.loop.observe.session_log import active_writer
        writer = active_writer()
        if writer is not None:
            writer.append("dependency/check", {"capability_id": cap, "facts": dependencies})
        unavailable = next((f for f in dependencies if f["status"] != "ready"), None)
        if unavailable:
            return _result(event, status=EventStatus.BLOCKED,
                summary="本地工具依赖未满足：" + unavailable["reason_code"],
                error=unavailable["reason_code"], raw_response={"dependencies": dependencies},
                elapsed_ms=int((time.time() - t0) * 1000))
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
            provider_id=str(getattr(ctx, "provider_id", "") or "") or None,
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
    if cap == "confirm_login_state":
        from mino_nexus.loop.login_state_probe import observe_login_state, remember_session
        from mino_nexus.runtime.session_gate import required_session as required_session_enum

        scene = getattr(ctx, "case_scene", None) if ctx is not None else None
        req = required_session_enum(scene=scene)
        session, reason = observe_login_state(ctx, router, shot)
        summary = reason or f"session={session}"
        if req == "guest" and session == "logged_in":
            summary = "当前已登录，与访客前置不符；需显式退出流程，观察工具不执行退出。"
        elif req == "logged_in" and session != "logged_in":
            summary = f"当前 session={session}，登录流程仍需执行。{reason}"
        src = str(getattr(ctx, "device_session_source", "") or "")
        if src == "tool_api":
            executor = "playwright"
        elif src == "screen":
            executor = "vlm"
        else:
            executor = "internal"
        known = session in {"logged_in", "logged_out", "guest"}
        mismatch = req == "guest" and session == "logged_in"
        error = "login_observation_unavailable" if not known else "session_requirement_mismatch" if mismatch else ""
        return _result(
            event,
            status=EventStatus.BLOCKED if not known else EventStatus.FAIL if mismatch else EventStatus.PASS,
            summary=summary[:500],
            error=error,
            executor=executor,
            elapsed_ms=int((time.time() - t0) * 1000),
        )
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
    if cap == "operation.check_box":
        from mino_nexus.loop.check_box import execute
        return execute(event, ctx=ctx, router=router, t0=t0)
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
        from mino_nexus.services.otp_resolve import resolve_gmail_inbox

        if resolve_gmail_inbox(env_doc, ctx):
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
    from mino_nexus.services.otp_binding import request_params, OTP_CHILD
    params = request_params(ctx)
    event = PlanEvent(
        seq=0,
        capability_id="plugin.gmail.fetch_otp",
        event_kind="plugin.gmail.fetch_otp",
        params=params,
        label="Gmail 取码",
    )
    private_result = {}
    token = OTP_CHILD.set((id(router), dict(params), private_result))
    try:
        result = router.dispatch(
            event,
            run_id=str(getattr(ctx, "scout_run_id", "") or getattr(ctx, "run_id", "") or "") if ctx else "",
            step_idx=-1,
            ctx=ctx,
        )
    finally:
        OTP_CHILD.reset(token)
    status = result.status.value if hasattr(result.status, "value") else str(result.status)
    raw = dict(getattr(result, "raw_response", None) or {})
    if status not in ("pass", "done"):
        raise GmailOtpError(str(getattr(result, "summary", "") or getattr(result, "error", "") or hint))
    code = str(private_result.get("code") or raw.get("code") or "").strip()
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
    from mino_nexus.core.secret_redaction import remember
    remember(code)
    if ctx is not None:
        acc = dict(getattr(ctx, "picked_account", None) or {})
        acc["otp"] = code
        ctx.picked_account = acc
        setattr(ctx, "otp_fetch_fail_streak", 0)
    return _result(
        event,
        status=EventStatus.PASS,
        summary=f"已取验证码（{source}）",
        executor="internal",
        elapsed_ms=int((time.time() - t0) * 1000),
        raw_response={"otp_available": True, "source": source},
    )


def _dismiss_ime(
    event: PlanEvent,
    *,
    ctx: Any,
    router: Any,
    t0: float,
) -> EventResult:
    from mino_nexus.loop.ui_channel import UiChannel, ui_channel_from_ctx
    from mino_nexus.loop.ui_consent import any_focused_input

    if ui_channel_from_ctx(ctx) == UiChannel.WEB:
        return _result(
            event,
            status=EventStatus.PASS,
            summary="Web 无需用 BACK 收起输入法，未派发按键",
            executor="internal",
            elapsed_ms=int((time.time() - t0) * 1000),
        )
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
    if getattr(ctx, "recovery_write_stopped", False):
        return _result(event, status=EventStatus.BLOCKED,
            summary="恢复动作已停止，设备与账号资源仍隔离", error="run_write_stopped",
            raw_response={"execution_control": {"stop_run": True, "resource_isolated": True,
                "reason_code": "run_write_stopped"}})

    elapsed = lambda: int((time.time() - t0) * 1000)
    facts = analyze_shot(shot)
    need_recovery = facts.get("capture_ok") == "no" or facts.get("capture_black") == "yes"

    if not need_recovery:
        cur = getattr(ctx, "cursor", None)
        pending = False
        if cur is not None:
            from mino_nexus.loop.visual_review import has_visual_prior

            pending = has_visual_prior(cur)
        if pending and router is not None and hasattr(router, "observe"):
            time.sleep(1.5)
            fresh = router.observe("screenshot", force_fresh=True)
            if shot_usable(fresh):
                return _result(
                    event,
                    status=EventStatus.PASS,
                    summary="等待加载后再看画面",
                    elapsed_ms=elapsed(),
                )
        return _result(
            event,
            status=EventStatus.PASS,
            summary="屏幕内容可读",
            elapsed_ms=elapsed(),
        )

    out = None
    if ctx is not None and router is not None:
        out = recover_if_needed(ctx, router, target_package=target_package, shot=shot)
        if out and out.stop_run:
            return _result(event, status=EventStatus.BLOCKED, summary=out.summary(),
                error=out.error, raw_response=out.raw_response, elapsed_ms=elapsed())
        if out and (out.applied or out.recovered) and hasattr(router, "observe"):
            fresh = router.observe("screenshot", force_fresh=True)
            facts = analyze_shot(fresh)
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
            facts = analyze_shot(fresh)
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
    if ctx is None or router is None:
        reason = "缺少设备恢复上下文，无法下发恢复动作"
    elif out is None:
        reason = "没有匹配到屏幕恢复规则，未下发恢复动作"
    elif out.mode == "advise":
        reason = "当前恢复规则仅提供处理建议，未下发恢复动作"
        if out.advice:
            reason += f"：{out.advice[:200]}"
    elif out.applied:
        reason = "已下发恢复动作，但最新截图仍不可读"
    else:
        reason = "已匹配恢复规则，但没有下发可执行的恢复动作"
    diagnostics = {"screen": facts, "recovery": {
        "matched": out is not None, "rule_id": out.rule_id if out else "",
        "mode": out.mode if out else "", "applied": bool(out and out.applied),
        "recovered": bool(out and out.recovered), "attempts": out.attempts if out else 0,
        "actions": [{"capability": a.get("capability"), "status": a.get("status"),
                     "skipped": a.get("skipped", "")} for a in (out.actions if out else [])],
    }}
    return _result(
        event,
        status=EventStatus.FAIL,
        summary=f"屏幕仍不可读：{reason}（{detail}）",
        error="screen not readable",
        elapsed_ms=elapsed(),
        raw_response=diagnostics,
    )


__all__ = ["LOCAL_CAPS", "LOCAL_CAP_PREFIXES", "dispatch_local", "is_local_cap"]
