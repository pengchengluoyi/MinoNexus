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


class TestAccountPatchBody(BaseModel):
    env: Optional[str] = None
    display_name: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    otp: Optional[str] = None
    note: Optional[str] = None
    locked: Optional[bool] = None
    facets: Optional[dict[str, Any]] = None


class TestAccountPickBody(BaseModel):
    prompt: str = ""
    env: str = ""
    surface: str = ""
    requirements: dict[str, Any] = {}


class FacetExtensionsBody(BaseModel):
    extensions: list[dict[str, Any]] = []


class FacetAiExpandBody(BaseModel):
    hint: str = ""
    apply: bool = True


class AccountTemplateIdsBody(BaseModel):
    template_ids: list[str] = []


class ProjectAccountPoolLocalBody(BaseModel):
    templates: list[dict[str, Any]] = []
    extension_addons: dict[str, list[dict[str, Any]]] = {}


class AccountAllocateBody(BaseModel):
    template_id: str
    env: str = ""
    run_id: str = ""
    app_id: str = ""
    requirements: dict[str, Any] = {}
    lease: bool = False


def _facet_extensions(doc: dict) -> list:
    from mino_nexus.services.account_facet_schema import extensions_from_env

    return extensions_from_env(doc)


def _public_accounts(rows: list, doc: dict, *, include_password: bool = False) -> list:
    from mino_nexus.services.account_pool_templates import merged_pool_field_defs

    pool_defs = merged_pool_field_defs(doc)
    return public_test_accounts(
        rows,
        include_password=include_password,
        pool_field_defs=pool_defs,
    )


def _missing(exc: KeyError) -> None:
    raise HTTPException(status_code=404, detail=str(exc) or "not found") from exc


def _resolve_project_id(project_or_app_id: str) -> str:
    """Console 有时把 app_id 填进 project 段；按 project 优先，否则反查所属项目。"""
    raw = str(project_or_app_id or "").strip()
    if not raw:
        raise HTTPException(status_code=404, detail="project_id required")
    try:
        ps.require_project(raw)
        return raw
    except KeyError:
        pass
    try:
        app = ps.require_app(raw)
    except KeyError as exc:
        _missing(exc)
    pid = str(app.get("project_id") or "").strip()
    if not pid:
        raise HTTPException(status_code=404, detail="App has no project_id")
    return pid


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
    safe.pop("test_accounts", None)
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
        }
    )
    if not doc.get("environments"):
        raise HTTPException(status_code=400, detail="至少保留一个环境")
    if doc["default_profile"] not in {e["key"] for e in doc["environments"]}:
        raise HTTPException(status_code=400, detail="默认环境不在环境列表里")
    saved = ps.save_project_env(project_id, doc)
    safe = dict(saved)
    safe.pop("test_accounts", None)
    return ok({"env": safe}, msg="Project env updated")


@router.get("/{project_id}/account-pool-schema")
def get_project_account_pool_schema(project_id: str, _sess: dict = Depends(current_session)):
    project_id = _resolve_project_id(project_id)
    try:
        doc = ps.project_env(project_id)
    except KeyError as exc:
        _missing(exc)
    from mino_nexus.services.account_pool_templates import merged_pool_field_defs

    return ok({"pool_field_defs": merged_pool_field_defs(doc)})


@router.get("/app/{app_id}/accounts")
def list_app_accounts(app_id: str, env: str = "", _sess: dict = Depends(current_session)):
    try:
        app = ps.require_app(app_id)
    except KeyError as exc:
        _missing(exc)
    pid = str(app.get("project_id") or "").strip()
    if not pid:
        raise HTTPException(status_code=404, detail="App has no project_id")
    return list_project_accounts(pid, env=env, _sess=_sess)


@router.get("/{project_id}/accounts")
def list_project_accounts(project_id: str, env: str = "", _sess: dict = Depends(current_session)):
    project_id = _resolve_project_id(project_id)
    try:
        doc = ps.project_env(project_id)
    except KeyError as exc:
        _missing(exc)
    rows = list_test_accounts(doc, project_id=project_id)
    if env:
        rows = [x for x in rows if str(x.get("env") or "") == env]
    return ok({
        "accounts": _public_accounts(rows, doc, include_password=True),
        "environments": doc.get("environments") or [],
        "channels": doc.get("channels") or [],
    })


@router.put("/{project_id}/accounts")
def save_project_accounts(project_id: str, body: TestAccountSaveBody, _sess: dict = Depends(current_session)):
    project_id = _resolve_project_id(project_id)
    from mino_nexus.services.pool_account_store import env_has_test_accounts_blob, persist_env_strip_test_accounts_if_needed

    try:
        base = ps.project_env(project_id)
        doc = save_test_accounts(base, body.accounts, project_id=project_id)
        if env_has_test_accounts_blob(base):
            ps.save_project_env(project_id, doc)
        else:
            persist_env_strip_test_accounts_if_needed(project_id, base)
    except KeyError as exc:
        _missing(exc)
    return ok(
        {
            "accounts": _public_accounts(
                list_test_accounts(doc, project_id=project_id), doc, include_password=True
            ),
        },
        msg="已保存",
    )


@router.patch("/{project_id}/accounts/{account_id}")
def patch_project_account(
    project_id: str,
    account_id: str,
    body: TestAccountPatchBody,
    _sess: dict = Depends(current_session),
):
    from mino_nexus.services.project_env import save_one_test_account

    from mino_nexus.services.pool_account_store import persist_env_strip_test_accounts_if_needed

    try:
        doc = ps.project_env(project_id)
    except KeyError as exc:
        _missing(exc)
    row = body.model_dump(exclude_unset=True)
    try:
        saved = save_one_test_account(doc, row, project_id=project_id, account_id=account_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    persist_env_strip_test_accounts_if_needed(project_id, doc)
    public = _public_accounts([saved], doc, include_password=True)[0]
    return ok({"account": public}, msg="已保存")


@router.post("/{project_id}/accounts")
def create_project_account(
    project_id: str,
    body: TestAccountPatchBody,
    _sess: dict = Depends(current_session),
):
    from mino_nexus.services.account_pool_templates import new_account_id
    from mino_nexus.services.project_env import save_one_test_account

    from mino_nexus.services.pool_account_store import persist_env_strip_test_accounts_if_needed

    try:
        doc = ps.project_env(project_id)
    except KeyError as exc:
        _missing(exc)
    aid = new_account_id()
    row = body.model_dump(exclude_unset=True)
    try:
        saved = save_one_test_account(doc, row, project_id=project_id, account_id=aid)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    persist_env_strip_test_accounts_if_needed(project_id, doc)
    public = _public_accounts([saved], doc, include_password=True)[0]
    return ok({"account": public}, msg="已保存")


@router.delete("/{project_id}/accounts/{account_id}")
def delete_project_account(
    project_id: str,
    account_id: str,
    _sess: dict = Depends(current_session),
):
    from mino_nexus.services.pool_account_store import delete_one

    try:
        ps.require_project(project_id)
    except KeyError as exc:
        _missing(exc)
    if not delete_one(project_id, account_id):
        raise HTTPException(status_code=404, detail="account not found")
    return ok(msg="已删除")


@router.get("/{project_id}/account-pool-templates")
def list_project_account_pool_templates(project_id: str, _sess: dict = Depends(current_session)):
    from mino_nexus.services.project_account_pool import project_pool_payload

    try:
        doc = ps.project_env(project_id)
    except KeyError as exc:
        _missing(exc)
    return ok(project_pool_payload(doc))


@router.get("/{project_id}/account-pool-local")
def get_project_account_pool_local(project_id: str, _sess: dict = Depends(current_session)):
    from mino_nexus.services.project_account_pool import project_pool_payload

    try:
        doc = ps.project_env(project_id)
    except KeyError as exc:
        _missing(exc)
    return ok(project_pool_payload(doc))


@router.put("/{project_id}/account-pool-local")
def put_project_account_pool_local(
    project_id: str,
    body: ProjectAccountPoolLocalBody,
    _sess: dict = Depends(current_session),
):
    from mino_nexus.services.project_account_pool import project_pool_payload, save_project_pool_local

    try:
        doc = ps.project_env(project_id)
    except KeyError as exc:
        _missing(exc)
    merged = save_project_pool_local(
        doc,
        {"templates": body.templates, "extension_addons": body.extension_addons},
    )
    saved = ps.save_project_env(project_id, merged)
    return ok(project_pool_payload(saved), msg="已保存项目模板与字段")


@router.put("/{project_id}/account-template-ids")
def put_project_account_template_ids(
    project_id: str,
    body: AccountTemplateIdsBody,
    _sess: dict = Depends(current_session),
):
    from mino_nexus.services.account_facet_schema import normalize_extensions
    from mino_nexus.services.project_env import _norm_template_ids

    try:
        doc = ps.project_env(project_id)
    except KeyError as exc:
        _missing(exc)
    doc = dict(doc)
    doc["account_template_ids"] = _norm_template_ids(body.template_ids)
    doc["account_facet_extensions"] = normalize_extensions(doc.get("account_facet_extensions"))
    saved = ps.save_project_env(project_id, doc)
    return ok({"enabled_template_ids": saved.get("account_template_ids") or []}, msg="已保存项目启用模板")


@router.post("/{project_id}/accounts/allocate")
def allocate_project_account(project_id: str, body: AccountAllocateBody, _sess: dict = Depends(current_session)):
    from types import SimpleNamespace

    from mino_nexus.services.account_allocate import allocate_by_template

    try:
        doc = ps.project_env(project_id)
    except KeyError as exc:
        _missing(exc)
    ctx = None
    if body.lease:
        if not str(body.app_id or "").strip():
            raise HTTPException(status_code=400, detail="lease 为 true 时需 app_id")
        ctx = SimpleNamespace(
            run_id=str(body.run_id or "").strip(),
            app_id=str(body.app_id or "").strip(),
            env_profile=str(body.env or "test").strip() or "test",
            platform="android",
            target_package="",
        )
    grant, err = allocate_by_template(
        ctx,
        project_id=project_id,
        env_doc=doc,
        template_id=body.template_id,
        env_profile=str(body.env or "test").strip() or "test",
        run_id=str(body.run_id or "").strip(),
        requirements=body.requirements or None,
    )
    if not grant:
        raise HTTPException(status_code=404, detail=err or "分配失败")
    return ok({"grant": grant}, msg="已分配账号凭证")


@router.post("/{project_id}/accounts/pick")
def pick_project_accounts(project_id: str, body: TestAccountPickBody, _sess: dict = Depends(current_session)):
    try:
        doc = ps.project_env(project_id)
    except KeyError as exc:
        _missing(exc)
    from mino_nexus.services.project_env import pick_test_accounts, resolve_pick_requirements

    rows = list_test_accounts(doc, project_id=project_id)
    req = resolve_pick_requirements(
        prompt=body.prompt,
        env=body.env,
        env_doc=doc,
        requirements=body.requirements or None,
    )
    ranked = pick_test_accounts(
        rows,
        prompt=body.prompt,
        env=body.env,
        surface=body.surface,
        channels=doc.get("channels") or [],
        env_doc=doc,
        requirements=req,
    )
    return ok({
        "accounts": _public_accounts(ranked, doc, include_password=False),
        "requirements": req,
    })


@router.get("/{project_id}/resource-profiles")
def list_resource_profiles(project_id: str, _sess: dict = Depends(current_session)):
    try:
        ps.require_project(project_id)
    except KeyError as exc:
        _missing(exc)
    from mino_nexus.services.resource_pool import list_pool_profiles

    return ok({"profiles": list_pool_profiles()})


@router.get("/{project_id}/account-facet-extensions")
def get_account_facet_extensions(project_id: str, _sess: dict = Depends(current_session)):
    try:
        doc = ps.project_env(project_id)
    except KeyError as exc:
        _missing(exc)
    return ok({"extensions": _facet_extensions(doc)})


@router.put("/{project_id}/account-facet-extensions")
def put_account_facet_extensions(project_id: str, body: FacetExtensionsBody, _sess: dict = Depends(current_session)):
    from mino_nexus.services.account_facet_schema import normalize_extensions, save_extensions_to_env

    try:
        doc = ps.project_env(project_id)
    except KeyError as exc:
        _missing(exc)
    merged = save_extensions_to_env(doc, normalize_extensions(body.extensions))
    saved = ps.save_project_env(project_id, merged)
    return ok({"extensions": _facet_extensions(saved)}, msg="已保存扩展字段")


@router.post("/{project_id}/account-facet-extensions/ai-expand")
def ai_expand_account_facet_extensions(
    project_id: str,
    body: FacetAiExpandBody,
    _sess: dict = Depends(current_session),
):
    from mino_nexus.services.account_facet_schema import ai_expand_facet_extensions, save_extensions_to_env

    try:
        ps.require_project(project_id)
        doc = ps.project_env(project_id)
    except KeyError as exc:
        _missing(exc)
    existing = _facet_extensions(doc)
    merged, err = ai_expand_facet_extensions(
        project_id,
        hint=body.hint,
        existing=existing,
    )
    if err and not merged:
        raise HTTPException(status_code=400, detail=err)
    if body.apply and merged:
        saved = ps.save_project_env(project_id, save_extensions_to_env(doc, merged))
        return ok({
            "extensions": _facet_extensions(saved),
            "message": err or "已合并 AI 建议字段",
        })
    return ok({"extensions": merged, "message": err or "仅预览，未写入"})


@router.delete("/{project_id}")
def delete_project(project_id: str, _sess: dict = Depends(current_session)):
    try:
        data = ps.delete_project(project_id)
    except KeyError as exc:
        _missing(exc)
    return ok(data, msg="deleted")
