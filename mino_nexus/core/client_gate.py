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
    (r"^/nav-fsm(/|$)", {"POST", "PUT", "PATCH", "DELETE"}),
    (r"^/task(/|$)", {"POST", "PUT", "DELETE"}),
    (r"^/runtime/nodes/install-token$", {"POST"}),
    (r"^/runtime/nodes/[^/]+/command$", {"POST"}),
    (r"^/runtime/nodes/[^/]+/plugins(/|$)", {"POST"}),
]
_STUDIO_DENY: list[tuple[str, set[str]]] = [
    (r"^/settings/ai/jobs(/|$)", {"PUT", "POST"}),
    (r"^/settings/robots(/|$)", {"POST", "PUT", "DELETE"}),
    (r"^/settings/figma(/|$)", {"PUT", "POST"}),
    (r"^/me/studio-nav$", {"PUT"}),
]


def _hit(rules: list[tuple[str, set[str]]], path: str, method: str) -> bool:
    for pattern, methods in rules:
        if method in methods and re.search(pattern, path):
            return True
    return False


def _api_token_allowed(path: str, method: str) -> bool:
    """助手 token / MCP token 只能走工具表里登记过的路由；删除类一律不放行。"""
    from mino_nexus.services.assistant_tools import route_allowed

    return route_allowed(method, path)


class ClientGateMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        if request.method == "OPTIONS":
            return await call_next(request)
        auth = str(request.headers.get("authorization") or "").strip()
        raw = auth[7:].strip() if auth.lower().startswith("bearer ") else auth
        if raw.startswith(("mino_mcp_", "mino_ast_")):
            if request.url.path == "/mcp" and raw.startswith("mino_mcp_"):
                return await call_next(request)
            if not _api_token_allowed(request.url.path, request.method.upper()):
                return JSONResponse(
                    {"detail": "助手 / MCP 只能调用工具表里登记过的接口"},
                    status_code=403,
                )
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
                {"detail": "该写操作仅允许 Mino Console（如 Jobs 编排、Studio 侧栏策略）。"},
                status_code=403,
            )
        return await call_next(request)
