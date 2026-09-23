"""登录发码/取码：按 UiChannel × login.kind 区分规则与文案。

能力名仍叫 request_sms_code（目录兼容），Web 邮箱场景下等价于点 Send 发邮件验证码，不是手机短信。
"""
from __future__ import annotations

import time
from typing import Any

from mino_nexus.loop.ui_channel import UiChannel, ui_channel_from_ctx


def record_verification_send(ctx: Any) -> None:
    """发码成功（request_sms_code 或 Web Send tap）时写入，供 Gmail get_otp 等使用。"""
    if ctx is not None:
        ctx.otp_sent_at = time.time()


def login_kind_for_ctx(ctx: Any | None) -> str:
    if ctx is None:
        return "phone"
    try:
        from mino_nexus.loop.local_executors import _otp_context
        from mino_nexus.services.otp_resolve import login_kind_from_secrets

        _doc, secrets, _otp = _otp_context(ctx)
        return login_kind_from_secrets(secrets)
    except Exception:
        return "phone"


def is_web_email_login(ctx: Any | None) -> bool:
    if ctx is None:
        return False
    return ui_channel_from_ctx(ctx) == UiChannel.WEB and login_kind_for_ctx(ctx) == "email"


def verification_send_required_message(ctx: Any | None) -> str:
    """守卫拦截 get_otp / 填码前的说明。"""
    ch = ui_channel_from_ctx(ctx) if ctx is not None else UiChannel.ANDROID
    kind = login_kind_for_ctx(ctx)
    if ch == UiChannel.WEB and kind == "email":
        return (
            "须先 input_text(field=email) 填租号邮箱，再点 Send 或 request_sms_code 向邮箱发验证码，"
            "再 get_otp。Web 邮箱登录无手机短信。"
        )
    if kind == "email":
        return (
            "邮箱登录须先 input_text(field=email) 填入租号邮箱，再 request_sms_code 发码，最后 get_otp。"
        )
    return (
        "须先 request_sms_code 成功发送短信验证码，再 get_otp / 填验证码。禁止跳过发送步骤。"
    )


def otp_not_sent_executor_summary(ctx: Any | None) -> str:
    """get_otp 在 otp_sent_at 未置位时的 fail summary。"""
    ch = ui_channel_from_ctx(ctx) if ctx is not None else UiChannel.ANDROID
    kind = login_kind_for_ctx(ctx)
    if ch == UiChannel.WEB and kind == "email":
        return (
            "尚未向邮箱发码：请先 input_text(邮箱)，再点 Send 或 request_sms_code，然后 get_otp"
        )
    if kind == "email":
        return "尚未发码：请先完成 input_text(邮箱) 与 request_sms_code，再 get_otp"
    return "尚未发码：请先 request_sms_code 发送短信验证码，再 get_otp"


def compile_verification_send_menu_hint(
    *,
    ctx: Any | None,
    accounts_brief: str = "",
    hierarchy_nodes: list[dict[str, Any]] | None = None,
    has_request_sms_code: bool = False,
    ui_channel: str = "android_hierarchy",
) -> str:
    """凭证已填且屏上仍有发送控件时，按渠道×登录方式注入菜单提示。"""
    brief = str(accounts_brief or "").strip()
    if not brief or "未租" in brief or brief.startswith("（未租"):
        return ""
    if not has_request_sms_code:
        return ""
    from mino_nexus.loop.ui_sms_request import (
        credential_field_for_login,
        credential_field_filled,
        find_send_code_button,
    )

    nodes = [n for n in (hierarchy_nodes or []) if isinstance(n, dict)]
    ch = str(ui_channel or "android_hierarchy").strip().lower()
    kind = login_kind_for_ctx(ctx)
    if not nodes or not credential_field_filled(nodes, login_kind=kind, channel=ch):
        return ""
    anchor = credential_field_for_login(nodes, login_kind=kind, channel=ch)
    if anchor is None:
        if kind == "email" and ch in ("web_dom", "web", "playwright"):
            return (
                "【邮箱登录·Web】邮箱已填入：点 Send 或 request_sms_code 发邮件验证码，"
                "再 get_otp 取码填入验证码框。"
            )
        return ""
    if find_send_code_button(nodes, anchor) is None:
        if kind == "email" and ch in ("web_dom", "web", "playwright"):
            return (
                "【邮箱登录·Web】邮箱已填入：点 Send 或 request_sms_code 发邮件验证码，"
                "再 get_otp 取码。"
            )
        return ""
    if kind == "email":
        if ch in ("web_dom", "web", "playwright"):
            return (
                "【邮箱登录·Web】邮箱已填入且屏上有发送控件："
                "点 Send 或 request_sms_code，再 get_otp 填验证码；勿跳过发码。"
            )
        return (
            "【邮箱登录】邮箱已填入且屏上有发送控件："
            "先 request_sms_code，再 get_otp 取码并 input_text 填入验证码框。"
        )
    return (
        "【短信登录】手机号已填入且右侧仍有发送控件："
        "先 request_sms_code，再 get_otp 取码并 input_text 填入验证码框；"
        "勿跳过发送步骤直接填码。"
    )
