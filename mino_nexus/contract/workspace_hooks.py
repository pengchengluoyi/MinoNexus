"""跑批结束后的工作区通知端口。

用例台账发出 `run_finished`，工作区/自动化在 import 时注册处理函数；
台账不 import 工作区，避免 `casework` 反向依赖上层业务包。
"""
from __future__ import annotations

from typing import Any, Callable

_hooks: list[Callable[[dict], None]] = []


def register_run_finished(fn: Callable[[dict], None]) -> None:
    if fn not in _hooks:
        _hooks.append(fn)


def publish_run_finished(doc: dict[str, Any]) -> None:
    for fn in tuple(_hooks):
        try:
            fn(doc)
        except Exception:
            from mino_nexus.core.log import SLog
            SLog.w("WorkspaceHooks", f"hook {getattr(fn, '__name__', fn)} failed")
