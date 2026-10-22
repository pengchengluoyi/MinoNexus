"""执行账号的上下文原语：纯 contextvar，无 I/O、无框架依赖。

从 `core/execution_user.py` 拆出，因为 `records/eventlog` 需要 `require_user_id()`
给事件打归属，而 records 只能依赖 core/contract/models。
HTTP 绑定（`ExecutionUserMiddleware`）与持久任务归属（`owned_job`）仍在
`platform/identity.py` / `api/middleware.py`，它们需要框架与业务。
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

_user: ContextVar[str] = ContextVar("execution_user", default="")


def current_user_id() -> str:
    return _user.get()


def require_user_id(user_id: str | None = None) -> str:
    uid = str(user_id or current_user_id()).strip()
    if not uid:
        raise PermissionError("缺少执行账号，已拒绝执行")
    if current_user_id() and uid != current_user_id():
        raise PermissionError("执行账号不匹配")
    return uid


@contextmanager
def execution_user(user_id: str) -> Iterator[str]:
    uid = require_user_id(user_id)
    token = _user.set(uid)
    try:
        yield uid
    finally:
        _user.reset(token)


def bind_user(user_id: str) -> object:
    """供中间件设置当前账号；返回值交给 `reset_user`。"""
    return _user.set(str(user_id or ""))


def reset_user(token: object) -> None:
    _user.reset(token)  # type: ignore[arg-type]
