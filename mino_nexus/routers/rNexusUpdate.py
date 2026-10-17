"""Deployment administration; unavailable to unmanaged source processes."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from mino_nexus.core.deployment import Deployment, deployment_root
from mino_nexus.core.http_util import ok
from mino_nexus.routers.deps import current_session
from mino_nexus.services.auth_store import is_admin_role

router = APIRouter(prefix="/sys/updates", tags=["Nexus maintenance"])


def deployment(sess: dict = Depends(current_session)) -> Deployment:
    if not is_admin_role(str(sess.get("role", ""))):
        raise HTTPException(403, "需要部署管理员权限")
    root = deployment_root()
    if root is None:
        raise HTTPException(409, "当前 Nexus 未登记为独立安装服务，请先完成正式安装基线")
    return Deployment(root)


class UpdateBody(BaseModel):
    operation_id: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    build_id: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    wait_seconds: int = Field(default=1800, ge=1, le=1800)
    rollback: bool = False


def invoke(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except FileNotFoundError as exc:
        raise HTTPException(409, "固定版本尚未暂存或缺少兼容演练记录，请先完成本地部署准备") from exc
    except (ValueError, RuntimeError, BlockingIOError) as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/status")
def status(operation_id: str = "", d: Deployment = Depends(deployment)):
    return ok(invoke(d.operation, operation_id) if operation_id else d.state())


@router.post("/request")
def update(body: UpdateBody, d: Deployment = Depends(deployment)):
    from mino_nexus.core.update_engine import request
    from mino_nexus.core.deployment_cli import kick
    row = invoke(request, d, body.operation_id, body.build_id, wait_seconds=body.wait_seconds, rollback=body.rollback)
    if row["state"] in {"requested", "waiting_idle"}:
        try:
            kick(d)
        except RuntimeError:
            # The operation exists. Do not turn a lost helper acknowledgement into
            # an invitation to create a new update; return its durable identity.
            return ok({**row, "helper_started": False, "recovery": "通过本地 maintenance helper 续接原操作"})
    return ok(row)


@router.post("/{operation_id}/cancel")
def cancel(operation_id: str, d: Deployment = Depends(deployment)):
    from mino_nexus.core.update_engine import cancel as cancel_wait
    return ok(invoke(cancel_wait, d, operation_id))
