"""Login business steps, invoked by the login flow; never registered as capabilities."""
from typing import Any
import time
from mino_nexus.core.schemas import PlanEvent, EventResult
from mino_nexus.core.protocol import EventStatus
from mino_nexus.loop.local_executors import _result, _dispatch_device, _exec_ok, _hierarchy_nodes, _otp_context

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
    from mino_nexus.services.nav_flow_block_catalog import resolve_effective_steps
    steps = resolve_effective_steps(app_id=str(getattr(ctx, 'app_id', '') or ''), block_id='fb.global.login')
    step = next((row for row in steps if row.get('id') == 'send_code'), {})
    labels = {str(x).strip().lower() for x in step.get('match', [])}
    exclude = {str(x).strip().lower() for x in step.get('exclude', [])}
    nodes = _hierarchy_nodes(ctx)
    hits = [n for n in nodes if str(n.get('text') or n.get('content_desc') or '').strip().lower() in labels
            and str(n.get('text') or n.get('content_desc') or '').strip().lower() not in exclude]
    if len(hits) != 1:
        return _result(event, status=EventStatus.BLOCKED, summary='login_send_target_not_unique', executor='internal')
    from mino_nexus.loop.ui_consent import tap_params_for_control
    tap = _dispatch_device(router, ctx=ctx, seq=event.seq, cap='tap_element',
        params=tap_params_for_control(hits[0], nodes), label='登录块：发送验证码')
    if _exec_ok(tap):
        from mino_nexus.loop.login_verification import record_verification_send
        record_verification_send(ctx)
    return tap


def send_code(
    event: PlanEvent,
    *,
    ctx: Any,
    router: Any,
    t0: float,
) -> EventResult:
    from mino_nexus.contract.execution.ui import UiChannel, ui_channel_from_ctx, ui_channel_label
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
    from mino_nexus.loop.recovery import collect_evidence, invalidate_evidence_cache
    if channel == UiChannel.ANDROID and collect_evidence(ctx, router,
            target_package=str(getattr(ctx, 'target_package', '') or '')).ime_shown == 'yes':
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
                raw_response=back.raw_response,
                error=str(back.error or back.summary or "press_key failed"),
                executor="internal",
                elapsed_ms=int((time.time() - t0) * 1000),
            )
        from mino_nexus.loop.channel_observation import bind_fresh_observation
        try:
            invalidate_evidence_cache(ctx)
            ctx.nav_hierarchy_nodes = []
            bind_fresh_observation(ctx, router)
        except Exception:
            return _result(event, status=EventStatus.FAIL, summary='login_send_observation_failed',
                           raw_response=back.raw_response)
        nodes = _hierarchy_nodes(ctx)
        anchor = credential_field_for_login(nodes, login_kind=login_kind, channel=ch_key)
        btn = find_send_code_button(nodes, anchor) if anchor is not None else None
        if btn is None or not credential_field_filled(nodes, login_kind=login_kind, channel=ch_key):
            return _result(event, status=EventStatus.BLOCKED, summary='login_send_requires_fresh_target', executor='internal', raw_response=back.raw_response)
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
            executor=tap.executor_used,
            raw_response=tap.raw_response,
            elapsed_ms=int((time.time() - t0) * 1000),
        )
    return _result(
        event,
        status=EventStatus.FAIL,
        summary=f"点击发送控件失败：{tap.summary or tap.error}",
        raw_response=tap.raw_response,
        error=str(tap.error or tap.summary or "tap failed"),
        executor="internal",
        elapsed_ms=int((time.time() - t0) * 1000),
    )
