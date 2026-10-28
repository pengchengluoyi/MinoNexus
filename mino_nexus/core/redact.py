"""读模型脱敏工具：只做展示层裁剪，不能替代「不查询/不返回」。

约束：
- 账号密码、OTP、渠道密钥、设备 session_key 等凭证字段一律不进入 MCP。
- 手机号/邮箱默认掩码；保留末 4 位/域名，供人工核对。
- 递归处理日志 detail 等自由结构，避免嵌套字段绕过。
"""
from __future__ import annotations

import re
from typing import Any

SECRET_KEYS = frozenset({
    "password", "passwd", "pwd", "otp", "secret", "secrets", "token",
    "access_token", "refresh_token", "id_token", "cookie", "cookies",
    "authorization", "session_key", "storage_state", "credential",
    "credentials", "channel_secrets", "api_key", "apikey", "private_key",
    "client_secret", "install_token", "mcp_token",
    # 日志 detail 里的收件地址/账号标识也按 PII 丢弃；facet 的
    # address 是状态枚举（filled/none），不在此列。
    "phone", "email", "username", "inbox", "to_address", "from_address",
    "account_ident", "identity_hint",
})

_SECRET_SUFFIXES = ("_password", "_passwd", "_secret", "_token", "_api_key")
_PHONE = re.compile(r"^\+?\d{7,15}$")


def is_secret_key(key: Any) -> bool:
    low = str(key or "").strip().lower()
    return low in SECRET_KEYS or low.endswith(_SECRET_SUFFIXES)


def mask_ident(value: Any) -> tuple[str, str]:
    """返回 (掩码值, 末四位)。手机号/邮箱/其它账号标识统一掩码。

    其它标识也保留末四位而不是原样返回：账号标识可能是用户名，不能因为
    不是手机号/邮箱就泄漏。
    """
    text = str(value or "").strip()
    if not text:
        return "", ""
    if _PHONE.fullmatch(text):
        tail = text[-4:]
        return ("*" * max(0, len(text) - 4)) + tail, tail
    if "@" in text and text.count("@") == 1:
        name, _, domain = text.partition("@")
        head = name[:1] if name else ""
        return f"{head}***@{domain}", ""
    if len(text) <= 4:
        return "*" * len(text), ""
    tail = text[-4:]
    return ("*" * (len(text) - 4)) + tail, tail


def redact(value: Any, *, _depth: int = 0, scrub: bool = False) -> Any:
    """递归丢弃凭证键；不改原对象。`scrub=True` 时同时掩码自由文本里的手机号/邮箱。"""
    if _depth > 12:
        return None
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if is_secret_key(key):
                continue
            out[str(key)] = redact(item, _depth=_depth + 1, scrub=scrub)
        return out
    if isinstance(value, (list, tuple)):
        return [redact(item, _depth=_depth + 1, scrub=scrub) for item in value]
    if isinstance(value, str):
        return scrub_text(value, 2000) if scrub else value[:2000]
    return value


def text(value: Any, limit: int = 500) -> str:
    return str(value or "").strip()[:limit]


def as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


_PHONE_IN_TEXT = re.compile(r"(?<![0-9A-Za-z_-])(\+?\d{7,15})(?![0-9A-Za-z_-])")
_EMAIL_IN_TEXT = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def scrub_text(value: Any, limit: int = 500) -> str:
    """把自由文本里的手机号/邮箱替换成掩码，保留可读上下文。"""
    raw = str(value or "")[: max(1, int(limit))]

    def _phone(match: re.Match[str]) -> str:
        got = match.group(1)
        return ("*" * max(0, len(got) - 4)) + got[-4:]

    def _email(match: re.Match[str]) -> str:
        name, _, domain = match.group(0).partition("@")
        return f"{name[:1]}***@{domain}"

    return _EMAIL_IN_TEXT.sub(_email, _PHONE_IN_TEXT.sub(_phone, raw))
