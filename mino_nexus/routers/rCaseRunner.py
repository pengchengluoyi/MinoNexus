"""CaseRunner：设备选择器 + 任务/运行 + Agent 执行循环。"""
from __future__ import annotations

from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict

from mino_nexus.services import project_store as ps
from mino_nexus.services import run_store
from mino_nexus.core.http_util import ok
from mino_nexus.loop import agent_stream, case_runner as cr
from mino_nexus.loop.web_env import release_web_for_run
from mino_nexus.routers.deps import current_session
from mino_nexus.services.ui_devices import ui_devices

router = APIRouter(prefix="/case-runner", tags=["CaseRunner"])


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


class PromoteBaselineRequest(BaseModel):
    run_id: str
    blessed_by: str = "manual"
    notes: str = ""


class RetryFailedRequest(BaseModel):
    sn: str = ""
    execution_mode: str = "agent"


@router.get("/devices")
def list_devices(only_online: bool = Query(True), _sess: dict = Depends(current_session)):
    items = ui_devices()
    if only_online:
        items = [d for d in items if d.get("status") == "online"]
    for row in items:
        busy = run_store.busy_task_for_sn(str(row.get("sn") or ""))
        row["busy_task_id"] = busy
    return ok({"count": len(items), "items": items})


@router.post("/run")
def run_cases(body: RunRequest, _sess: dict = Depends(current_session)):
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
        )
        return ok(snapshot, msg="AI-led 回归任务已启动")
    except cr.DeviceBusy as exc:
        raise HTTPException(
            status_code=409,
            detail={"message": "device busy", "busy_task_id": exc.busy_task_id, "sn": exc.sn},
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


@router.post("/tasks/{task_id}/cancel")
def cancel_task(task_id: str, _sess: dict = Depends(current_session)):
    result = run_store.request_cancel(task_id)
    if not result.get("ok"):
        raise HTTPException(status_code=int(result.get("code") or 404), detail=result.get("reason") or "cancel failed")
    if result.get("already"):
        return ok(result, msg="任务已结束")
    doc = run_store.get(task_id) or {}
    sns = list(doc.get("sns") or [])
    head = str(doc.get("sn") or "").strip()
    if head and head not in sns:
        sns = [head, *sns]
    release_web_for_run(
        task_id,
        sns=sns,
        platforms_by_sn=doc.get("platforms_by_sn") if isinstance(doc.get("platforms_by_sn"), dict) else {},
    )
    return ok(result, msg="已请求取消，当前步骤结束后停止")


@router.post("/tasks/{task_id}/retry-failed")
def retry_failed_cases(task_id: str, body: Optional[RetryFailedRequest] = None, _sess: dict = Depends(current_session)):
    body = body or RetryFailedRequest()
    sn = (body.sn or "").strip()
    if sn:
        busy = run_store.busy_task_for_sn(sn)
        if busy:
            raise HTTPException(status_code=409, detail={"message": "device busy", "busy_task_id": busy, "sn": sn})
    result = cr.retry_failed(task_id, sn=sn)
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


@router.get("/agent/runs")
def agent_runs(_sess: dict = Depends(current_session)):
    return ok({"runs": agent_stream.list_recent_runs()})


@router.get("/agent/steps/{run_id:path}")
def agent_steps(run_id: str, _sess: dict = Depends(current_session)):
    data = agent_stream.get_run_events(run_id)
    if data is None:
        raise HTTPException(status_code=404, detail=f"agent run not found: {run_id}")
    return ok(data)


@router.get("/traces")
def traces(
    case_id: Optional[str] = None,
    device_signature: Optional[str] = None,
    app_id: Optional[str] = None,
    only_pass: bool = False,
    limit: int = 20,
    _sess: dict = Depends(current_session),
):
    rows = run_store.list_runs(limit=limit, app_id=str(app_id or ""))
    items = []
    for row in rows:
        for case in row.get("cases") or []:
            if not isinstance(case, dict):
                continue
            if case_id and str(case.get("case_id")) != str(case_id):
                continue
            if only_pass and case.get("status") != "pass":
                continue
            items.append({
                "run_id": case.get("report_run_id") or row.get("run_id"),
                "batch_id": row.get("run_id"),
                "app_id": row.get("app_id"),
                "case_id": case.get("case_id"),
                "status": case.get("status"),
                "sn": case.get("sn") or row.get("sn"),
                "summary": case.get("summary") or "",
                "started_at": row.get("started_at"),
            })
            if len(items) >= limit:
                break
        if len(items) >= limit:
            break
    return ok({"count": len(items), "items": items})


@router.get("/traces/{run_id:path}")
def get_trace_detail(run_id: str, _sess: dict = Depends(current_session)):
    batch, _, case_id = str(run_id or "").partition("::")
    case_id = case_id.split("::", 1)[0]
    doc = run_store.get(run_id)
    if doc is None:
        doc = run_store.get(batch)
    events = agent_stream.get_run_events(run_id)
    if doc is None:
        if events is None:
            raise HTTPException(status_code=404, detail=f"trace not found: {run_id}")
        return ok(events)
    payload = run_store.to_task_json(doc)
    case = None
    if case_id:
        case = next(
            (c for c in (payload.get("cases") or []) if str(c.get("case_id") or "") == case_id),
            None,
        )
    engine = (case or {}).get("engine_steps") or []
    if engine:
        payload["event_results"] = engine
    if case:
        payload["case_status"] = str(case.get("status") or "")
    if events:
        payload["events"] = events.get("events") or []
        payload["goal"] = events.get("goal") or payload.get("goal") or ""
        payload["agent_finished"] = bool(events.get("finished"))
        skill_id = events.get("skill_id") or (case or {}).get("skill_id")
        view_id = events.get("view_id") or (case or {}).get("view_id")
        slots = events.get("slots") if isinstance(events.get("slots"), dict) else None
        if not slots and isinstance((case or {}).get("slots"), dict):
            slots = case.get("slots")
        if skill_id:
            payload["skill_id"] = skill_id
        if view_id:
            payload["view_id"] = view_id
        if slots:
            payload["slots"] = slots
        ov = str(events.get("overall") or "").strip()
        if ov in {
            "pass", "fail", "done", "blocked", "declined", "skipped",
            "cancelled", "untestable", "unverifiable", "unexecutable",
        }:
            payload["overall_status"] = ov
        else:
            if ov:
                payload["summary"] = ov
            payload["overall_status"] = str((case or {}).get("status") or "")
    elif case:
        payload["overall_status"] = str(case.get("status") or "")
        if case.get("skill_id"):
            payload["skill_id"] = case.get("skill_id")
        if case.get("view_id"):
            payload["view_id"] = case.get("view_id")
        if isinstance(case.get("slots"), dict):
            payload["slots"] = case.get("slots")
    return ok(payload)


@router.get("/baseline/{case_id}")
def get_baseline(case_id: str, _sess: dict = Depends(current_session)):
    return ok({"case_id": case_id, "baseline": None, "prompt_block": "", "note": "基线库尚未搬迁"})


@router.post("/baseline/promote")
def promote_baseline(body: PromoteBaselineRequest, _sess: dict = Depends(current_session)):
    doc = run_store.get(body.run_id)
    if doc is None:
        raise HTTPException(status_code=400, detail="找不到这条 run，无法提升为基线")
    return ok({"ok": True, "run_id": body.run_id, "blessed_by": body.blessed_by, "note": "基线库尚未搬迁，已记下请求"})
