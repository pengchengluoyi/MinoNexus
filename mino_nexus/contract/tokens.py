"""API/助手 token 解析端口。

`platform.session_auth` 需要识别助手 token，但不该 import 助手域。
助手在 import 时注册解析器；返回 None 表示"不是助手 token"，继续走普通会话。
"""
from __future__ import annotations

from typing import Any, Callable

_resolver: Callable[[str], Any] | None = None


def register_token_resolver(fn: Callable[[str], Any]) -> None:
    global _resolver
    _resolver = fn


def resolve_token_session(raw: str) -> dict | None:
    if _resolver is None:
        return None
    return _resolver(raw)
