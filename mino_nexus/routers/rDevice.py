"""设备清单与锁屏密码。权威连通性来自 Scout，密码存在本地 json。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from mino_nexus.services.device_secrets import set_lock_password
from mino_nexus.core.http_util import http_error, ok
from mino_nexus.routers.deps import current_session
from mino_nexus.services.ui_devices import ui_devices

router = APIRouter(prefix="/device", tags=["Device"])


class SetPasswordBody(BaseModel):
    sn: str = ""
    password: str = ""


@router.get("/list")
def get_device_list(_sess: dict = Depends(current_session)):
    return ui_devices()


@router.post("/command")
def send_command(_sess: dict = Depends(current_session)):
    raise HTTPException(status_code=501, detail="设备指令经 Scout 执行，HTTP /device/command 已不再提供。")


@router.post("/set_password")
def set_password(body: SetPasswordBody, _sess: dict = Depends(current_session)):
    try:
        return ok(set_lock_password(body.sn, body.password), msg="已保存")
    except ValueError as exc:
        http_error(exc)
