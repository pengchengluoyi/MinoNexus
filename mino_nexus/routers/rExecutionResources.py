from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from mino_nexus.core.database import session_scope
from mino_nexus.core.http_util import ok
from mino_nexus.routers.deps import current_session, project_maintainer
from mino_nexus.services.case_compilation import owned
from mino_nexus.models.execution_resource import ResourceDomain, ExecutionResource

router = APIRouter(prefix="/project/{project_id}/execution-resources", dependencies=[Depends(current_session)])


class DomainBody(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=100)
    env: str = Field(min_length=1, max_length=32)
    app_id: str = Field(default="", max_length=100)
    account_ids: list[str] = Field(default_factory=list, max_length=1000)
    enabled: bool = True


@router.get("")
def listing(project_id: str):
    with session_scope() as db:
        actor = owned(db, project_id, write=False)
        domains = db.query(ResourceDomain).filter_by(project_id=project_id).order_by(ResourceDomain.name).all()
        leases = db.query(ExecutionResource).filter_by(project_id=project_id, owner_user_id=actor).filter(ExecutionResource.state != "released").all()
        from mino_nexus.models.project import App
        from mino_nexus.models.pool_account import PoolAccount
        apps = db.query(App).filter_by(project_id=project_id).order_by(App.name).all()
        accounts = db.query(PoolAccount).filter_by(project_id=project_id).order_by(PoolAccount.display_name).all()
        return ok({"domains": [{key: getattr(row, key) for key in ("domain_id", "name", "env", "app_id", "account_ids", "enabled")} for row in domains],
            "leases": [{key: getattr(row, key) for key in ("resource_key", "run_id", "execution_unit_id", "attempt_id", "sn", "epoch", "expires_at", "state")} for row in leases],
            "apps": [{"id": row.id, "name": row.name} for row in apps],
            "accounts": [{"id": row.id, "name": row.display_name or row.id, "env": row.env} for row in accounts]})


@router.put("/domains/{domain_id}", dependencies=[Depends(project_maintainer)])
def save_domain(project_id: str, domain_id: str, body: DomainBody):
    import uuid
    from mino_nexus.models.project import App
    from mino_nexus.models.pool_account import PoolAccount
    with session_scope() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        owned(db, project_id, write=True)
        row = db.get(ResourceDomain, domain_id)
        if row and row.project_id != project_id:
            raise HTTPException(404, "resource_domain_missing")
        if body.app_id:
            app = db.get(App, body.app_id)
            if app is None or app.project_id != project_id:
                raise HTTPException(400, "domain_application_scope_mismatch")
        if body.account_ids and db.query(PoolAccount).filter(PoolAccount.project_id == project_id, PoolAccount.id.in_(body.account_ids), PoolAccount.env == body.env).count() != len(set(body.account_ids)):
            raise HTTPException(400, "domain_account_scope_mismatch")
        if db.query(ExecutionResource).filter_by(resource_key="domain:" + domain_id).filter(ExecutionResource.state != "released").first():
            raise HTTPException(409, "resource_domain_busy")
        if row is None:
            row = ResourceDomain(domain_id="domain-" + uuid.uuid4().hex, project_id=project_id)
            db.add(row)
        for key, value in body.model_dump().items():
            setattr(row, key, value)
        return ok({"domain_id": row.domain_id})


class RecoveryProof(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    resource_key: str = Field(min_length=1, max_length=300)
    session_id: str = Field(min_length=1, max_length=200)
    seq: int = Field(ge=0)
    statement: str = Field(min_length=5, max_length=500)


class RecoveryBody(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    request_id: str = Field(min_length=1, max_length=100)
    expected_digest: str = Field(min_length=64, max_length=64)
    reason: str = Field(min_length=5, max_length=500)
    proofs: list[RecoveryProof] = Field(min_length=1, max_length=200)


@router.get("/runs/{run_id}/recovery", dependencies=[Depends(project_maintainer)])
def recovery_preview(project_id: str, run_id: str):
    from mino_nexus.services.resource_recovery import preview
    try:
        return ok(preview(project_id, run_id))
    except ValueError as exc:
        raise HTTPException(409, detail=str(exc)) from exc


@router.post("/runs/{run_id}/recovery", dependencies=[Depends(project_maintainer)])
def recover_resources(project_id: str, run_id: str, body: RecoveryBody):
    from mino_nexus.services.resource_recovery import recover
    try:
        return ok(recover(project_id, run_id, body))
    except ValueError as exc:
        raise HTTPException(409, detail=str(exc)) from exc


@router.post("/runs/{run_id}/recovery/observe", dependencies=[Depends(project_maintainer)])
async def observe_recovery(project_id: str, run_id: str):
    from mino_nexus.services.resource_recovery import observe
    try:
        return ok(await observe(project_id, run_id))
    except (ValueError, PermissionError) as exc:
        raise HTTPException(409, detail=str(exc)) from exc
