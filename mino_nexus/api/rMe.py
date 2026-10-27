"""登录后一次拉齐身份与门禁。"""
from __future__ import annotations

from fastapi import APIRouter, Header

from mino_nexus.platform import auth_store as auth
from mino_nexus.core.http_util import bearer, http_error, ok

router = APIRouter(tags=["Me"])


@router.get("/me/bootstrap")
def me_bootstrap(
    authorization: str = Header(default=""),
    x_mino_client: str = Header(default="", alias="X-Mino-Client"),
):
    try:
        data = auth.bootstrap(bearer(authorization), client=x_mino_client)
    except Exception as e:
        http_error(e)
    return ok(data)
