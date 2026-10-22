from __future__ import annotations
from typing import Any, Literal
from fastapi import APIRouter, BackgroundTasks, Depends
from pydantic import BaseModel, ConfigDict, Field
from mino_nexus.core.http_util import ok
from mino_nexus.api.deps import current_session
from mino_nexus.api import development as svc

router = APIRouter(prefix="/project/app/{app_id}/development", tags=["Development"])


class SaveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=0)
    config: svc.Binding


class OperationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{8,100}$")
    action: Literal["check", "execute", "stop", "restart", "build", "install", "logs", "probe"]
    revision: int = Field(ge=1)


class ConnectionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{8,100}$")
    node_id: str
    action: Literal["save_connection", "list_connections"]
    connection_id: str = ""
    connection_revision: int = Field(default=0, ge=0)
    connection: dict[str, Any] = Field(default_factory=dict)


class ManualBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmed_idle: Literal[True]


@router.get("")
def get_binding(app_id: str, sess: dict = Depends(current_session)):
    return ok(svc.binding_get(app_id, sess))


@router.put("")
def save_binding(app_id: str, body: SaveBody, sess: dict = Depends(current_session)):
    return ok(svc.binding_save(app_id, sess, body.revision, body.config))


@router.post("/operations")
def prepare(app_id: str, body: OperationBody, background: BackgroundTasks, sess: dict = Depends(current_session)):
    result, created = svc.create_operation(app_id, sess, body.operation_id, body.action, body.revision)
    return ok(result)


@router.post("/connections")
def connection(app_id: str, body: ConnectionBody, background: BackgroundTasks, sess: dict = Depends(current_session)):
    result, created = svc.create_operation(app_id, sess, body.operation_id, body.action, 0,
                                          body.model_dump(exclude={"operation_id", "action"}))
    return ok(result)


@router.get("/operations")
def operations(app_id: str, sess: dict = Depends(current_session)):
    return ok({"items": svc.list_operations(app_id, sess)})


@router.get("/operations/{operation_id}")
def operation(app_id: str, operation_id: str, sess: dict = Depends(current_session)):
    return ok(svc.get_operation(app_id, operation_id, sess))


@router.post("/operations/{operation_id}/recover")
async def recover(app_id: str, operation_id: str, sess: dict = Depends(current_session)):
    return ok(await svc.recover_operation(app_id, operation_id, sess))


@router.post("/operations/{operation_id}/manual-review")
def manual_review(app_id: str, operation_id: str, body: ManualBody, sess: dict = Depends(current_session)):
    return ok(svc.acknowledge(app_id, operation_id, sess))
