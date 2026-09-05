"""账号：邮箱注册 / 登录，以及内部账号密码登录。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel

from mino_nexus.services import auth_store as auth
from mino_nexus.core.http_util import bearer, http_error, ok
from mino_nexus.routers.deps import current_session

router = APIRouter(prefix="/auth", tags=["Auth"])


class AccountBody(BaseModel):
    email: str = ""
    password: str = ""
    name: str = ""
    username: str = ""
    code: str = ""
    role: str = ""


class SendCodeBody(BaseModel):
    email: str = ""
    purpose: str = "register"


class AgentSessionsBody(BaseModel):
    sessions: list[dict[str, Any]] = []


@router.get("/status")
def auth_status(
    authorization: str = Header(default=""),
    x_mino_client: str = Header(default="", alias="X-Mino-Client"),
):
    try:
        return ok(auth.status(bearer(authorization), client=x_mino_client))
    except Exception as e:
        http_error(e)


@router.post("/send-code")
def auth_send_code(body: SendCodeBody):
    try:
        data = auth.send_code(body.email, purpose=body.purpose)
    except Exception as e:
        http_error(e)
    return ok(data, msg="验证码已发送")


@router.post("/register")
def auth_register(body: AccountBody):
    try:
        data = auth.register(
            email=body.email,
            password=body.password,
            name=body.name,
            username=body.username,
            code=body.code,
        )
    except Exception as e:
        http_error(e)
    return ok(data, msg="账号已创建")


@router.get("/users")
def auth_list_users(_sess: dict = Depends(current_session)):
    return ok({"users": auth.list_accounts()})


@router.post("/users")
def auth_create_user(body: AccountBody, _sess: dict = Depends(current_session)):
    try:
        row = auth.create_local_user(
            username=body.username or body.email,
            password=body.password,
            name=body.name,
            email=body.email,
            role=body.role or "user",
        )
    except Exception as e:
        http_error(e)
    return ok(row, msg="账号已添加")


@router.delete("/users/{user_id}")
def auth_delete_user(user_id: str, sess: dict = Depends(current_session)):
    try:
        auth.delete_account(user_id, actor_id=str(sess.get("user_id") or ""))
    except Exception as e:
        http_error(e)
    return ok(msg="已删除")


@router.get("/agent-sessions")
def auth_list_agent_sessions(authorization: str = Header(default="")):
    try:
        rows = auth.list_agent_sessions(bearer(authorization))
    except Exception as e:
        http_error(e)
    return ok({"sessions": rows})


@router.put("/agent-sessions")
def auth_save_agent_sessions(body: AgentSessionsBody, authorization: str = Header(default="")):
    try:
        rows = auth.save_agent_sessions(bearer(authorization), body.sessions)
    except Exception as e:
        http_error(e)
    return ok({"sessions": rows})


@router.post("/login")
def auth_login(
    body: AccountBody,
    x_mino_client: str = Header(default="", alias="X-Mino-Client"),
):
    try:
        data = auth.login(
            email=body.email,
            password=body.password,
            username=body.username,
            client=x_mino_client,
        )
    except Exception as e:
        http_error(e)
    return ok(data, msg="登录成功")


@router.post("/logout")
def auth_logout(authorization: str = Header(default="")):
    auth.logout(bearer(authorization))
    return ok(msg="已退出")
