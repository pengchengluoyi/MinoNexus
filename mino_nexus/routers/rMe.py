"""登录后一次拉齐身份与门禁。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel

from mino_nexus.services import auth_store as auth
from mino_nexus.services import studio_nav
from mino_nexus.core.http_util import bearer, http_error, ok
from mino_nexus.routers.deps import current_session

router = APIRouter(tags=["Me"])


class StudioNavBody(BaseModel):
    allowed: list[str] = []


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


@router.get("/me/studio-nav")
def get_studio_nav(_sess: dict = Depends(current_session)):
    return ok(studio_nav.public_nav())


@router.put("/me/studio-nav")
def put_studio_nav(body: StudioNavBody, _sess: dict = Depends(current_session)):
    return ok(studio_nav.save_allowed(body.allowed), msg="已保存")
