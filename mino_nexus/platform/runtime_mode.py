"""V5 运行时模式：进入执行期后控件注册表只读，避免边跑边改配置。"""
from __future__ import annotations

from contextvars import ContextVar

_RUNTIME_MODE: ContextVar[str] = ContextVar("control_runtime_mode", default="")


from mino_nexus.contract.errors import RuntimeReadOnlyError


def runtime_readonly() -> bool:
    return _RUNTIME_MODE.get() == "v5"


def assert_writable(reason: str = "v5_runtime") -> None:
    if runtime_readonly():
        raise RuntimeReadOnlyError(reason)


def set_runtime_mode(mode: str):
    return _RUNTIME_MODE.set(str(mode or ""))


def reset_runtime_mode(token) -> None:
    _RUNTIME_MODE.reset(token)
