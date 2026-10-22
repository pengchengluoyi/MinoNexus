"""OTP 发送提示：凭证已填但屏上仍有发送控件时，引导 request_sms_code。"""
from __future__ import annotations

from typing import Any


def compile_sms_send_hint(
    *,
    accounts_brief: str = "",
    hierarchy_nodes: list[dict[str, Any]] | None = None,
    has_request_sms_code: bool = False,
    ui_channel: str = "android",
) -> str:
    """凭证已填且屏上仍有发送控件时，引导 request_sms_code（按渠道选 DOM / 安卓 hierarchy）。"""
    brief = str(accounts_brief or "").strip()
    if not brief or "未租" in brief or brief.startswith("（未租"):
        return ""
    if not has_request_sms_code:
        return ""
    from mino_nexus.navigation.geometry.ui_sms_request import (
        credential_field_for_login,
        credential_field_filled,
        find_send_code_button,
    )

    nodes = [n for n in (hierarchy_nodes or []) if isinstance(n, dict)]
    ch = str(ui_channel or "android").strip().lower()
    kind = "phone"
    if not nodes or not credential_field_filled(nodes, login_kind=kind, channel=ch):
        return ""
    anchor = credential_field_for_login(nodes, login_kind=kind, channel=ch)
    if anchor is None or find_send_code_button(nodes, anchor) is None:
        return ""
    return (
        "【短信登录】手机号已填入且右侧仍有发送控件："
        "先用 tap_element 点击发送验证码控件，确认发码成功后再 get_otp 取码并 input_text 填入验证码框；"
        "勿跳过发送步骤直接填码。"
    )
