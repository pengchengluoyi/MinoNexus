"""HTTP 层中间件：把认证会话里的执行账号绑定到本次请求的上下文。"""
from __future__ import annotations

from typing import Any

from mino_nexus.contract.ownership import bind_user, reset_user


class ExecutionUserMiddleware:
    """纯 ASGI 绑定，确保 sync endpoint、async endpoint 和 to_thread 均继承认证账号。"""
    def __init__(self, app: Any):
        self.app = app

    async def __call__(self, scope, receive, send):
        token = bind_user("")
        try:
            if scope["type"] == "http":
                from mino_nexus.platform.session_auth import resolve_session
                from starlette.concurrency import run_in_threadpool
                authorization = dict(scope.get("headers") or []).get(b"authorization", b"").decode("latin1")
                if authorization:
                    try:
                        sess = await run_in_threadpool(resolve_session, authorization)
                        bind_user(str(sess.get("user_id") or ""))
                    except Exception:
                        pass  # 具体 endpoint 的认证依赖返回统一的 401
            await self.app(scope, receive, send)
        finally:
            reset_user(token)
