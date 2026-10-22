"""登录发码/取码：按 UiChannel × login.kind 区分规则与文案。

发送验证码由当前登录块的 tap_element 点击发送控件完成；Web 邮箱场景点击 Send 发邮件验证码。
"""
from __future__ import annotations

import time
from typing import Any

from mino_nexus.contract.execution.ui import UiChannel, ui_channel_from_ctx
from mino_nexus.resources.otp_resolve import is_web_email_login, login_kind_for_ctx  # noqa: F401


def record_verification_send(ctx: Any) -> None:
    """发码成功（tap_element 或 Web Send tap）时写入，供 Gmail get_otp 等使用。"""
    if ctx is not None:
        ctx.otp_sent_at = time.time()




def verification_send_required_message(ctx: Any | None) -> str:
    """守卫拦截 get_otp / 填码前的说明。"""
    ch = ui_channel_from_ctx(ctx) if ctx is not None else UiChannel.ANDROID
    kind = login_kind_for_ctx(ctx)
    if ch == UiChannel.WEB and kind == "email":
        return (
            "须先完成账号填写（account_fill），再用 tap_element 点击 Send 向邮箱发送验证码，"
            "再 get_otp。Web 邮箱登录无手机短信。"
        )
    if kind == "email":
        return (
            "须先完成账号填写（account_fill），再用 tap_element 点击发送验证码控件，确认发码成功后 get_otp。"
        )
    return (
        "须先用 tap_element 点击发送验证码控件并确认发码成功，再 get_otp / 填验证码。禁止跳过发送步骤。"
    )


def otp_not_sent_executor_summary(ctx: Any | None) -> str:
    """get_otp 在 otp_sent_at 未置位时的 fail summary。"""
    ch = ui_channel_from_ctx(ctx) if ctx is not None else UiChannel.ANDROID
    kind = login_kind_for_ctx(ctx)
    if ch == UiChannel.WEB and kind == "email":
        return (
            "尚未向邮箱发码：请先完成账号填写（account_fill），再用 tap_element 点击 Send，确认发码成功后 get_otp"
        )
    if kind == "email":
        return "尚未发码：请先完成账号填写（account_fill），再用 tap_element 点击发送验证码控件，确认发码成功后 get_otp"
    return "尚未发码：请先用 tap_element 点击发送验证码控件，确认发码成功后 get_otp"


def compile_verification_send_menu_hint(
    *,
    ctx: Any | None,
    accounts_brief: str = "",
    hierarchy_nodes: list[dict[str, Any]] | None = None,
    has_tap_element: bool = False,
    ui_channel: str = "android_hierarchy",
) -> str:
    """凭证已填且屏上仍有发送控件时，按渠道×登录方式注入菜单提示。"""
    brief = str(accounts_brief or "").strip()
    if not brief or "未租" in brief or brief.startswith("（未租"):
        return ""
    if not has_tap_element:
        return ""
    from mino_nexus.navigation.geometry.ui_sms_request import (
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
                "【邮箱登录·Web】邮箱已填入：用 tap_element 点击 Send 发邮件验证码，"
                "再 get_otp 取码填入验证码框。"
            )
        return ""
    if find_send_code_button(nodes, anchor) is None:
        if kind == "email" and ch in ("web_dom", "web", "playwright"):
            return (
                "【邮箱登录·Web】邮箱已填入：用 tap_element 点击 Send 发邮件验证码，"
                "再 get_otp 取码。"
            )
        return ""
    if kind == "email":
        if ch in ("web_dom", "web", "playwright"):
            return (
                "【邮箱登录·Web】邮箱已填入且屏上有发送控件："
                "用 tap_element 点击 Send 并确认发码成功，再 get_otp 填验证码；勿跳过发码。"
            )
        return (
            "【邮箱登录】邮箱已填入且屏上有发送控件："
            "先用 tap_element 点击发送验证码控件并确认发码成功，再 get_otp 取码并 input_text 填入验证码框。"
        )
    return (
        "【短信登录】手机号已填入且右侧仍有发送控件："
        "先用 tap_element 点击发送验证码控件并确认发码成功，再 get_otp 取码并 input_text 填入验证码框；"
        "勿跳过发送步骤直接填码。"
    )
