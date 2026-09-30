"""CaseRunner：启动、取消、重跑任务。会话轨迹的读接口在 rObserve。"""
from __future__ import annotations

from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict

from mino_nexus.services import project_store as ps
from mino_nexus.services import run_store
from mino_nexus.core.http_util import ok
from mino_nexus.loop import case_runner as cr
from mino_nexus.loop.observe.agent_stream import emit_testing_task
from mino_nexus.loop.web.web_env import release_devices_for_run
from mino_nexus.routers.deps import current_session
from mino_nexus.services.ui_devices import ui_devices
from mino_nexus.runtime.run_context import WEB_PLAYWRIGHT_PARALLEL_LANES

router = APIRouter(prefix="/case-runner", tags=["CaseRunner"])


def _plugin_user(sess: dict[str, Any]) -> str:
    return str(sess.get("user_id") or "").strip()


class ExploreRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    app_id: str = ""
    sn: str = ""
    platform: str = "android"
    async_exec: bool = True
    instruction: str = ""
    provider_id: str = ""
    max_steps: int = 80
    max_idle_steps: int = 15
    playwright_headless: bool = True
    env_profile: str = ""
    env_surface: str = ""


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    app_id: str = ""
    sn: str = ""
    sns: Optional[List[str]] = None
    coverage: str = ""
    platform: str = "android"
    case_ids: Optional[List[str]] = None
    start_index: int = 0
    async_exec: bool = True
    use_persisted_baseline: bool = True
    use_cache: bool = True
    execution_mode: str = "agent"
    instruction: str = ""
    run_type: str = "manual"
    slot_id: str = ""
    requirement_id: str = ""
    release_id: str = ""
    provider_id: str = ""
    playwright_headless: bool = True
    env_profile: str = ""
    env_surface: str = ""
    action_scheme: str = "visual"


class PromoteBaselineRequest(BaseModel):
    run_id: str
    blessed_by: str = "manual"
    notes: str = ""


class RetryFailedRequest(BaseModel):
    sn: str = ""
    execution_mode: str = "agent"


class TaskAccountTemplateStateBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    account_id: str
    facets: Optional[dict[str, Any]] = None
    env: Optional[str] = None
    display_name: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    otp: Optional[str] = None
    note: Optional[str] = None
    case_id: str = ""
    sn: str = ""
    package_id: str = ""
    message: str = ""


@router.get("/devices")
def list_devices(only_online: bool = Query(True), _sess: dict = Depends(current_session)):
    items = ui_devices()
    if only_online:
        items = [d for d in items if d.get("status") == "online"]
    running = run_store.running_index()
    for row in items:
        sn = str(row.get("sn") or "")
        plat = str(row.get("platform") or row.get("device_type") or row.get("type") or "")
        from mino_nexus.runtime.run_context import is_web_slot

        ids = running.get(sn) or []
        if is_web_slot(sn, plat):
            row["active_run_count"] = len(ids)
            row["web_parallel_max"] = WEB_PLAYWRIGHT_PARALLEL_LANES
            row["web_parallel_full"] = len(ids) >= WEB_PLAYWRIGHT_PARALLEL_LANES
            row["busy_task_id"] = ""
        else:
            row["active_run_count"] = 0
            row["web_parallel_max"] = 0
            row["web_parallel_full"] = False
            row["busy_task_id"] = ids[0] if ids else ""
    return ok({"count": len(items), "items": items})


@router.post("/run")
def run_cases(body: RunRequest, sess: dict = Depends(current_session)):
    if not str(body.app_id or "").strip():
        raise HTTPException(status_code=400, detail="缺少 app_id")
    try:
        app = ps.require_app(body.app_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="App not found") from exc
    cov = str(body.coverage or "").strip().lower()
    if cov not in ("", "once", "per_device"):
        raise HTTPException(status_code=400, detail="coverage 必须是 once 或 per_device")
    try:
        snapshot = cr.run_cases(
            app,
            sn=body.sn,
            sns=body.sns,
            coverage=cov,
            platform=body.platform or "android",
            case_ids=body.case_ids,
            start_index=body.start_index or 0,
            async_exec=body.async_exec,
            run_type=(body.run_type or "manual").lower(),
            requirement_id=body.requirement_id or "",
            release_id=body.release_id or "",
            slot_id=body.slot_id or "",
            instruction=str(body.instruction or "").strip(),
            provider_id=str(body.provider_id or "").strip(),
            playwright_headless=bool(body.playwright_headless),
            env_profile=str(body.env_profile or "").strip(),
            env_surface=str(body.env_surface or "").strip(),
            action_scheme=str(body.action_scheme or "visual").strip().lower(),
            plugin_user_id=_plugin_user(sess),
        )
        return ok(snapshot, msg="AI-led 回归任务已启动")
    except cr.DeviceBusy as exc:
        raise HTTPException(
            status_code=409,
            detail={"message": "device busy", "busy_task_id": exc.busy_task_id, "sn": exc.sn},
        ) from exc
    except cr.WebSlotFull as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "web parallel full",
                "sn": exc.sn,
                "active": exc.active,
                "max": exc.limit,
            },
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/explore")
def run_explore(body: ExploreRequest, sess: dict = Depends(current_session)):
    if not str(body.app_id or "").strip():
        raise HTTPException(status_code=400, detail="缺少 app_id")
    try:
        app = ps.require_app(body.app_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="App not found") from exc
    try:
        snapshot = cr.run_explore(
            app,
            sn=body.sn,
            max_steps=int(body.max_steps or 80),
            max_idle_steps=int(body.max_idle_steps or 15),
            instruction=str(body.instruction or "").strip(),
            provider_id=str(body.provider_id or "").strip(),
            async_exec=bool(body.async_exec),
            platform=body.platform or "android",
            playwright_headless=bool(body.playwright_headless),
            env_profile=str(body.env_profile or "").strip(),
            env_surface=str(body.env_surface or "").strip(),
            plugin_user_id=_plugin_user(sess),
        )
        return ok(snapshot, msg="应用探索已启动")
    except cr.DeviceBusy as exc:
        raise HTTPException(
            status_code=409,
            detail={"message": "device busy", "busy_task_id": exc.busy_task_id, "sn": exc.sn},
        ) from exc
    except cr.WebSlotFull as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "web parallel full",
                "sn": exc.sn,
                "active": exc.active,
                "max": exc.limit,
            },
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/runs")
def list_runs(limit: int = Query(30), app_id: str = "", _sess: dict = Depends(current_session)):
    rows = run_store.list_runs(limit=limit, app_id=(app_id or "").strip())
    return ok({"runs": [run_store.to_task_json(r, include_cases=False) for r in rows], "limit": limit})


@router.get("/run/{run_id:path}")
def get_run(run_id: str, _sess: dict = Depends(current_session)):
    doc = run_store.get(run_id)
    if doc is None:
        raise HTTPException(status_code=404, detail=f"run not found: {run_id}")
    return ok(run_store.to_task_json(doc))


@router.get("/tasks/summary")
def tasks_summary(app_ids: str = Query(""), _sess: dict = Depends(current_session)):
    ids = [a.strip() for a in (app_ids or "").split(",") if a.strip()]
    return ok({"items": run_store.summary_for_apps(ids)})


@router.get("/tasks")
def list_tasks(
    app_id: str = "",
    status: str = "",
    limit: int = 50,
    offset: int = 0,
    _sess: dict = Depends(current_session),
):
    return ok(run_store.list_tasks(app_id=app_id, status=status, limit=limit, offset=offset))


@router.get("/tasks/{task_id}")
def get_task(task_id: str, _sess: dict = Depends(current_session)):
    doc = run_store.get(task_id)
    if doc is None:
        raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
    return ok(run_store.to_task_json(doc))


@router.get("/tasks/{task_id}/resource-restore-logs")
def list_task_resource_restore_logs(
    task_id: str,
    page: int = 1,
    page_size: int = 30,
    case_id: str = "",
    _sess: dict = Depends(current_session),
):
    from mino_nexus.services.account_facet_sync import resolve_task_project_id
    from mino_nexus.services.resource_allocation_log import ACTION_FACET_UPDATE, list_allocation_logs

    project_id, _app_id, err = resolve_task_project_id(task_id)
    if not project_id:
        raise HTTPException(status_code=400, detail=err or "无法解析项目")
    payload = list_allocation_logs(
        project_id,
        page=page,
        page_size=page_size,
        run_id=str(task_id or "").strip(),
        case_id=str(case_id or "").strip(),
        action=ACTION_FACET_UPDATE,
    )
    payload["project_id"] = project_id
    payload["task_id"] = str(task_id or "").strip()
    return ok(payload)


@router.post("/tasks/{task_id}/resource-restore-logs/{log_id}/restore")
def restore_task_resource_log(
    task_id: str,
    log_id: int,
    _sess: dict = Depends(current_session),
):
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.resource_ops import ResourceAllocationLog
    from mino_nexus.services.account_facet_sync import resolve_task_project_id, restore_account_from_allocation_log

    project_id, _app_id, err = resolve_task_project_id(task_id)
    if not project_id:
        raise HTTPException(status_code=400, detail=err or "无法解析项目")
    tid = str(task_id or "").strip()
    with session_scope() as db:
        row = (
            db.query(ResourceAllocationLog)
            .filter(ResourceAllocationLog.project_id == project_id, ResourceAllocationLog.id == int(log_id))
            .one_or_none()
        )
        if row is None:
            raise HTTPException(status_code=404, detail="日志不存在")
        if tid and tid not in str(row.run_id or ""):
            raise HTTPException(status_code=400, detail="该日志不属于本任务")
    saved, rerr = restore_account_from_allocation_log(project_id, log_id)
    if not saved:
        raise HTTPException(status_code=400, detail=rerr or "恢复失败")
    return ok({"account_id": str(saved.get("id") or ""), "log_id": log_id, "project_id": project_id}, msg="已从任务资源日志恢复")


@router.patch("/tasks/{task_id}/account-template-state")
def patch_task_account_template_state(
    task_id: str,
    body: TaskAccountTemplateStateBody,
    _sess: dict = Depends(current_session),
):
    from mino_nexus.services.account_facet_schema import facets_for_storage
    from mino_nexus.services.account_facet_sync import FacetLogContext, apply_account_template_state, resolve_task_project_id
    from mino_nexus.services.account_pool_templates import merged_pool_field_defs

    doc = run_store.get(task_id)
    if doc is None:
        raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
    project_id, _app_id, err = resolve_task_project_id(task_id)
    if not project_id:
        raise HTTPException(status_code=400, detail=err or "无法解析项目")
    account_id = str(body.account_id or "").strip()
    if not account_id:
        raise HTTPException(status_code=400, detail="account_id required")
    raw = body.model_dump(exclude_unset=True)
    raw.pop("account_id", None)
    facets = raw.pop("facets", None)
    message = str(raw.pop("message", "") or "执行任务修改模板参数")[:2000]
    case_id = str(raw.pop("case_id", "") or "")[:80]
    sn = str(raw.pop("sn", "") or doc.get("sn") or "")[:64]
    package_id = str(raw.pop("package_id", "") or "")[:120]
    log_ctx = FacetLogContext(
        source="run_task",
        run_id=str(task_id or "")[:80],
        case_id=case_id,
        sn=sn,
        package_id=package_id,
        env=str(raw.get("env") or doc.get("env_profile") or "")[:32],
        message=message,
    )
    row_patch = raw
    if facets is not None:
        env_doc = ps.project_env(project_id)
        stored = facets_for_storage(facets, merged_pool_field_defs(env_doc))
        saved, errors = apply_account_template_state(
            project_id,
            account_id,
            stored_facets=stored,
            row_patch=row_patch or None,
            log_ctx=log_ctx,
        )
    else:
        saved, errors = apply_account_template_state(
            project_id,
            account_id,
            row_patch=row_patch or None,
            log_ctx=log_ctx,
        )
    if not saved:
        raise HTTPException(status_code=400, detail=errors[0] if errors else "同步失败")
    env_doc = ps.project_env(project_id)
    from mino_nexus.services.account_pool_templates import merged_pool_field_defs
    from mino_nexus.services.project_env import public_test_accounts

    public = public_test_accounts(
        [saved],
        include_password=False,
        pool_field_defs=merged_pool_field_defs(env_doc),
    )[0]
    return ok({"account": public, "project_id": project_id, "warnings": errors}, msg="已同步到号池资源")


@router.post("/tasks/{task_id}/cancel")
def cancel_task(task_id: str, _sess: dict = Depends(current_session)):
    result = run_store.cancel_run(task_id, reason="已取消")
    if not result.get("ok"):
        raise HTTPException(status_code=int(result.get("code") or 404), detail=result.get("reason") or "cancel failed")
    doc = result.get("doc") or run_store.get(task_id) or {}
    if result.get("already"):
        return ok(result, msg="任务已结束")
    sns = list(doc.get("sns") or [])
    head = str(doc.get("sn") or "").strip()
    if head and head not in sns:
        sns = [head, *sns]
    release_devices_for_run(
        task_id,
        sns=sns,
        platforms_by_sn=doc.get("platforms_by_sn") if isinstance(doc.get("platforms_by_sn"), dict) else {},
    )
    from mino_nexus.services.run_resource_release import release_run_resource_holdings

    release_run_resource_holdings(doc, run_id=task_id)
    emit_testing_task({
        "event": "task_finished",
        "run_id": task_id,
        "task_id": task_id,
        "status": doc.get("status") or "cancelled",
        "app_id": str(doc.get("app_id") or ""),
    })
    return ok(result, msg="任务已取消")


@router.post("/tasks/{task_id}/retry-failed")
def retry_failed_cases(task_id: str, body: Optional[RetryFailedRequest] = None, _sess: dict = Depends(current_session)):
    body = body or RetryFailedRequest()
    sn = (body.sn or "").strip()
    if sn:
        from mino_nexus.runtime.run_context import is_web_slot

        if not is_web_slot(sn, ""):
            busy = run_store.busy_task_for_sn(sn)
            if busy:
                raise HTTPException(status_code=409, detail={"message": "device busy", "busy_task_id": busy, "sn": sn})
    try:
        result = cr.retry_failed(task_id, sn=sn)
    except cr.DeviceBusy as exc:
        raise HTTPException(
            status_code=409,
            detail={"message": "device busy", "busy_task_id": exc.busy_task_id, "sn": exc.sn},
        ) from exc
    except cr.WebSlotFull as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "web parallel full",
                "sn": exc.sn,
                "active": exc.active,
                "max": exc.limit,
            },
        ) from exc
    if not result.get("ok"):
        raise HTTPException(status_code=int(result.get("code") or 400), detail=result.get("reason") or "retry failed")
    snapshot = result.get("data") or {}
    return ok(
        {
            "task_id": snapshot.get("run_id") or snapshot.get("task_id") or "",
            "retried_from": task_id,
            "case_ids": result.get("case_ids") or [],
            "task": snapshot,
        },
        msg=f"已重跑 {len(result.get('case_ids') or [])} 条失败用例",
    )


@router.get("/baseline/{case_id}")
def get_baseline(case_id: str, _sess: dict = Depends(current_session)):
    return ok({"case_id": case_id, "baseline": None, "prompt_block": "", "note": "基线库尚未搬迁"})


@router.post("/baseline/promote")
def promote_baseline(body: PromoteBaselineRequest, _sess: dict = Depends(current_session)):
    doc = run_store.get(body.run_id)
    if doc is None:
        raise HTTPException(status_code=400, detail="找不到这条 run，无法提升为基线")
    return ok({"ok": True, "run_id": body.run_id, "blessed_by": body.blessed_by, "note": "基线库尚未搬迁，已记下请求"})
