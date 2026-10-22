"""入站消息端口：节点收到 IM 消息后交给助手域处理。

`resources/node_registry` 不该 import `services.assistant_gateway`——
资源域不认识助手。助手在 import 时把自己注册成 handler。
"""
from __future__ import annotations

from typing import Any, Callable

_handlers: list[Callable[[dict], None]] = []


def register_inbound_handler(handler: Callable[[dict], None]) -> None:
    if handler not in _handlers:
        _handlers.append(handler)


def publish_inbound(payload: dict[str, Any]) -> None:
    for handler in tuple(_handlers):
        handler(payload)
