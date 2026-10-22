"""应用级统计端口：知识条目数由知识域提供，项目域只展示。

未注册时返回 0——这是展示用计数，不影响执行。
"""
from __future__ import annotations

from typing import Callable

_counter: Callable[[str], int] | None = None


def register_knowledge_counter(fn: Callable[[str], int]) -> None:
    global _counter
    _counter = fn


def knowledge_count_for(app_id: str) -> int:
    if _counter is None:
        return 0
    return int(_counter(app_id))
