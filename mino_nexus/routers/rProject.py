"""项目 / 应用 / 号池。契约对齐上游 `rProject`，存储走 JSON。"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from mino_nexus.core.http_util import ok
from mino_nexus.services.project_env import (
    ENV_PROFILE_LABELS,
    list_test_accounts,
    normalize_project_env,
    pick_test_accounts,
    public_test_accounts,
    save_test_accounts,
)
from mino_nexus.services import project_store as ps
from mino_nexus.routers.deps import current_session

router = APIRouter(prefix="/project", tags=["Project"])


class ProjectCreate(BaseModel):
    name: str
    description: Optional[str] = None


class AppCreate(BaseModel):
    project_id: str
    name: str
    description: Optional[str] = None
    platforms: str = ""
    env: dict[str, Any] = {}


class AppEnvUpdate(BaseModel):
    env: dict[str, Any] = {}


class ProjectEnvUpdate(BaseModel):
    default_profile: Optional[str] = "test"
    profiles: dict[str, Any] = {}
    environments: Optional[list] = None
    channels: Optional[list] = None
    pipeline: Optional[list] = None


class TestAccountSaveBody(BaseModel):
    accounts: list[dict[str, Any]] = []


class TestAccountPickBody(BaseModel):
    prompt: str = ""
    env: str = ""
    surface: str = ""


def _missing(exc: KeyError) -> None:
    raise HTTPException(status_code=404, detail=str(exc) or "not found") from exc


@router.post("/create")
def create_project(item: ProjectCreate, sess: dict = Depends(current_session)):
    return ps.create_project(item.name, item.description or "", owner=sess)


@router.post("/app/create")
def create_app(item: AppCreate, sess: dict = Depends(current_session)):
    try:
        return ps.create_app(
            item.project_id,
            name=item.name,
            description=item.description or "",
            platforms=item.platforms,
            env=item.env or {},
            owner=sess,
        )
    except KeyError as exc:
        _missing(exc)


@router.get("/list")
def list_projects(_sess: dict = Depends(current_session)):
    return ps.list_projects()


@router.delete("/app/{app_id}")
def delete_app(app_id: str, _sess: dict = Depends(current_session)):
    try:
        data = ps.delete_app(app_id)
    except KeyError as exc:
        _missing(exc)
    return ok(data, msg="deleted")


@router.get("/app/{app_id}")
def get_app(app_id: str, _sess: dict = Depends(current_session)):
    try:
        return ok(ps.get_app_detail(app_id))
    except KeyError as exc:
        _missing(exc)


@router.put("/app/{app_id}/env")
def update_app_env(app_id: str, item: AppEnvUpdate, _sess: dict = Depends(current_session)):
    try:
        doc = ps.update_app_env(app_id, item.env or {})
    except KeyError as exc:
        _missing(exc)
    return ok(doc, msg="已保存到项目环境(测试)")


@router.get("/{project_id}/env")
def get_project_env(project_id: str, _sess: dict = Depends(current_session)):
    try:
        project = ps.require_project(project_id)
        doc = ps.project_env(project_id)
    except KeyError as exc:
        _missing(exc)
    safe = dict(doc)
    safe["test_accounts"] = public_test_accounts(list_test_accounts(doc))
    return ok({
        "project_id": project["id"],
        "project_name": project.get("name"),
        "env": safe,
        "profile_labels": ENV_PROFILE_LABELS,
    })


@router.put("/{project_id}/env")
def update_project_env(project_id: str, item: ProjectEnvUpdate, _sess: dict = Depends(current_session)):
    try:
        project = ps.require_project(project_id)
    except KeyError as exc:
        _missing(exc)
    prev = project.get("env") if isinstance(project.get("env"), dict) else {}
    incoming_envs = item.environments if item.environments is not None else prev.get("environments")
    if isinstance(incoming_envs, list) and isinstance(prev.get("environments"), list):
        prev_by_key = {
            str(e.get("key") or ""): e
            for e in prev.get("environments") or []
            if isinstance(e, dict) and e.get("key")
        }
        merged_envs = []
        for row in incoming_envs:
            if not isinstance(row, dict):
                continue
            old = prev_by_key.get(str(row.get("key") or "")) or {}
            if "secrets" not in row and old.get("secrets"):
                row = {**row, "secrets": old.get("secrets")}
            merged_envs.append(row)
        incoming_envs = merged_envs
    doc = normalize_project_env(
        {
            "default_profile": item.default_profile,
            "profiles": item.profiles,
            "environments": incoming_envs,
            "channels": item.channels,
            "pipeline": item.pipeline,
            "test_accounts": prev.get("test_accounts"),
        }
    )
    if not doc.get("environments"):
        raise HTTPException(status_code=400, detail="至少保留一个环境")
    if doc["default_profile"] not in {e["key"] for e in doc["environments"]}:
        raise HTTPException(status_code=400, detail="默认环境不在环境列表里")
    saved = ps.save_project_env(project_id, doc)
    safe = dict(saved)
    safe["test_accounts"] = public_test_accounts(list_test_accounts(safe))
    return ok({"env": safe}, msg="Project env updated")


@router.get("/{project_id}/accounts")
def list_project_accounts(project_id: str, env: str = "", _sess: dict = Depends(current_session)):
    try:
        doc = ps.project_env(project_id)
    except KeyError as exc:
        _missing(exc)
    rows = list_test_accounts(doc)
    if env:
        rows = [x for x in rows if str(x.get("env") or "") == env]
    return ok({
        "accounts": public_test_accounts(rows, include_password=True),
        "environments": doc.get("environments") or [],
        "channels": doc.get("channels") or [],
    })


@router.put("/{project_id}/accounts")
def save_project_accounts(project_id: str, body: TestAccountSaveBody, _sess: dict = Depends(current_session)):
    try:
        doc = save_test_accounts(ps.project_env(project_id), body.accounts)
        ps.save_project_env(project_id, doc)
    except KeyError as exc:
        _missing(exc)
    return ok(
        {"accounts": public_test_accounts(list_test_accounts(doc), include_password=True)},
        msg="已保存",
    )


@router.post("/{project_id}/accounts/pick")
def pick_project_accounts(project_id: str, body: TestAccountPickBody, _sess: dict = Depends(current_session)):
    try:
        doc = ps.project_env(project_id)
    except KeyError as exc:
        _missing(exc)
    ranked = pick_test_accounts(
        list_test_accounts(doc),
        prompt=body.prompt,
        env=body.env,
        surface=body.surface,
        channels=doc.get("channels") or [],
        env_doc=doc,
    )
    return ok({"accounts": public_test_accounts(ranked)})


@router.delete("/{project_id}")
def delete_project(project_id: str, _sess: dict = Depends(current_session)):
    try:
        data = ps.delete_project(project_id)
    except KeyError as exc:
        _missing(exc)
    return ok(data, msg="deleted")
