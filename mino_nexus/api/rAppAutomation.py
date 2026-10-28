"""应用配置 / 用例草稿 / qa-process。"""
from __future__ import annotations
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from mino_nexus.resources import app_automation as aas
from mino_nexus.resources import project_store as ps
from mino_nexus.core.http_util import ok
from mino_nexus.api.deps import app_maintainer, current_session

router = APIRouter(prefix="/app-automation", tags=["App Automation"])


class PlaybookSaveBody(BaseModel):
    playbook: dict[str, Any] = {}


class LoginEvidencePolicyBody(BaseModel):
    policy: dict[str, Any]
    expected_policy: dict[str, Any]


class SkillsBlock(BaseModel):
    pre: list[str] = []
    post: list[str] = []


class AutomationSkills(BaseModel):
    default: SkillsBlock = SkillsBlock()
    devices: dict[str, SkillsBlock] = {}


class ExecutionEnvConfig(BaseModel):
    mode: str = "fixed"
    profile: str = "test"


class CaseSuiteBody(BaseModel):
    id: str = ""
    name: str
    case_ids: list[str] = []
    updated_at: str = ""


class AutomationConfigUpdate(BaseModel):
    env_profile: Optional[str] = None
    execution_env: Optional[ExecutionEnvConfig] = None
    skills: Optional[AutomationSkills] = None
    suites: Optional[list[CaseSuiteBody]] = None
    qa_process: Optional[dict[str, Any]] = None


class QaProcessAssistBody(BaseModel):
    entity: str
    id: str
    job: str
    requirement: Optional[dict[str, Any]] = None
    release: Optional[dict[str, Any]] = None
    requirements: Optional[list[dict[str, Any]]] = None
    cases: Optional[list[dict[str, Any]]] = None
    tasks: Optional[list[dict[str, Any]]] = None
    suites: Optional[list[dict[str, Any]]] = None


class AtlasAliasUpdateBody(BaseModel):
    review_status: str = ""
    note: str = ""


def _app(app_id: str) -> dict[str, Any]:
    try:
        return ps.require_app(app_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="App not found") from exc


@router.get("/config/{app_id}")
def get_automation_config(app_id: str, _sess: dict = Depends(current_session)):
    return ok(aas.config_payload(_app(app_id)))


@router.put("/config/{app_id}", dependencies=[Depends(app_maintainer)])
def update_automation_config(app_id: str, body: AutomationConfigUpdate, _sess: dict = Depends(current_session)):
    app = _app(app_id)
    payload: dict[str, Any] = {}
    dumped = body.model_dump(exclude_none=True)
    payload.update(dumped)
    cfg = aas.save_automation_config(app, payload)
    return ok({"automation": cfg}, msg="自动化配置已保存")


@router.get("/playbook/{app_id}")
def get_app_playbook(app_id: str, _sess: dict = Depends(current_session)):
    app = _app(app_id)
    pkg = aas.package_for_app(app)
    pb = aas.get_playbook(app)
    return ok({"playbook": pb, "package": pkg})


@router.put("/playbook/{app_id}", dependencies=[Depends(app_maintainer)])
def save_app_playbook(app_id: str, body: PlaybookSaveBody, _sess: dict = Depends(current_session)):
    app = _app(app_id)
    pb = aas.save_playbook(app, body.playbook or {})
    return ok({"playbook": pb}, msg="已保存应用基础逻辑")


@router.get("/login-evidence-policy/{app_id}")
def get_login_evidence_policy(app_id: str, _sess: dict = Depends(current_session)):
    from mino_nexus.navigation.login.policy import normalize_policy
    policy = normalize_policy(aas.get_playbook(_app(app_id)).get("login_evidence_policy"))
    return ok({"policy": policy})


@router.put("/login-evidence-policy/{app_id}", dependencies=[Depends(app_maintainer)])
def save_login_evidence_policy(app_id: str, body: LoginEvidencePolicyBody, _sess: dict = Depends(current_session)):
    from mino_nexus.navigation.login.policy import normalize_policy
    app = _app(app_id)
    try:
        policy = normalize_policy(body.policy)
        expected = normalize_policy(body.expected_policy)
        current = normalize_policy(aas.get_playbook(app).get("login_evidence_policy"))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if expected != current:
        raise HTTPException(status_code=409, detail="登录校验配置已被修改，请重新读取后保存")
    aas.save_playbook(app, {"login_evidence_policy": policy})
    return ok({"policy": policy}, msg="应用登录校验配置已保存；新任务生效")


@router.get("/qa-process/summary")
def qa_process_summary(_sess: dict = Depends(current_session)):
    return ok({"items": aas.qa_process_summary()})


@router.get("/cases/{app_id}")
def list_cached_cases(app_id: str, _sess: dict = Depends(current_session)):
    return ok(aas.cases_payload(_app(app_id)))


@router.get("/runs/{app_id}")
def list_app_runs(app_id: str, _sess: dict = Depends(current_session)):
    _app(app_id)
    from mino_nexus.casework import run_store

    rows = run_store.list_runs(limit=30, app_id=app_id)
    return ok({"runs": [run_store.to_task_json(r, include_cases=False) for r in rows]})


@router.post("/qa-process/assist/{app_id}")
def qa_assist(app_id: str, body: QaProcessAssistBody, _sess: dict = Depends(current_session)):
    from mino_nexus.casework.qa import qa_process_assist as assist

    _app(app_id)
    if body.job not in assist.ASSIST_JOBS:
        raise HTTPException(status_code=400, detail="unknown assist job")
    if body.entity not in ("req", "rel"):
        raise HTTPException(status_code=400, detail="entity must be req or rel")
    art = assist.run_job(
        body.job,
        requirement=body.requirement,
        release=body.release,
        requirements=body.requirements or [],
        cases=body.cases or [],
        tasks=body.tasks or [],
        suites=body.suites or [],
    )
    return ok({"artifact": art})


@router.get("/qa-process/atlas-aliases/{app_id}")
def atlas_aliases(app_id: str, _sess: dict = Depends(current_session)):
    from mino_nexus.navigation.atlas import atlas_aliases as aliases

    _app(app_id)
    return ok({"items": aliases.list_aliases(app_id)})


@router.patch("/qa-process/atlas-aliases/{app_id}/{alias_id}", dependencies=[Depends(app_maintainer)])
def patch_alias(app_id: str, alias_id: str, body: AtlasAliasUpdateBody, _sess: dict = Depends(current_session)):
    from mino_nexus.navigation.atlas import atlas_aliases as aliases

    _app(app_id)
    row = aliases.patch(app_id, alias_id, body.model_dump(exclude_none=True))
    if not row:
        raise HTTPException(status_code=404, detail="别名不存在")
    return ok(row)


@router.delete("/qa-process/atlas-aliases/{app_id}/{alias_id}", dependencies=[Depends(app_maintainer)])
def delete_alias(app_id: str, alias_id: str, _sess: dict = Depends(current_session)):
    from mino_nexus.navigation.atlas import atlas_aliases as aliases

    _app(app_id)
    if not aliases.delete(app_id, alias_id):
        raise HTTPException(status_code=404, detail="别名不存在")
    return ok(msg="已删除")

