"""应用配置 / 用例草稿 / qa-process / Figma 同步。"""
from __future__ import annotations

import threading
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from mino_nexus.services import app_automation as aas
from mino_nexus.services import project_store as ps
from mino_nexus.core.http_util import ok
from mino_nexus.routers.deps import current_session

router = APIRouter(prefix="/app-automation", tags=["App Automation"])


class PlaybookSaveBody(BaseModel):
    playbook: dict[str, Any] = {}


class SkillsBlock(BaseModel):
    pre: list[str] = []
    post: list[str] = []


class AutomationSkills(BaseModel):
    default: SkillsBlock = SkillsBlock()
    devices: dict[str, SkillsBlock] = {}


class ExecutionEnvConfig(BaseModel):
    mode: str = "fixed"
    profile: str = "test"


class FigmaDesignConfig(BaseModel):
    file_url: str = ""
    file_key: str = ""
    last_sync_at: str = ""
    pages_summary: list[str] = []


class CaseSuiteBody(BaseModel):
    id: str = ""
    name: str
    case_ids: list[str] = []
    updated_at: str = ""


class AutomationConfigUpdate(BaseModel):
    env_profile: Optional[str] = None
    execution_env: Optional[ExecutionEnvConfig] = None
    skills: Optional[AutomationSkills] = None
    figma: Optional[FigmaDesignConfig] = None
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


class QaProcessTickBody(BaseModel):
    requirement_id: str = ""
    user_note: str = ""
    force: bool = False
    jobs: list[str] = []
    point_ids: list[str] = []
    rewrite_stubs: bool = False
    replace_cases: bool = False


class CoverImportBody(BaseModel):
    requirement_id: str = ""
    kind: str = "mindmap"
    text: str = ""
    filename: str = ""
    replace: bool = False


class AtlasPatchBody(BaseModel):
    patch_id: str
    action: str = "accept"
    after: dict | None = None
    reason: str = ""
    note: str = ""
    rerun: bool = True
    run_pipeline: bool = True
    release_id: str = ""


class AtlasAliasUpdateBody(BaseModel):
    review_status: str = ""
    note: str = ""


class FigmaSyncBody(BaseModel):
    file_url: str = ""
    file_key: str = ""
    access_token: str = ""


class FigmaApplyLogicBody(BaseModel):
    file_url: str = ""
    file_key: str = ""
    access_token: str = ""
    write_knowledge: bool = True
    write_graph: bool = True


class PublishMindmapBody(BaseModel):
    requirement_id: str = ""


class HideMindmapWikiBody(BaseModel):
    requirement_id: str = ""


def _app(app_id: str) -> dict[str, Any]:
    try:
        return ps.require_app(app_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="App not found") from exc


@router.get("/config/{app_id}")
def get_automation_config(app_id: str, _sess: dict = Depends(current_session)):
    return ok(aas.config_payload(_app(app_id)))


@router.put("/config/{app_id}")
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


@router.put("/playbook/{app_id}")
def save_app_playbook(app_id: str, body: PlaybookSaveBody, _sess: dict = Depends(current_session)):
    app = _app(app_id)
    pb = aas.save_playbook(app, body.playbook or {})
    return ok({"playbook": pb}, msg="已保存应用基础逻辑")


@router.get("/qa-process/summary")
def qa_process_summary(_sess: dict = Depends(current_session)):
    return ok({"items": aas.qa_process_summary()})


@router.get("/cases/{app_id}")
def list_cached_cases(app_id: str, _sess: dict = Depends(current_session)):
    return ok(aas.cases_payload(_app(app_id)))


@router.get("/runs/{app_id}")
def list_app_runs(app_id: str, _sess: dict = Depends(current_session)):
    _app(app_id)
    from mino_nexus.services import run_store

    rows = run_store.list_runs(limit=30, app_id=app_id)
    return ok({"runs": [run_store.to_task_json(r, include_cases=False) for r in rows]})


@router.post("/qa-process/assist/{app_id}")
def qa_assist(app_id: str, body: QaProcessAssistBody, _sess: dict = Depends(current_session)):
    from mino_nexus.services import qa_process_assist as assist

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


@router.post("/qa-process/tick/{app_id}")
def qa_tick(app_id: str, body: QaProcessTickBody, _sess: dict = Depends(current_session)):
    from mino_nexus.services import qa_process_jobs as cover_jobs

    app = _app(app_id)
    cfg = aas.get_automation_config(app)
    cases = aas.list_app_cases(app)
    try:
        job = cover_jobs.create(
            app_id=app_id,
            requirement_id=body.requirement_id or "",
            jobs=body.jobs or [],
        )
    except cover_jobs.JobConflict as exc:
        snap = exc.job.public()
        raise HTTPException(status_code=409, detail={"message": "已有推进任务在跑", "job": snap}) from exc

    def flush(doc: dict) -> None:
        row = ps.find_app(app_id)
        if not row:
            return
        aas.save_automation_config(row, {"qa_process": doc})

    job.flush = flush
    job.report(phase="queued", label="已排队")
    seed = dict(cfg.get("qa_process") or {})
    job.save(seed)
    threading.Thread(
        target=_tick_in_background,
        kwargs={
            "project_id": str(app.get("project_id") or ""),
            "qa_process": cfg.get("qa_process") or {},
            "cases": cases,
            "job_id": job.id,
            "requirement_id": body.requirement_id or "",
            "user_note": body.user_note or "",
            "force": body.force,
            "jobs": body.jobs or [],
            "point_ids": body.point_ids or [],
            "rewrite_stubs": body.rewrite_stubs,
            "replace_cases": body.replace_cases,
        },
        daemon=True,
        name=f"qa-tick-{job.id}",
    ).start()
    return ok({"job_id": job.id, "job": job.public()})


@router.get("/qa-process/job/{job_id}")
def qa_job(job_id: str, _sess: dict = Depends(current_session)):
    from mino_nexus.services import qa_process_jobs as cover_jobs

    job = cover_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    data: dict[str, Any] = {"job": job.public()}
    if isinstance(job.doc, dict):
        data["qa_process"] = job.doc
    if job.status in ("done", "cancelled", "error") and job.result:
        data["actions"] = job.result.get("actions") or []
        data["autonomy"] = job.result.get("autonomy") or {}
        data["usage"] = job.result.get("usage") or {}
    return ok(data)


@router.post("/qa-process/job/{job_id}/cancel")
def qa_cancel(job_id: str, _sess: dict = Depends(current_session)):
    from mino_nexus.services import qa_process_jobs as cover_jobs

    job = cover_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    job.cancel()
    return ok({"job": job.public()})


def _tick_in_background(
    *,
    project_id: str,
    qa_process: dict,
    cases: list,
    job_id: str,
    requirement_id: str,
    user_note: str,
    force: bool,
    jobs: list,
    point_ids: list,
    rewrite_stubs: bool,
    replace_cases: bool,
) -> None:
    from mino_nexus.services import qa_process_jobs as cover_jobs
    from mino_nexus.services.qa_cover import tick

    job = cover_jobs.get(job_id)
    if not job:
        return
    job_tok = cover_jobs.bind(job)
    try:
        job.report(phase="running", label="开始推进")
        result = tick(
            qa_process=qa_process,
            cases=cases,
            project_id=project_id,
            requirement_id=requirement_id,
            user_note=user_note,
            force=force,
            jobs=jobs,
            point_ids=point_ids,
            rewrite_stubs=rewrite_stubs,
            replace_cases=replace_cases,
        )
        job.finish(result)
    except cover_jobs.Cancelled:
        if isinstance(job.doc, dict):
            job.result = {
                "qa_process": job.doc,
                "actions": [{"role": "system", "action": "cancelled", "detail": "已取消"}],
                "autonomy": (job.doc.get("autonomy") if isinstance(job.doc.get("autonomy"), dict) else {}),
                "usage": {},
            }
        job.mark_cancelled()
    except Exception as exc:
        job.fail(str(exc)[:240])
    finally:
        cover_jobs.release(job)
        cover_jobs.reset(job_tok)


@router.post("/qa-process/import/{app_id}")
def qa_import(app_id: str, body: CoverImportBody, _sess: dict = Depends(current_session)):
    from mino_nexus.services.qa_cover import import_cover

    app = _app(app_id)
    cfg = aas.get_automation_config(app)
    try:
        result = import_cover(
            qa_process=cfg.get("qa_process") or {},
            project_id=str(app.get("project_id") or ""),
            requirement_id=body.requirement_id,
            kind=body.kind,
            text=body.text,
            filename=body.filename,
            replace=body.replace,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    saved = aas.save_automation_config(app, {"qa_process": result.get("qa_process") or {}})
    result["qa_process"] = saved.get("qa_process") or result.get("qa_process") or {}
    return ok(result)


@router.post("/qa-process/publish-mindmap/{app_id}")
def qa_mindmap(app_id: str, body: PublishMindmapBody, _sess: dict = Depends(current_session)):
    from mino_nexus.services.qa_cover import publish_mindmap

    app = _app(app_id)
    cfg = aas.get_automation_config(app)
    try:
        result = publish_mindmap(cfg.get("qa_process") or {}, requirement_id=body.requirement_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    saved = aas.save_automation_config(app, {"qa_process": result.get("qa_process") or {}})
    result["qa_process"] = saved.get("qa_process") or {}
    return ok(result)


@router.post("/qa-process/hide-mindmap-wiki/{app_id}")
def qa_hide(app_id: str, body: HideMindmapWikiBody, _sess: dict = Depends(current_session)):
    from mino_nexus.services.qa_cover import hide_mindmap

    app = _app(app_id)
    cfg = aas.get_automation_config(app)
    try:
        result = hide_mindmap(cfg.get("qa_process") or {}, requirement_id=body.requirement_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    saved = aas.save_automation_config(app, {"qa_process": result.get("qa_process") or {}})
    result["qa_process"] = saved.get("qa_process") or {}
    return ok(result)


@router.post("/qa-process/atlas-patch/{app_id}")
def qa_atlas(app_id: str, body: AtlasPatchBody, _sess: dict = Depends(current_session)):
    from mino_nexus.services.qa_cover import apply_atlas_patch

    app = _app(app_id)
    cfg = aas.get_automation_config(app)
    try:
        result = apply_atlas_patch(
            cfg.get("qa_process") or {},
            patch_id=body.patch_id,
            action=body.action,
            after=body.after,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    saved = aas.save_automation_config(app, {"qa_process": result.get("qa_process") or {}})
    result["qa_process"] = saved.get("qa_process") or {}
    return ok(result)


@router.get("/qa-process/atlas-aliases/{app_id}")
def atlas_aliases(app_id: str, _sess: dict = Depends(current_session)):
    from mino_nexus.services import atlas_aliases as aliases

    _app(app_id)
    return ok({"items": aliases.list_aliases(app_id)})


@router.patch("/qa-process/atlas-aliases/{app_id}/{alias_id}")
def patch_alias(app_id: str, alias_id: str, body: AtlasAliasUpdateBody, _sess: dict = Depends(current_session)):
    from mino_nexus.services import atlas_aliases as aliases

    _app(app_id)
    row = aliases.patch(app_id, alias_id, body.model_dump(exclude_none=True))
    if not row:
        raise HTTPException(status_code=404, detail="别名不存在")
    return ok(row)


@router.delete("/qa-process/atlas-aliases/{app_id}/{alias_id}")
def delete_alias(app_id: str, alias_id: str, _sess: dict = Depends(current_session)):
    from mino_nexus.services import atlas_aliases as aliases

    _app(app_id)
    if not aliases.delete(app_id, alias_id):
        raise HTTPException(status_code=404, detail="别名不存在")
    return ok(msg="已删除")


@router.post("/config/{app_id}/figma/sync")
def figma_sync(app_id: str, body: FigmaSyncBody, _sess: dict = Depends(current_session)):
    from mino_nexus.services import figma_service as fs
    from mino_nexus.services import settings_store as ss

    app = _app(app_id)
    file_key = ""
    try:
        file_key = fs.parse_file_key(file_url=body.file_url, file_key=body.file_key)
    except ValueError:
        file_key = str(body.file_key or "").strip()
    if body.file_url or file_key:
        aas.save_automation_config(app, {"figma": {"file_url": body.file_url or "", "file_key": file_key}})
        app = _app(app_id)
    token = (body.access_token or ss.get_figma_access_token() or "").strip()
    if not token:
        raise HTTPException(
            status_code=400,
            detail="还没有设计稿凭证。请到 Studio → 设置 → Figma 填写 Personal Access Token，或在同步请求里带 access_token。",
        )
    try:
        synced = fs.sync_figma_file(
            file_url=body.file_url,
            file_key=body.file_key,
            depth=8,
            token=token,
            include_raw_document=False,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    payload = {k: v for k, v in synced.items() if k != "raw_document"}
    cfg = aas.save_automation_config(app, {"figma": payload})
    return ok(
        {
            "figma": cfg.get("figma") or payload,
            "page_count": synced.get("page_count", 0),
            "frame_count": synced.get("frame_count", 0),
            "logic_pages": len((synced.get("logic") or {}).get("pages") or []),
            "login_icons": {},
        },
        msg="设计稿已同步",
    )


@router.post("/config/{app_id}/figma/apply-logic")
def figma_apply(app_id: str, body: FigmaApplyLogicBody, _sess: dict = Depends(current_session)):
    from mino_nexus.services import figma_logic as fls
    from mino_nexus.services import settings_store as ss

    app = _app(app_id)
    if body.file_url or body.file_key:
        aas.save_automation_config(app, {"figma": {"file_url": body.file_url or "", "file_key": body.file_key or ""}})
        app = _app(app_id)
    token = (body.access_token or ss.get_figma_access_token() or "").strip()
    if not token:
        raise HTTPException(
            status_code=400,
            detail="还没有设计稿凭证。请到 Studio → 设置 → Figma 填写 Personal Access Token，或在请求里带 access_token。",
        )
    try:
        result = fls.apply_figma_logic(
            app,
            file_url=body.file_url,
            file_key=body.file_key,
            token=token,
            write_knowledge=body.write_knowledge,
            write_graph=body.write_graph,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return ok(result, msg="已从 Figma 学习应用逻辑")

