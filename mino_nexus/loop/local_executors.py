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
    "请 press_back 退出栈顶，或 recover_restart_target_app 冷启动后再点 Tab；勿重复盲目 fsm_navigate。"
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
        from mino_nexus.ai.planner import inspect_session
        from mino_nexus.runtime.session_gate import (
            format_required_session_brief,
            required_session as required_session_enum,
        )

        image = ""
        mime = "image/png"
        if shot is not None and getattr(shot, "has_image", lambda: False)():
            image = shot.image_base64
            mime = getattr(shot, "image_mime", None) or "image/png"
        scene = getattr(ctx, "case_scene", None) if ctx is not None else None
        req = required_session_enum(scene=scene)
        row = inspect_session(
            required_session=format_required_session_brief(scene),
            accounts_brief=str(getattr(ctx, "accounts_brief", "") or "") if ctx else "",
            image_base64=image,
            image_mime=mime,
            screen_w=int(getattr(shot, "width", 0) or 0) if shot else 0,
            screen_h=int(getattr(shot, "height", 0) or 0) if shot else 0,
        )
        session = str(row.get("session") or "unknown").strip().lower()
        reason = str(row.get("reason") or "").strip()
        if not row.get("ok"):
            summary = reason or "会话观察失败"
            return _result(
                event,
                status=EventStatus.FAIL,
                summary=summary,
                error=summary,
                executor="vlm",
                elapsed_ms=int((time.time() - t0) * 1000),
            )
        if req == "logged_in":
            if session == "logged_in":
                summary = reason or "session=logged_in"
                status = EventStatus.PASS
            else:
                summary = f"观察完成，登录未完成（session={session}）"
                if reason:
                    summary = f"{summary}；{reason}"
                status = EventStatus.FAIL
        elif req == "guest":
            if session == "logged_in":
                from mino_nexus.loop.session_ensure import try_logout_via_nav

                ok, logout_msg = try_logout_via_nav(ctx, router, seq=event.seq)
                if ok:
                    summary = f"已执行 logout 边（原 session={session}）；{logout_msg}"
                    status = EventStatus.PASS
                else:
                    summary = (
                        f"观察完成，当前仍已登录（session={session}）。{logout_msg}"
                    )
                    if reason:
                        summary = f"{summary}；{reason}"
                    status = EventStatus.FAIL
            else:
                summary = reason or f"会话观察：{session}"
                status = EventStatus.PASS
        else:
            summary = reason or f"会话观察：{session}"
            status = EventStatus.PASS
        return _result(
            event,
            status=status,
            summary=summary,
            error="" if status == EventStatus.PASS else summary,
            executor="vlm",
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
        need = account_need_from_case({}, scene if isinstance(scene, dict) else {})
        row, err = lease_for_context(
            ctx,
            params,
            ai_reasoning=str(event.ai_reasoning or ""),
            need_facets=need,
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
        return _get_otp(event, ctx=ctx, t0=t0)
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
    from mino_nexus.loop.web_env import agent_step_idx

    event = PlanEvent(
        seq=seq,
        capability_id=cap,
        event_kind=cap,
        params=dict(params or {}),
        ai_reasoning=label,
        label=label[:80],
    )
    scout_run_id = str(getattr(ctx, "scout_run_id", "") or getattr(ctx, "run_id", "") or "") if ctx else ""
    case_seq = int(getattr(ctx, "case_seq", 0) or 0) if ctx else 0
    return router.dispatch(event, run_id=scout_run_id, step_idx=agent_step_idx(case_seq, seq))


def _exec_ok(result: Any) -> bool:
    st = result.status.value if hasattr(result.status, "value") else str(result.status)
    return st in ("pass", "done")


def _resolve_otp(ctx: Any) -> tuple[str, str]:
    acc = getattr(ctx, "picked_account", None) if ctx is not None else None
    if isinstance(acc, dict):
        for key in ("otp", "sms_code", "code"):
            val = str(acc.get(key) or "").strip()
            if val:
                return val, "account"
    try:
        from mino_nexus.services import project_store as ps
        from mino_nexus.services.project_env import env_secrets

        app_id = str(getattr(ctx, "app_id", "") or "").strip() if ctx is not None else ""
        if not app_id:
            return "", "missing"
        app = ps.require_app(app_id)
        project_id = str(app.get("project_id") or "").strip()
        if not project_id:
            return "", "missing"
        env_doc = ps.project_env(project_id)
        env_key = str(getattr(ctx, "env_profile", "") or "test") if ctx is not None else "test"
        secrets = env_secrets(env_doc, env_key)
        otp = secrets.get("otp") if isinstance(secrets, dict) else {}
        if not isinstance(otp, dict):
            otp = {}
        fixed = str(otp.get("fixed") or "").strip()
        mode = str(otp.get("mode") or "auto").strip().lower()
        if fixed and mode in ("fixed", "auto"):
            return fixed, "env_fixed"
    except Exception:
        return "", "missing"
    return "", "missing"


def _get_otp(event: PlanEvent, *, ctx: Any, t0: float) -> EventResult:
    code, source = _resolve_otp(ctx)
    if not code:
        return _result(
            event,
            status=EventStatus.FAIL,
            summary="未配置验证码：请在测试账号 otp 字段或项目环境 otp.fixed 填写，勿盲填固定码",
            error="otp not configured",
            executor="internal",
            elapsed_ms=int((time.time() - t0) * 1000),
        )
    if ctx is not None:
        acc = dict(getattr(ctx, "picked_account", None) or {})
        acc["otp"] = code
        ctx.picked_account = acc
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


def _request_sms_code(
    event: PlanEvent,
    *,
    ctx: Any,
    router: Any,
    t0: float,
) -> EventResult:
    from mino_nexus.loop.ui_consent import any_focused_input
    from mino_nexus.loop.ui_sms_request import (
        find_phone_field,
        find_send_code_button,
        phone_field_filled,
        tap_params_for_send_button,
    )

    nodes = _hierarchy_nodes(ctx)
    phone = find_phone_field(nodes)
    if phone is None:
        return _result(
            event,
            status=EventStatus.FAIL,
            summary="未找到宽手机号输入框。请先 input_text 填入手机号，或 tap_element 聚焦输入框。",
            error="phone field not found",
            executor="internal",
            elapsed_ms=int((time.time() - t0) * 1000),
        )
    if not phone_field_filled(nodes):
        return _result(
            event,
            status=EventStatus.FAIL,
            summary="手机号输入框尚无 11 位号码。请先 input_text(field=phone) 或粘贴已租账号手机号。",
            error="phone not filled",
            executor="internal",
            elapsed_ms=int((time.time() - t0) * 1000),
        )
    btn = find_send_code_button(nodes, phone)
    if btn is None:
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
    if any_focused_input(nodes):
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
                plan_msg = (
                    f"规划 {hops} 步：{summary}；本步 {first.get('edge_id') or ''} "
                    f"→ {first.get('to') or ''}"
                )
                dest_tab = nav_route.tab_root_label_for_state(
                    fsm, str(nav_attempt.get("resolved_to") or plan.get("to_state") or "")
                )
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
                        # 最短路常把「去 Tab 页」编成内容区控件（拍照按钮/列表项），
                        # 当前又没有底栏可点，系统返回比瞎点内容更接近 Tab 根。
                        step_cap = "press_key"
                        step_params = {"key": "BACK"}
                        nav_attempt["step_pick"] = "tab_target_press_back"
                        plan_msg = (
                            f"{plan_msg}；目标是底栏 Tab「{dest_tab}」但当前无底栏，"
                            "本步先系统返回，请再次 fsm_navigate"
                        )
        else:
            err = str(plan.get("error") or "无路径")
            nav_attempt["plan_ok"] = False
            nav_attempt["plan_error"] = err
            if _skip_tab_fallback(localized, ctx):
                # 详情/栈顶无底栏：直点 Tab 必败。系统返回是最短可用 hop，
                # 不要 declined 把这一步踢回模型再自己 press_key。
                hits = getattr(ctx, "_fsm_recover_back_hits", None) if ctx is not None else None
                if not isinstance(hits, dict):
                    hits = {}
                    if ctx is not None:
                        setattr(ctx, "_fsm_recover_back_hits", hits)
                n = int(hits.get("back", 0) or 0) + 1
                hits["back"] = n
                two_stage = nav_route.tab_root_entry_hint(
                    fsm, resolve.get("resolved_to") or to_raw
                )
                if n > 3:
                    hint = (
                        f"{err}；当前屏无底栏 Tab，"
                        f"已连续系统返回 {n - 1} 次仍无法规划。"
                        "请 recover_restart_target_app 或 tap_element 直点目标。"
                    )
                    nav_attempt["plan_error"] = hint
                    return _degrade(hint, err, nav_attempt)
                step_cap = "press_key"
                step_params = {"key": "BACK"}
                plan_msg = (
                    f"{err}；当前屏无底栏 Tab，"
                    f"本步先系统返回退出栈顶（{n}/3）"
                )
                if two_stage:
                    plan_msg = f"{plan_msg}，{two_stage}"
                nav_attempt["step_pick"] = "recover_press_back"
                # 没有路线图，就别报「第 1/N 步」—— 那句进度是假的，摘要会像导航已完成。
                nav_attempt["planned_hops"] = 0
                nav_attempt["recover_hint"] = "；".join(
                    x for x in (err, two_stage, "退栈后请再次 fsm_navigate；底栏可见时可直点目标") if x
                )
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

    if router is None:
        return _result(
            event,
            status=EventStatus.PASS,
            summary=f"{plan_msg}；未连接设备，仅返回规划",
            executor="internal",
            elapsed_ms=int((time.time() - t0) * 1000),
            raw_response={"nav_attempt": nav_attempt},
        )

    from mino_nexus.loop.web_env import agent_step_idx

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
            kinds=["prep", "generic", "recovery"],
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
