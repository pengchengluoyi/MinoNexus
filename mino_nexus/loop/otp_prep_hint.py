"""OTP 前置提示：凭证已填但屏上仍有发送控件时的引导文案。"""
from __future__ import annotations

import re


from typing import Any


def compile_otp_prep_hint(
    *,
    accounts_brief: str = "",
    session_block: str = "",
    hierarchy_text: str = "",
    has_get_otp: bool = False,
    has_hitl: bool = False,
) -> str:
    """已租号且处于验证码页时，引导 get_otp 而非盲填固定码。"""
    brief = str(accounts_brief or "").strip()
    if not brief or "未租" in brief or brief.startswith("（未租"):
        return ""
    blob = f"{session_block}\n{hierarchy_text}"
    if not re.search(r"验证码|短信|OTP|sms", blob, re.I):
        return ""
    fallback = "请 signal_ask_human 获取验证码。" if has_hitl else "请确认账号池/环境已配置验证码。"
    if has_get_otp:
        fail_to = "signal_ask_human" if has_hitl else "确认账号 otp 或环境 otp.fixed"
        return (
            "【验证码】已租号且屏上为验证码/短信流程：优先 get_otp 取码填入，"
            f"禁止盲填知识库固定码；取码失败再 {fail_to}。"
        )
    return f"【验证码】已租号且屏上为验证码流程：勿重复 input_text 固定码，{fallback}"
