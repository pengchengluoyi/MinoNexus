"""领域事件出口（端口）：业务把事件发出去，不关心谁在收。

反过来的写法——业务 `from mino_nexus.api.ws import observers`——会让
`services` / `loop` 反向依赖 HTTP 层，包级环就永远拆不掉。
UI 实时通道在 import 时把自己注册成 sink。
"""
from __future__ import annotations

from typing import Any, Callable

_sinks: list[Callable[[dict], None]] = []


def register_event_sink(sink: Callable[[dict], None]) -> None:
    if sink not in _sinks:
        _sinks.append(sink)


def publish_event(payload: dict[str, Any]) -> None:
    for sink in tuple(_sinks):
        sink(payload)
