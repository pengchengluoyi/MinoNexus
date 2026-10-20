"""执行账号来自认证会话或持久任务；绑定是局部、不可变且必定复原的。"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from typing import Any, Iterator

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


def owned_job(owner_field: str = "owner_user_id"):
    """从参数里的持久任务归属恢复上下文，未归属的旧任务拒绝执行。"""
    from functools import wraps
    def decorate(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            from mino_nexus.services import run_store
            doc = run_store.get(str(kwargs.get("run_id") or "")) or kwargs.get("doc") or {}
            owner = str(doc.get(owner_field) or "")
            if not owner:
                if doc.get("run_id"):
                    run_store.finish(doc["run_id"], status="failed", error="历史任务缺少归属，已拒绝执行")
                raise PermissionError("缺少执行账号，已拒绝执行")
            with execution_user(owner):
                from mino_nexus.ai import dispatch_log
                token = dispatch_log.bind(task_id=str(doc.get("run_id") or kwargs.get("run_id") or ""),
                                          trigger="case_run", source="case_run", job="case-initialization")
                try:
                    return fn(*args, **kwargs)
                finally:
                    dispatch_log.reset(token)
        return wrapped
    return decorate


def user_thread(*, target, args=(), kwargs=None, **options) -> threading.Thread:
    """在创建时固化已验证账号，而不是在线程启动时读取共享状态。"""
    require_user_id()
    context = copy_context()
    return threading.Thread(target=context.run, args=(target, *args), kwargs=kwargs or {}, **options)


class ExecutionUserMiddleware:
    """纯 ASGI 绑定，确保 sync endpoint、async endpoint 和 to_thread 均继承认证账号。"""
    def __init__(self, app: Any):
        self.app = app

    async def __call__(self, scope, receive, send):
        token = _user.set("")
        try:
            if scope["type"] == "http":
                from fastapi import HTTPException
                from mino_nexus.routers.deps import current_session
                from starlette.concurrency import run_in_threadpool
                authorization = dict(scope.get("headers") or []).get(b"authorization", b"").decode("latin1")
                if authorization:
                    try:
                        sess = await run_in_threadpool(current_session, authorization)
                        _user.set(str(sess.get("user_id") or ""))
                    except HTTPException:
                        pass  # 具体 endpoint 的认证依赖返回统一的 401
            await self.app(scope, receive, send)
        finally:
            _user.reset(token)
