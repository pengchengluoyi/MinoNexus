"""一次跑批的显式状态：`_run_loop` 里那些跨阶段的本地量与闭包。

为什么要有它：`_run_loop` 原本是 4900 行单函数，所有阶段共用一个巨大的局部作用域，
任何"把某一段搬出去"的动作都会退化成二十个参数的函数。状态对象是唯一能把这段代码
分期拆开的载体——先有 `RunState`，再谈阶段外提。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from mino_nexus.loop.execution.setup import RunSetup


@dataclass
class RunState:
    setup: RunSetup
    ctx: Any
    proxy: Any
    writer: Any
    emit: Callable[..., None]
    history: list[str] = field(default_factory=list)
    scout_run_id: str = ""
    t0: float = 0.0
    case_seq: int = 0
    resolved_case: Any = None
    login_module_case: bool = False
    cancel_check: Any = None
    leave: Any = None
    rec: Any = None
    screen_unregistered: Any = None
