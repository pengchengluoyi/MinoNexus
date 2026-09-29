"""项目用例库与导入。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from mino_nexus.core.http_util import ok
from mino_nexus.routers.deps import current_session
from mino_nexus.services import case_import as cim
from mino_nexus.services import project_store as ps

router = APIRouter(prefix="/project", tags=["Project Cases"])


class ResourceKeyPreviewBody(BaseModel):
    precondition: str = ""
    platform: str = ""
    package_id: str = ""
    env: str = "test"


class CaseImportPreviewBody(BaseModel):
    requirement_id: str
    table: list[list[Any]] | None = None
    text: str = ""
    filename: str = ""
    header_row: int = 0
    skip_rows: list[int] = Field(default_factory=list)
    column_map: dict[str, int] = Field(default_factory=dict)
    default_platform: str = ""


class CaseImportCommitRow(BaseModel):
    row_index: int | None = None
    selected: bool = True
    on_conflict: str = "skip"
    payload: dict[str, Any] | None = None


class CaseImportCommitBody(BaseModel):
    requirement_id: str
    preview_token: str = ""
    default_on_conflict: str = "skip"
    default_platform: str = ""
    block_on_step_key_issues: bool | None = None
    rows: list[CaseImportCommitRow] = Field(default_factory=list)


class CaseStepKeysValidateBody(BaseModel):
    precondition: str = ""
    steps_raw: str = ""
    expected_raw: str = ""
    steps: list[str] = Field(default_factory=list)
    expected: list[str] = Field(default_factory=list)
    app_id: str = ""


class CaseDeleteBody(BaseModel):
    case_ids: list[str] = Field(default_factory=list)


class CaseUpdateBody(BaseModel):
    requirement_id: str = ""
    name: str = ""
    module: str = ""
    platform: str = ""
    aspect: str = ""
    precondition: str = ""
    steps: list[str] = Field(default_factory=list)
    expected: list[str] = Field(default_factory=list)
    steps_raw: str = ""
    expected_raw: str = ""
    resource_key: dict[str, Any] | None = None
    case_scene: dict[str, Any] | None = None


def _project(project_id: str) -> dict[str, Any]:
    row = ps.find_project(project_id)
    if not row:
        raise HTTPException(status_code=404, detail="Project not found")
    return row


@router.post("/{project_id}/resource-key/preview")
def preview_resource_key(project_id: str, body: ResourceKeyPreviewBody, _sess: dict = Depends(current_session)):
    _project(project_id)
    from mino_nexus.services import project_store as ps
    from mino_nexus.services.case_resource_claim import (
        compile_resource_key_from_precondition,
        default_package_for_project,
    )
    from mino_nexus.services.resource_claim_summary import resource_claim_summary

    try:
        env_doc = ps.project_env(project_id)
    except KeyError:
        env_doc = None
    pkg = str(body.package_id or "").strip() or default_package_for_project(project_id)
    claim = compile_resource_key_from_precondition(
        body.precondition,
        env_doc=env_doc,
        platform=str(body.platform or "").strip(),
        package=pkg,
        env=str(body.env or "test"),
    )
    scene = claim.get("case_scene") if isinstance(claim.get("case_scene"), dict) else {}
    return ok({
        "resource_key": claim,
        "case_scene": scene,
        "summary": resource_claim_summary(claim, scene=scene, precondition=body.precondition),
    })


@router.get("/{project_id}/cases")
def list_project_cases(project_id: str, _sess: dict = Depends(current_session)):
    _project(project_id)
    return ok(cim.cases_payload(project_id))


@router.get("/{project_id}/cases/{case_id}")
def get_project_case(project_id: str, case_id: str, _sess: dict = Depends(current_session)):
    from mino_nexus.services.case_key_registry import step_keys_summary_for_case
    from mino_nexus.services.case_store import get_case

    _project(project_id)
    cid = str(case_id or "").strip()
    row = get_case(project_id, cid)
    if not row:
        raise HTTPException(status_code=404, detail="用例不存在")
    return ok(
        {
            "case": row,
            "step_keys_summary": step_keys_summary_for_case(row),
        }
    )


@router.get("/{project_id}/requirements")
def list_import_requirements(project_id: str, _sess: dict = Depends(current_session)):
    _project(project_id)
    return ok({"requirements": cim.list_project_requirements(project_id)})


@router.post("/{project_id}/cases/import/preview")
def import_preview(project_id: str, body: CaseImportPreviewBody, _sess: dict = Depends(current_session)):
    _project(project_id)
    try:
        data = cim.preview_import(
            project_id=project_id,
            requirement_id=body.requirement_id,
            table=body.table,
            text=body.text,
            filename=body.filename,
            header_row=body.header_row,
            skip_rows=body.skip_rows,
            column_map=body.column_map,
            default_platform=body.default_platform,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ok(data)


@router.put("/{project_id}/cases/{case_id}")
def update_one_case(
    project_id: str,
    case_id: str,
    body: CaseUpdateBody,
    _sess: dict = Depends(current_session),
):
    from mino_nexus.services.case_store import get_case, save_case

    _project(project_id)
    cid = str(case_id or "").strip()
    existing = get_case(project_id, cid)
    if not existing:
        raise HTTPException(status_code=404, detail="用例不存在")
    patch = body.model_dump(exclude_unset=True)
    merged = {**existing, **patch, "case_id": cid}
    rid = str(patch.get("requirement_id") or existing.get("requirement_id") or "").strip()
    saved = save_case(project_id, merged, requirement_id=rid)
    return ok({"case": saved}, msg="用例已保存")


@router.post("/{project_id}/cases/step-keys/validate")
def validate_case_step_keys_body(
    project_id: str,
    body: CaseStepKeysValidateBody,
    _sess: dict = Depends(current_session),
):
    from mino_nexus.services.case_key_registry import validate_case_step_keys

    _project(project_id)
    case_like = {
        "precondition": body.precondition,
        "steps_raw": body.steps_raw,
        "expected_raw": body.expected_raw,
        "steps": body.steps,
        "expected": body.expected,
    }
    return ok(validate_case_step_keys(case_like, app_id=str(body.app_id or "").strip()))


@router.post("/{project_id}/cases/{case_id}/step-keys/compile")
def compile_case_step_keys(project_id: str, case_id: str, _sess: dict = Depends(current_session)):
    from mino_nexus.services.case_step_key_compiler import sync_case_step_program_keys
    from mino_nexus.services.case_store import get_case, save_case

    _project(project_id)
    cid = str(case_id or "").strip()
    existing = get_case(project_id, cid)
    if not existing:
        raise HTTPException(status_code=404, detail="用例不存在")
    synced = sync_case_step_program_keys(dict(existing))
    rid = str(existing.get("requirement_id") or "").strip()
    saved = save_case(project_id, synced, requirement_id=rid)
    warnings = (saved.get("meta") or {}).get("key_compile_warnings") or []
    return ok(
        {"case": saved, "warning_count": len(warnings)},
        msg="已编译操作/预期 step_program_keys",
    )


@router.post("/{project_id}/cases/{case_id}/resource-key/compile")
def compile_case_resource_key(project_id: str, case_id: str, _sess: dict = Depends(current_session)):
    from mino_nexus.services.case_resource_claim import sync_case_resource_metadata
    from mino_nexus.services.case_store import get_case, save_case

    _project(project_id)
    cid = str(case_id or "").strip()
    existing = get_case(project_id, cid)
    if not existing:
        raise HTTPException(status_code=404, detail="用例不存在")
    synced = sync_case_resource_metadata(dict(existing), project_id=project_id, force_recompile=True)
    rid = str(existing.get("requirement_id") or "").strip()
    saved = save_case(project_id, synced, requirement_id=rid)
    return ok({"case": saved}, msg="已从前置编译 resource_key")


@router.delete("/{project_id}/cases/{case_id}")
def delete_one_case(project_id: str, case_id: str, _sess: dict = Depends(current_session)):
    from mino_nexus.services.case_store import delete_case

    _project(project_id)
    if not delete_case(project_id, case_id):
        raise HTTPException(status_code=404, detail="用例不存在")
    return ok({"deleted": 1, "case_id": case_id}, msg="已删除")


@router.post("/{project_id}/cases/delete")
def delete_many_cases(project_id: str, body: CaseDeleteBody, _sess: dict = Depends(current_session)):
    from mino_nexus.services.case_store import delete_cases

    _project(project_id)
    ids = [str(x).strip() for x in (body.case_ids or []) if str(x).strip()]
    if not ids:
        raise HTTPException(status_code=400, detail="请指定要删除的用例编号")
    removed = delete_cases(project_id, ids)
    return ok({"deleted": removed, "case_ids": ids[:50]}, msg=f"已删除 {removed} 条")


@router.post("/{project_id}/cases/import/commit")
def import_commit(project_id: str, body: CaseImportCommitBody, _sess: dict = Depends(current_session)):
    _project(project_id)
    try:
        rows = [r.model_dump() for r in body.rows] if body.rows else None
        default_action = "overwrite" if body.default_on_conflict == "overwrite" else "skip"
        from mino_nexus.services.settings_store import resolve_block_on_step_key_issues

        data = cim.commit_import(
            project_id=project_id,
            requirement_id=body.requirement_id,
            preview_token=body.preview_token,
            rows=rows,
            default_on_conflict=default_action,
            default_platform=str(body.default_platform or "").strip(),
            block_on_step_key_issues=resolve_block_on_step_key_issues(body.block_on_step_key_issues),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ok(data, msg="用例已导入")
