"""Administrator-only Workbench updates through the registered local helper."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from mino_nexus.core.http_util import ok
from mino_nexus.routers.deps import current_session
from mino_nexus.services.auth_store import is_admin_role
from mino_nexus.services.workbench_updates import WorkbenchInstallation, WorkbenchUpdateError

router = APIRouter(prefix="/sys/workbench", tags=["Workbench maintenance"])


def administrator(sess: dict = Depends(current_session)):
    if not is_admin_role(str(sess.get("role", ""))):
        raise HTTPException(403, "需要部署管理员权限")


def invoke(fn):
    try:
        return ok(fn())
    except WorkbenchUpdateError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(409, "登记安装、固定版本暂存包或原操作不存在；不会自动创建替代操作") from exc
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise HTTPException(409, "Workbench 维护文件不可读或合同无效，请检查本机登记与操作记录") from exc


class OperationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    installation_id: str = Field(min_length=1, max_length=101, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    operation_id: str = Field(min_length=1, max_length=101, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class UpdateBody(OperationBody):
    build_id: str = Field(min_length=1, max_length=101, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    rollback: bool = False


@router.get("/installation", dependencies=[Depends(administrator)])
def installation():
    return invoke(lambda: WorkbenchInstallation().info())


@router.get("/updates/check", dependencies=[Depends(administrator)])
def check(installation_id: str, build_id: str = "", release_tag: str = ""):
    return invoke(lambda: WorkbenchInstallation(installation_id).check(build_id, release_tag))


@router.get("/updates/status", dependencies=[Depends(administrator)])
def status(installation_id: str, operation_id: str):
    return invoke(lambda: WorkbenchInstallation(installation_id).status(operation_id))


@router.post("/updates/request", dependencies=[Depends(administrator)])
def update(body: UpdateBody):
    return invoke(lambda: WorkbenchInstallation(body.installation_id).request(body.operation_id, body.build_id, body.rollback))


@router.post("/updates/cancel", dependencies=[Depends(administrator)])
def cancel(body: OperationBody):
    return invoke(lambda: WorkbenchInstallation(body.installation_id).cancel(body.operation_id))


@router.post("/updates/recover", dependencies=[Depends(administrator)])
def recover(body: OperationBody):
    return invoke(lambda: WorkbenchInstallation(body.installation_id).resume(body.operation_id))


class PrepareBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    installation_id: str = Field(min_length=1, max_length=101)
    release_tag: str = Field(min_length=1, max_length=101)
    build_id: str = Field(min_length=1, max_length=101)


@router.post("/updates/prepare", dependencies=[Depends(administrator)])
def prepare(body: PrepareBody):
    return invoke(lambda: WorkbenchInstallation(body.installation_id).prepare(body.release_tag, body.build_id))
