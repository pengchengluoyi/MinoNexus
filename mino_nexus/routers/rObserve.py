"""会话、轨迹、Agent 步骤的只读接口。路径仍挂在 /case-runner 下。"""
from __future__ import annotations

from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict

from mino_nexus.core.http_util import ok
from mino_nexus.loop.observe import agent_stream
from mino_nexus.routers.deps import current_session
from mino_nexus.services import run_store

router = APIRouter(prefix="/case-runner", tags=["CaseRunner"])


class SessionForkRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    from_turn: int = 1
    sn: str = ""
    provider_id: str = ""


class SessionReplayRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    sn: str = ""
    up_to_turn: int | None = None


class SessionHarvestRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    app_id: str = ""
    limit: int = 20
    expect_status: str = ""
    max_steps: int | None = None
    menu_must_include: Optional[List[str]] = None



@router.get("/agent/runs")
def agent_runs(_sess: dict = Depends(current_session)):
    return ok({"runs": agent_stream.list_recent_runs()})


@router.get("/agent/steps/{run_id:path}")
def agent_steps(run_id: str, _sess: dict = Depends(current_session)):
    from mino_nexus.loop.observe.session_project import project_trajectory, resolve_session_id

    sid = resolve_session_id(run_id)
    data = project_trajectory(sid)
    if data is None:
        data = agent_stream.get_run_events(run_id)
    if data is None and sid != run_id:
        data = agent_stream.get_run_events(sid)
    if data is None:
        raise HTTPException(status_code=404, detail=f"agent run not found: {run_id}")
    return ok(data)


@router.get("/sessions")
def list_sessions(
    app_id: Optional[str] = Query(None),
    run_id: Optional[str] = Query(None),
    case_id: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    limit: int = Query(30, ge=1, le=100),
    offset: int = Query(0, ge=0),
    _sess: dict = Depends(current_session),
):
    from mino_nexus.services import session_store

    items, total = session_store.list_sessions(
        app_id=str(app_id or ""),
        run_id=str(run_id or ""),
        case_id=str(case_id or ""),
        status=str(status or ""),
        limit=limit,
        offset=offset,
    )
    return ok({"items": items, "total": total, "limit": limit, "offset": offset})


@router.get("/sessions/{session_id:path}/events")
def session_events(
    session_id: str,
    from_seq: int = 0,
    limit: int = 500,
    _sess: dict = Depends(current_session),
):
    from mino_nexus.loop.observe.session_log import get_meta, read_events

    meta = get_meta(session_id)
    lim = max(1, min(2000, int(limit or 500)))
    start = max(0, int(from_seq or 0))
    events = read_events(session_id, from_seq=start, limit=lim)
    if meta is None and not events:
        raise HTTPException(status_code=404, detail=f"session not found: {session_id}")
    total = int((meta or {}).get("event_count") or 0)
    last_seq = int(events[-1]["seq"]) if events else start
    has_more = bool(total and last_seq < total) or (not total and len(events) >= lim)
    next_from_seq = last_seq + 1 if has_more else None
    return ok({
        "session_id": session_id,
        "meta": meta,
        "events": events,
        "count": len(events),
        "total": total,
        "from_seq": start,
        "has_more": has_more,
        "next_from_seq": next_from_seq,
    })


@router.get("/sessions/{session_id:path}/turns")
def session_turns(session_id: str, _sess: dict = Depends(current_session)):
    from mino_nexus.loop.observe.session_project import project_turns

    data = project_turns(session_id)
    if data is None:
        raise HTTPException(status_code=404, detail=f"session not found: {session_id}")
    return ok(data)


@router.get("/sessions/{session_id:path}/eval")
def session_eval(session_id: str, _sess: dict = Depends(current_session)):
    from mino_nexus.loop.observe.session_harness import project_eval

    data = project_eval(session_id)
    if data is None:
        raise HTTPException(status_code=404, detail=f"session not found: {session_id}")
    return ok(data)


@router.get("/sessions/{session_id:path}/audit")
def session_audit(session_id: str, _sess: dict = Depends(current_session)):
    from mino_nexus.loop.observe.session_harness import project_audit

    data = project_audit(session_id)
    if data is None:
        raise HTTPException(status_code=404, detail=f"session not found: {session_id}")
    return ok(data)


@router.get("/sessions/{session_id:path}/replay-plan")
def session_replay_plan(
    session_id: str,
    up_to_turn: Optional[int] = Query(None),
    _sess: dict = Depends(current_session),
):
    from mino_nexus.loop.observe.session_harness import build_replay_plan

    data = build_replay_plan(session_id, up_to_turn=up_to_turn)
    if data is None:
        raise HTTPException(status_code=404, detail=f"session not found: {session_id}")
    return ok(data)


@router.get("/sessions/{session_id:path}/fork-plan")
def session_fork_plan(
    session_id: str,
    from_turn: int = Query(..., ge=1),
    provider_id: str = Query(""),
    _sess: dict = Depends(current_session),
):
    from mino_nexus.loop.observe.session_harness import build_fork_plan

    data = build_fork_plan(
        session_id,
        from_turn=from_turn,
        overrides={"provider_id": provider_id} if provider_id else None,
    )
    if data is None:
        raise HTTPException(status_code=404, detail=f"session or turn not found: {session_id}")
    return ok(data)


@router.post("/sessions/{session_id:path}/replay")
def session_replay_exec(
    session_id: str,
    body: SessionReplayRequest,
    _sess: dict = Depends(current_session),
):
    from mino_nexus.services.session_replay import replay_session

    out = replay_session(session_id, sn=str(body.sn or ""))
    if not out.get("ok"):
        raise HTTPException(status_code=int(out.get("code") or 400), detail=out.get("reason") or "replay failed")
    return ok(out)


@router.post("/sessions/{session_id:path}/fork")
def session_fork_exec(
    session_id: str,
    body: SessionForkRequest,
    _sess: dict = Depends(current_session),
):
    from mino_nexus.services.session_replay import fork_session

    out = fork_session(
        session_id,
        from_turn=int(body.from_turn or 1),
        sn=str(body.sn or ""),
        provider_id=str(body.provider_id or ""),
    )
    if not out.get("ok"):
        raise HTTPException(status_code=int(out.get("code") or 400), detail=out.get("reason") or "fork failed")
    return ok(out)


@router.post("/sessions/harvest")
def sessions_harvest(body: SessionHarvestRequest, _sess: dict = Depends(current_session)):
    from mino_nexus.loop.observe.session_harness import harvest_sessions

    expect: dict[str, Any] = {}
    if body.expect_status:
        expect["status"] = body.expect_status
    if body.max_steps is not None:
        expect["max_steps"] = body.max_steps
    if body.menu_must_include:
        expect["tools_must_include"] = list(body.menu_must_include)
    data = harvest_sessions(
        app_id=str(body.app_id or ""),
        limit=int(body.limit or 20),
        expect=expect or None,
    )
    return ok(data)


@router.get("/sessions/{session_id:path}/trajectory")
def session_trajectory(session_id: str, _sess: dict = Depends(current_session)):
    from mino_nexus.loop.observe.session_project import project_trajectory, resolve_session_id

    data = project_trajectory(resolve_session_id(session_id))
    if data is None:
        raise HTTPException(status_code=404, detail=f"session not found: {session_id}")
    return ok(data)


@router.get("/sessions/{session_id:path}/llm")
def session_llm_calls(session_id: str, _sess: dict = Depends(current_session)):
    from mino_nexus.loop.observe.session_log import get_meta
    from mino_nexus.loop.observe.session_project import project_llm_calls

    if get_meta(session_id) is None:
        raise HTTPException(status_code=404, detail=f"session not found: {session_id}")
    return ok({"session_id": session_id, "items": project_llm_calls(session_id)})


@router.get("/sessions/{session_id:path}/metrics")
def session_metrics(session_id: str, _sess: dict = Depends(current_session)):
    from mino_nexus.loop.observe.session_log import get_meta
    from mino_nexus.loop.observe.session_project import project_metrics

    if get_meta(session_id) is None:
        raise HTTPException(status_code=404, detail=f"session not found: {session_id}")
    return ok(project_metrics(session_id))


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
    if events is None:
        from mino_nexus.loop.observe.session_project import project_trajectory, resolve_session_id

        events = project_trajectory(resolve_session_id(run_id, case_id=case_id))
    elif not (events.get("events") or []) and str(events.get("source") or "") != "session_log":
        from mino_nexus.loop.observe.session_project import project_trajectory, resolve_session_id

        logged = project_trajectory(resolve_session_id(run_id, case_id=case_id))
        if logged and (logged.get("events") or []):
            events = logged
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
    ui_events = (events or {}).get("events") if isinstance(events, dict) else None
    if engine:
        from mino_nexus.loop.observe.session_project import merge_engine_thumbs

        payload["event_results"] = merge_engine_thumbs(engine, ui_events)
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


