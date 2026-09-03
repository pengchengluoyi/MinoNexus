"""UI 契约：axios 拦截器直接吃 `response.data`，业务成功一律 `{code: 200, data}`。"""
from __future__ import annotations

from typing import Any, NoReturn, Optional

from fastapi import Header, HTTPException


def ok(data: Any = None, msg: str = "", **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"code": 200, "ok": True}
    if msg:
        body["msg"] = msg
    if data is not None:
        body["data"] = data
    body.update(extra)
    return body


def bearer(authorization: str = "") -> str:
    raw = str(authorization or "").strip()
    if raw.lower().startswith("bearer "):
        return raw[7:].strip()
    return raw


def http_error(exc: Exception, fallback: int = 400) -> NoReturn:
    if isinstance(exc, PermissionError):
        detail = str(exc)
        code = 403 if ("仅管理员" in detail or "无权" in detail) else 401
        raise HTTPException(status_code=code, detail=detail) from exc
    if isinstance(exc, (RuntimeError, ValueError)):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    raise HTTPException(status_code=fallback, detail=str(exc)) from exc


def client_header(x_mino_client: Optional[str] = Header(default="", alias="X-Mino-Client")) -> str:
    return str(x_mino_client or "").strip().lower()
