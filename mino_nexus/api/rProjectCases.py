"""项目用例库与导入。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from mino_nexus.core.http_util import ok
from mino_nexus.api.deps import current_session, project_maintainer
from mino_nexus.casework import case_import as cim
from mino_nexus.resources import project_store as ps

router = APIRouter(prefix="/project", tags=["Project Cases"])


class ResourceKeyPreviewBody(BaseModel):
    precondition: str = ""
    platform: str = ""
    package_id: str = ""
    env: str = "test"


class ImportRequirementCreateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    app_id: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=200)


class CaseImportPreviewBody(BaseModel):
    requirement_id: str
    table: list[list[Any]] | None = None
    text: str = ""
    filename: str = ""
    header_row: int = 0
    skip_rows: list[int] = Field(default_factory=list)
    column_map: dict[str, int] = Field(default_factory=dict)
    default_platform: str = ""


class CaseImportSourceBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str
    configuration: dict[str, Any]
    operation: str = "read"


class CaseImportCommitRow(BaseModel):
    row_index: int | None = None
    selected: bool = True
    on_conflict: str = "skip"
    payload: dict[str, Any] | None = None


class CaseImportCommitBody(BaseModel):
    requirement_id: str
    client_request_id: str = Field(default="", max_length=160)
    preview_token: str = ""
    default_on_conflict: str = "skip"
    default_platform: str = ""
    block_on_step_key_issues: bool | None = None
    import_policy: str = ""
    rows: list[CaseImportCommitRow] = Field(default_factory=list)


class CaseStepKeysValidateBody(BaseModel):
    precondition: str = ""
    steps_raw: str = ""
    expected_raw: str = ""
    steps: list[str] = Field(default_factory=list)
    expected: list[str] = Field(default_factory=list)
    app_id: str = ""


class CasePreflightBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    app_id: str = Field(min_length=1, max_length=200)
    case_ids: list[str] = Field(default_factory=list, max_length=200)
    recompile_stale: bool = False


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


class V5PublishBody(BaseModel):
    expected_revision: int = Field(ge=1)
    expected_compile_job_id: str = Field(default="", max_length=160)
    reviewed_expected_strength: bool = False
    retry_policy: dict[str, Any] | None = None
    reversible: bool = False


class V5ControlBody(BaseModel):
    expected_revision: int = Field(ge=1)
    locator_spec: dict[str, Any]
    sensitive: bool = False
    publish: bool = False


class V5ControlProbeBody(BaseModel):
    expected_revision: int = Field(ge=1)
    sn: str = Field(min_length=1)
    url: str = Field(min_length=1)


class CaseReviewSource(BaseModel):
    precondition: str = Field(max_length=30000)
    steps_raw: str = Field(max_length=60000)
    expected_raw: str = Field(max_length=60000)


class CaseReviewBody(BaseModel):
    expected_revision: int = Field(ge=1)
    source: CaseReviewSource
    reason: str = Field(min_length=1, max_length=1000)



class CompileJobBody(BaseModel):
    expected_revision: int = Field(ge=1)
    client_request_id: str = Field(min_length=1, max_length=160)


class CompileJobCancelBody(BaseModel):
    expected_revision: int = Field(ge=1)


class CaseReviewItemBody(BaseModel):
    expected_revision: int = Field(ge=1)
    expected_job_id: str = ""
    reason: str = Field(min_length=1, max_length=1000)
    category: str | None = None


class CaseReviewContext(BaseModel):
    app_id: str = Field(min_length=1)
    sn: str = Field(min_length=1)
    env_profile: str = ""
    env_surface: str = ""
    action_scheme: str = "dom_preferred"
    provider_id: str = ""


class CaseReviewRecheckBody(CaseReviewItemBody):
    context: CaseReviewContext | None = None
    evidence_session_id: str = ""


def _case_error(exc):
    code = str(exc)
    return HTTPException(status_code=404 if code.endswith("missing") else 409 if "conflict" in code else 400,
                         detail={"reason_code": code})


@router.get("/{project_id}/cases/{case_id}/version-chain")
def case_version_chain(project_id: str, case_id: str, _sess: dict = Depends(current_session)):
    from mino_nexus.casework.case_compilation import history
    try:
        return ok(history(project_id, case_id))
    except ValueError as exc:
        raise _case_error(exc) from exc


@router.get("/{project_id}/cases/{case_id}/compile-jobs/{job_id}")
def case_compile_job(project_id: str, case_id: str, job_id: str, _sess: dict = Depends(current_session)):
    from mino_nexus.casework.case_compilation import get_job
    try:
        return ok(get_job(project_id, case_id, job_id))
    except ValueError as exc:
        raise _case_error(exc) from exc


@router.post("/{project_id}/cases/{case_id}/compile-jobs", dependencies=[Depends(project_maintainer)])
def queue_case_compile(project_id: str, case_id: str, body: CompileJobBody, _sess: dict = Depends(current_session)):
    from mino_nexus.casework.case_compilation import enqueue
    try:
        return ok(enqueue(project_id, case_id, body.expected_revision, body.client_request_id))
    except ValueError as exc:
        raise _case_error(exc) from exc


@router.post("/{project_id}/cases/{case_id}/compile-jobs/{job_id}/cancel", dependencies=[Depends(project_maintainer)])
def cancel_case_compile(project_id: str, case_id: str, job_id: str, body: CompileJobCancelBody, _sess: dict = Depends(current_session)):
    from mino_nexus.casework.case_compilation import cancel
    try:
        return ok(cancel(project_id, case_id, job_id, body.expected_revision))
    except ValueError as exc:
        raise _case_error(exc) from exc


@router.post("/{project_id}/cases/{case_id}/compile-jobs/{job_id}/recover", dependencies=[Depends(project_maintainer)])
def recover_case_compile(project_id: str, case_id: str, job_id: str, body: CompileJobBody, _sess: dict = Depends(current_session)):
    from mino_nexus.casework.case_compilation import enqueue
    try:
        return ok(enqueue(project_id, case_id, body.expected_revision, body.client_request_id, parent_job_id=job_id))
    except ValueError as exc:
        raise _case_error(exc) from exc


@router.post("/{project_id}/cases/{case_id}/review-requirements/recheck", dependencies=[Depends(project_maintainer)])
def recheck_case_review(project_id: str, case_id: str, body: CaseReviewRecheckBody, _sess: dict = Depends(current_session)):
    from mino_nexus.casework.case_review import recheck
    try:
        return ok(recheck(project_id, case_id, body.expected_revision, body.expected_job_id, body.reason,
            context=body.context.model_dump() if body.context else None, evidence_session_id=body.evidence_session_id))
    except ValueError as exc:
        raise _case_error(exc) from exc


@router.post("/{project_id}/cases/{case_id}/review-requirements/{item_id}/review", dependencies=[Depends(project_maintainer)])
def review_case_item(project_id: str, case_id: str, item_id: str, body: CaseReviewItemBody, _sess: dict = Depends(current_session)):
    from mino_nexus.casework.case_review import review
    try:
        return ok(review(project_id, case_id, body.expected_revision, body.expected_job_id, item_id, body.reason, category=body.category))
    except ValueError as exc:
        raise _case_error(exc) from exc

class V5BaselineBody(BaseModel):
    expected_revision: int = Field(ge=1)
    control_id: str = Field(min_length=1)


def _project(project_id: str) -> dict[str, Any]:
    row = ps.find_project(project_id)
    if not row:
        raise HTTPException(status_code=404, detail="Project not found")
    return row


@router.get("/{project_id}/cases/{case_id}/event-migration-preview")
def event_migration_preview(project_id: str, case_id: str, _sess: dict = Depends(current_session)):
    _project(project_id)
    from mino_nexus.casework.case_store import get_case
    from mino_nexus.casework.event_migration import preview
    from mino_nexus.core.secret_redaction import sanitize
    case = get_case(project_id, case_id)
    if not case:
        raise HTTPException(status_code=404, detail="用例不存在")
    return ok(sanitize(preview(case)))


class EventPolicyReviewBody(BaseModel):
    kind: str
    entry_id: str
    source_digest: str = Field(min_length=64, max_length=64)
    request_id: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=1000)
    action: str
    replacement: dict[str, Any] = Field(default_factory=dict)


@router.get("/{project_id}/event-policies/migration-preview")
def event_policy_preview(project_id: str, sess: dict = Depends(current_session)):
    from mino_nexus.api.deps import require_packs_writer
    from mino_nexus.casework.event_policy_review import inventory
    from mino_nexus.core.secret_redaction import sanitize
    require_packs_writer(sess)
    _project(project_id)
    return ok(sanitize({'items': inventory()}))


@router.post("/{project_id}/event-policies/migration-apply")
def event_policy_apply(project_id: str, body: EventPolicyReviewBody, sess: dict = Depends(current_session)):
    from mino_nexus.api.deps import require_packs_writer
    from mino_nexus.casework.event_policy_review import apply
    from mino_nexus.platform.identity import require_user_id
    require_packs_writer(sess)
    _project(project_id)
    try:
        values = body.model_dump()
        return ok(apply(**values, actor=require_user_id()))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail={"reason_code": "policy_admin_required"}) from exc
    except ValueError as exc:
        raise _case_error(exc) from exc


class EventMigrationBody(BaseModel):
    expected_revision: int = Field(ge=1)
    expected_job_id: str = Field(min_length=1)
    state_digest: str = Field(min_length=64, max_length=64)
    candidate_digest: str = Field(min_length=64, max_length=64)
    request_id: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=1000)


@router.post("/{project_id}/cases/{case_id}/event-migration-apply", dependencies=[Depends(project_maintainer)])
def event_migration_apply(project_id: str, case_id: str, body: EventMigrationBody, _sess: dict = Depends(current_session)):
    from mino_nexus.casework.event_migration import apply
    try:
        return ok(apply(project_id, case_id, **body.model_dump()))
    except ValueError as exc:
        raise _case_error(exc) from exc


@router.get("/{project_id}/cases/{case_id}/review-requirements")
def case_review_requirements(project_id: str, case_id: str, _sess: dict = Depends(current_session)):
    _project(project_id)
    from mino_nexus.casework.case_store import get_case
    from mino_nexus.casework.case_review import describe
    case = get_case(project_id, case_id)
    if not case:
        raise HTTPException(status_code=404, detail="用例不存在")
    return ok(describe(case))


@router.put("/{project_id}/cases/{case_id}/review-requirements", dependencies=[Depends(project_maintainer)])
def case_review_supplement(project_id: str, case_id: str, body: CaseReviewBody, _sess: dict = Depends(current_session)):
    _project(project_id)
    from mino_nexus.casework.case_review import supplement
    try:
        return ok(supplement(project_id, case_id, body.expected_revision, body.source.model_dump(), body.reason))
    except ValueError as exc:
        code = str(exc)
        raise HTTPException(status_code=409 if code == "case_revision_conflict" else 404 if code == "case_missing" else 400, detail=code) from exc


@router.get("/{project_id}/controls")
def v5_controls(project_id: str, _sess: dict = Depends(current_session)):
    _project(project_id)
    from mino_nexus.casework.v5_controls import list_controls, audit
    return ok({"items": list_controls(project_id), "identity_conflicts": audit(project_id)})


@router.put("/{project_id}/controls/{control_id}", dependencies=[Depends(project_maintainer)])
def v5_configure_control(project_id: str, control_id: str, body: V5ControlBody, _sess: dict = Depends(current_session)):
    _project(project_id)
    from mino_nexus.casework.v5_controls import configure
    try:
        return ok(configure(project_id, control_id, **body.model_dump()))
    except ValueError as exc:
        raise HTTPException(status_code=409 if "conflict" in str(exc) else 400, detail={"reason_code": str(exc)}) from exc


@router.post("/{project_id}/controls/{control_id}/probe")
def v5_probe_control(project_id: str, control_id: str, body: V5ControlProbeBody, _sess: dict = Depends(current_session)):
    _project(project_id)
    from mino_nexus.loop.v5_probe import probe
    try:
        return ok(probe(project_id, control_id, **body.model_dump()))
    except ValueError as exc:
        raise HTTPException(status_code=409 if "conflict" in str(exc) else 400, detail={"reason_code": str(exc)}) from exc


@router.get("/{project_id}/cases/{case_id}/v5/preview")
def v5_case_preview(project_id: str, case_id: str, _sess: dict = Depends(current_session)):
    _project(project_id)
    from mino_nexus.casework.case_store import get_case
    from mino_nexus.casework.v5_publication import preview
    case = get_case(project_id, case_id)
    if not case:
        raise HTTPException(status_code=404, detail="用例不存在")
    return ok(preview(case))


@router.get("/{project_id}/cases/{case_id}/execution-preview")
@router.get("/{project_id}/cases/{case_id}/v5/execution-preview")
def v5_execution_preview(project_id: str, case_id: str, sn: str = Query(..., min_length=1),
                         app_id: str = Query(..., min_length=1), env_profile: str = '', env_surface: str = '',
                         action_scheme: str = 'dom_preferred', provider_id: str = '',
                         _sess: dict = Depends(current_session)):
    from mino_nexus.casework.execution_binding_runtime import binding_target_config
    try:
        binding_target_config(project_id, app_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail={"reason_code": str(exc)}) from exc
    from mino_nexus.casework.case_store import get_case
    from mino_nexus.casework.execution_binding_runtime import resolve_live
    case = get_case(project_id, case_id)
    if not case:
        raise HTTPException(status_code=404, detail='用例不存在')
    try:
        from mino_nexus.casework.execution_requirements import ordinary_requirements
        from mino_nexus.resources.node_registry import get_registry
        from mino_nexus.action_space.scheme import normalize_action_scheme
        snapshot = get_registry().execution_snapshot(sn)
        if not snapshot:
            raise ValueError('binding_target_unavailable')
        requirements = ordinary_requirements(case, app_id, platform=snapshot['device']['platform'],
            action_scheme=normalize_action_scheme(action_scheme), provider_id=provider_id)
        result = resolve_live(requirements, {'sn': sn, 'env_profile': env_profile, 'env_surface': env_surface})
    except (ValueError, KeyError) as exc:
        return ok({'status': 'needs_input' if str(exc).endswith('need_compile') else 'blocked', 'binding': None,
            'diagnostics': getattr(exc, 'diagnostics', None) or [{'reason_code': str(exc), 'responsibility': 'configuration',
                'case_ref': case_id, 'step_ref': '', 'clause_ref': '', 'operation_ref': '', 'missing_fields': [],
                'recovery_entry': 'workbench.execution_review', 'recheckable': True}],
            'scope': 'execution_binding_only', 'runnable_now': False})
    return ok({**result.model_dump(), 'scope': 'execution_binding_only', 'runnable_now': False})




@router.post("/{project_id}/cases/{case_id}/v5/compile", dependencies=[Depends(project_maintainer)])
def v5_compile_draft(project_id: str, case_id: str, body: CompileJobBody | None = None, _sess: dict = Depends(current_session)):
    import uuid
    from mino_nexus.casework.case_store import get_case
    from mino_nexus.casework.case_compilation import enqueue
    from mino_nexus.casework.v5_publication import preview
    _project(project_id)
    case = get_case(project_id, case_id)
    if not case:
        raise HTTPException(status_code=404, detail="用例不存在")
    try:
        job = enqueue(project_id, case_id, body.expected_revision if body else int(case.get("v5_draft_revision") or 1),
            body.client_request_id if body else "manual:" + uuid.uuid4().hex)
        saved = get_case(project_id, case_id)
        return ok({"case": saved, "job": job, "preview": preview(saved)}, msg="编译任务已排队，请查询任务状态")
    except ValueError as exc:
        raise _case_error(exc) from exc




@router.get("/{project_id}/v5/metrics")
def v5_weekly_metrics(project_id: str, _sess: dict = Depends(current_session)):
    _project(project_id)
    from mino_nexus.casework.v5_metrics import weekly
    return ok(weekly(project_id))


@router.post("/{project_id}/resource-key/preview")
def preview_resource_key(project_id: str, body: ResourceKeyPreviewBody, _sess: dict = Depends(current_session)):
    _project(project_id)
    from mino_nexus.resources import project_store as ps
    from mino_nexus.resources.case_resource_claim import (
        compile_resource_key_from_precondition,
        default_package_for_project,
    )
    from mino_nexus.resources.resource_claim_summary import resource_claim_summary

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
def list_project_cases(project_id: str, limit: int | None = Query(None, ge=1, le=200), offset: int = Query(0, ge=0),
                       search: str = "", module: str = "", app_id: str = "", result: str = "all",
                       _sess: dict = Depends(current_session)):
    _project(project_id)
    if limit is not None:
        if result not in ("all", "failed", "never"):
            raise HTTPException(status_code=400, detail="未知结果筛选")
        if app_id:
            app = ps.find_app(app_id)
            if not app or str(app.get("project_id") or "") != project_id:
                raise HTTPException(status_code=400, detail="应用不属于当前项目")
        elif result != "all":
            raise HTTPException(status_code=400, detail="结果筛选需要 app_id")
        from mino_nexus.casework.case_store import list_case_page

        return ok(list_case_page(project_id, limit=limit, offset=offset, search=search, module=module, app_id=app_id, result=result))
    return ok(cim.cases_payload(project_id))


@router.get("/{project_id}/case-modules")
def list_case_modules(project_id: str, _sess: dict = Depends(current_session)):
    _project(project_id)
    from mino_nexus.casework.case_store import case_modules

    return ok(case_modules(project_id))


@router.post("/{project_id}/cases/preflight")
def post_case_preflight(project_id: str, body: CasePreflightBody, _sess: dict = Depends(current_session)):
    _project(project_id)
    try:
        app = ps.require_app(body.app_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="App not found") from exc
    if str(app.get("project_id") or "") != str(project_id):
        raise HTTPException(status_code=400, detail="app 与项目不匹配")
    if body.recompile_stale:
        project_maintainer(project_id, _sess)
    from mino_nexus.casework.case_preflight import preflight

    try:
        return ok(preflight(app, body.case_ids, recompile_stale=body.recompile_stale))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{project_id}/cases/{case_id}")
def get_project_case(project_id: str, case_id: str, _sess: dict = Depends(current_session)):
    from mino_nexus.casework.case_key_registry import step_keys_summary_for_case
    from mino_nexus.casework.case_store import get_case

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


@router.post("/{project_id}/requirements")
def create_import_requirement(project_id: str, body: ImportRequirementCreateBody, _sess: dict = Depends(project_maintainer)):
    try:
        return ok({"requirement": cim.create_project_requirement(project_id, body.app_id, body.title)})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{project_id}/cases/import/sources")
def import_source_nodes(project_id: str, _sess: dict = Depends(project_maintainer)):
    from mino_nexus.casework.case_import_sources import nodes
    return ok(nodes(project_id))


@router.post("/{project_id}/cases/import/read-source")
def import_read_source(project_id: str, body: CaseImportSourceBody, _sess: dict = Depends(project_maintainer)):
    from mino_nexus.casework.case_import_sources import read
    try:
        return ok(read(project_id, body.kind, body.configuration, body.operation))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"reason_code": str(exc)}) from exc


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


@router.get("/{project_id}/cases/import/preview/{token}")
def import_preview_status(project_id: str, token: str, _sess: dict = Depends(current_session)):
    _project(project_id)
    data = cim.preview_status(token, project_id)
    if not data:
        raise HTTPException(status_code=404, detail="预览已过期")
    return ok(data)


@router.post("/{project_id}/cases/import/preview/{token}/cancel")
def import_preview_cancel(project_id: str, token: str, _sess: dict = Depends(current_session)):
    _project(project_id)
    if not cim.cancel_preview(token, project_id):
        raise HTTPException(status_code=404, detail="预览已过期")
    return ok({"cancelled": True})


class PreviewRetryBody(BaseModel):
    row_index: int


@router.post("/{project_id}/cases/import/preview/{token}/retry")
def import_preview_retry(
    project_id: str,
    token: str,
    body: PreviewRetryBody,
    _sess: dict = Depends(current_session),
):
    _project(project_id)
    if not cim.retry_preview_row(token, body.row_index, project_id):
        raise HTTPException(status_code=400, detail="这一行不能重试")
    return ok(cim.preview_status(token, project_id) or {})


@router.put("/{project_id}/cases/{case_id}", dependencies=[Depends(project_maintainer)])
def update_one_case(
    project_id: str,
    case_id: str,
    body: CaseUpdateBody,
    _sess: dict = Depends(current_session),
):
    from mino_nexus.casework.case_store import get_case, save_case

    _project(project_id)
    cid = str(case_id or "").strip()
    existing = get_case(project_id, cid)
    if not existing:
        raise HTTPException(status_code=404, detail="用例不存在")
    patch = body.model_dump(exclude_unset=True)
    merged = {**existing, **patch, "case_id": cid}
    rid = str(patch.get("requirement_id") or existing.get("requirement_id") or "").strip()
    try:
        saved = save_case(project_id, merged, requirement_id=rid)
    except ValueError as exc:
        raise _case_error(exc) from exc
    return ok({"case": saved}, msg="用例已保存")


@router.post("/{project_id}/cases/step-keys/validate")
def validate_case_step_keys_body(
    project_id: str,
    body: CaseStepKeysValidateBody,
    _sess: dict = Depends(current_session),
):
    from mino_nexus.casework.case_key_registry import validate_case_step_keys

    _project(project_id)
    case_like = {
        "project_id": project_id,
        "app_id": str(body.app_id or "").strip(),
        "precondition": body.precondition,
        "steps_raw": body.steps_raw,
        "expected_raw": body.expected_raw,
        "steps": body.steps,
        "expected": body.expected,
    }
    try:
        return ok(validate_case_step_keys(case_like, app_id=str(body.app_id or "").strip()))
    except ValueError as exc:
        if str(exc) not in {"compile_provider_unavailable", "compile_prompt_unavailable",
            "compile_model_call_failed", "compile_model_response_incomplete"}:
            raise
        raise HTTPException(status_code=409, detail={"reason_code": str(exc)}) from exc


@router.post("/{project_id}/cases/{case_id}/step-keys/compile", dependencies=[Depends(project_maintainer)])
def compile_case_step_keys(project_id: str, case_id: str, body: CompileJobBody | None = None, _sess: dict = Depends(current_session)):
    import uuid
    from mino_nexus.casework.case_store import get_case
    from mino_nexus.casework.case_compilation import enqueue
    from mino_nexus.casework.v5_publication import preview
    _project(project_id)
    case = get_case(project_id, case_id)
    if not case:
        raise HTTPException(status_code=404, detail="用例不存在")
    try:
        job = enqueue(project_id, case_id, body.expected_revision if body else int(case.get("v5_draft_revision") or 1),
            body.client_request_id if body else "manual:" + uuid.uuid4().hex)
        saved = get_case(project_id, case_id)
        return ok({"case": saved, "job": job, "preview": preview(saved)}, msg="编译任务已排队，请查询任务状态")
    except ValueError as exc:
        raise _case_error(exc) from exc


@router.post("/{project_id}/cases/{case_id}/resource-key/compile", dependencies=[Depends(project_maintainer)])
def compile_case_resource_key(project_id: str, case_id: str, body: CompileJobBody | None = None, _sess: dict = Depends(current_session)):
    import uuid
    from mino_nexus.casework.case_store import get_case
    from mino_nexus.casework.case_compilation import enqueue
    from mino_nexus.casework.v5_publication import preview
    _project(project_id)
    case = get_case(project_id, case_id)
    if not case:
        raise HTTPException(status_code=404, detail="用例不存在")
    try:
        job = enqueue(project_id, case_id, body.expected_revision if body else int(case.get("v5_draft_revision") or 1),
            body.client_request_id if body else "manual:" + uuid.uuid4().hex)
        saved = get_case(project_id, case_id)
        return ok({"case": saved, "job": job, "preview": preview(saved)}, msg="编译任务已排队，请查询任务状态")
    except ValueError as exc:
        raise _case_error(exc) from exc


@router.delete("/{project_id}/cases/{case_id}", dependencies=[Depends(project_maintainer)])
def delete_one_case(project_id: str, case_id: str, _sess: dict = Depends(current_session)):
    from mino_nexus.casework.case_store import delete_case

    _project(project_id)
    if not delete_case(project_id, case_id):
        raise HTTPException(status_code=404, detail="用例不存在")
    return ok({"deleted": 1, "case_id": case_id}, msg="已删除")


@router.post("/{project_id}/cases/delete", dependencies=[Depends(project_maintainer)])
def delete_many_cases(project_id: str, body: CaseDeleteBody, _sess: dict = Depends(current_session)):
    from mino_nexus.casework.case_store import delete_cases

    _project(project_id)
    ids = [str(x).strip() for x in (body.case_ids or []) if str(x).strip()]
    if not ids:
        raise HTTPException(status_code=400, detail="请指定要删除的用例编号")
    removed = delete_cases(project_id, ids)
    return ok({"deleted": removed, "case_ids": ids[:50]}, msg=f"已删除 {removed} 条")


@router.post("/{project_id}/cases/import/commit", dependencies=[Depends(project_maintainer)])
def import_commit(project_id: str, body: CaseImportCommitBody, _sess: dict = Depends(current_session)):
    _project(project_id)
    try:
        rows = [r.model_dump() for r in body.rows] if body.rows else None
        default_action = "overwrite" if body.default_on_conflict == "overwrite" else "skip"
        from mino_nexus.platform.settings_store import resolve_block_on_step_key_issues

        data = cim.commit_import(
            project_id=project_id,
            requirement_id=body.requirement_id,
            preview_token=body.preview_token,
            rows=rows,
            default_on_conflict=default_action,
            default_platform=str(body.default_platform or "").strip(),
            block_on_step_key_issues=resolve_block_on_step_key_issues(body.block_on_step_key_issues),
            import_policy=body.import_policy,
            client_request_id=body.client_request_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409 if str(exc) == "import_request_conflict" else 400, detail=str(exc)) from exc
    if data.get("blocked") and not data.get("created") and not data.get("updated"):
        return ok(data, msg="没有可导入的 UI 可执行用例；阻断项和前置准备项见导入结果")
    return ok(data, msg="用例已导入")


@router.get("/{project_id}/cases/import/receipts/by-request/{client_request_id}")
def import_receipt_by_request(project_id: str, client_request_id: str, _sess: dict = Depends(current_session)):
    _project(project_id)
    from mino_nexus.casework.case_import_receipts import get
    data = get(project_id, client_request_id=client_request_id)
    if data is None:
        raise HTTPException(status_code=404, detail="未找到本次提交收据")
    return ok(data)


@router.get("/{project_id}/cases/import/receipts/{receipt_id}")
def import_receipt(project_id: str, receipt_id: str, _sess: dict = Depends(current_session)):
    _project(project_id)
    from mino_nexus.casework.case_import_receipts import get
    data = get(project_id, receipt_id=receipt_id)
    if data is None:
        raise HTTPException(status_code=404, detail="未找到本次提交收据")
    return ok(data)


@router.post("/{project_id}/cases/import/receipts/{receipt_id}/resume", dependencies=[Depends(project_maintainer)])
def resume_import_receipt(project_id: str, receipt_id: str, _sess: dict = Depends(current_session)):
    _project(project_id)
    from mino_nexus.casework.case_import_receipts import get, resume
    if get(project_id, receipt_id=receipt_id) is None:
        raise HTTPException(status_code=404, detail="未找到本次提交收据")
    return ok(resume(project_id, receipt_id))


@router.get("/{project_id}/apps/{app_id}/issues")
def application_issues(project_id: str, app_id: str,
                       platform: str = Query(default="", max_length=40),
                       category: str = Query(default="", max_length=80),
                       status: str = Query(default="", max_length=80),
                       page: int = Query(default=1, ge=1),
                       size: int = Query(default=50, ge=1, le=100),
                       _sess: dict = Depends(current_session)):
    from mino_nexus.casework.app_issues import list_issues
    return ok(list_issues(project_id, app_id, platform=platform.lower(),
                          category=category, status=status, page=page, size=size))
