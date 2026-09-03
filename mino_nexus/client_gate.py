"""`X-Mino-Client: console|studio` 写接口门禁。完整 RBAC 之前先按产品切面拦。"""
from __future__ import annotations

import re
from typing import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

# (path 前缀正则, 被禁的方法)
_CONSOLE_DENY: list[tuple[str, set[str]]] = [
    (r"^/settings/ai/providers(/|$)", {"PUT", "POST", "DELETE"}),
    (r"^/settings/ai/usage$", {"PUT", "POST"}),
    (r"^/case-runner(/|$)", {"POST", "PUT", "DELETE"}),
    (r"^/project(/|$)", {"POST", "PUT", "DELETE"}),
    (r"^/hitl(/|$)", {"POST", "PUT", "DELETE"}),
    (r"^/schedule(/|$)", {"POST", "PUT", "DELETE"}),
    (r"^/app-automation(/|$)", {"POST", "PUT", "PATCH", "DELETE"}),
    (r"^/task(/|$)", {"POST", "PUT", "DELETE"}),
    (r"^/runtime/nodes/install-token$", {"POST"}),
    (r"^/runtime/nodes/[^/]+/command$", {"POST"}),
]
_STUDIO_DENY: list[tuple[str, set[str]]] = [
    (r"^/auth/users(/|$)", {"POST", "PUT", "DELETE"}),
    (r"^/packs(/|$)", {"POST", "PUT", "DELETE"}),
    (r"^/settings/mail(/|$)", {"PUT", "POST"}),
    (r"^/settings/plugins/[^/]+$", {"PUT", "DELETE"}),
    (r"^/settings/plugins/(feishu|wechat|zentao)(/|$)", {"POST"}),
    (r"^/settings/robots(/|$)", {"POST", "PUT", "DELETE"}),
    (r"^/settings/figma(/|$)", {"PUT", "POST"}),
    (r"^/me/studio-nav$", {"PUT"}),
]


def _hit(rules: list[tuple[str, set[str]]], path: str, method: str) -> bool:
    for pattern, methods in rules:
        if method in methods and re.search(pattern, path):
            return True
    return False


class ClientGateMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        if request.method == "OPTIONS":
            return await call_next(request)
        client = str(request.headers.get("x-mino-client") or "").strip().lower()
        path = request.url.path
        method = request.method.upper()
        if client == "console" and _hit(_CONSOLE_DENY, path, method):
            return JSONResponse(
                {"detail": "控制台不能改模型密钥或下发用例。这些在 Mino Studio。"},
                status_code=403,
            )
        if client == "studio" and _hit(_STUDIO_DENY, path, method):
            return JSONResponse(
                {"detail": "工作台不能改账号、发信邮箱、扩展包或插件配置。这些在 Mino Console。"},
                status_code=403,
            )
        return await call_next(request)
