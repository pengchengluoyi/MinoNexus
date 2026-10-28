"""外部 agent 自行执行、Nexus 记账的接口。路径挂在 /case-runner/external 下。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict

from mino_nexus.api.deps import current_session
from mino_nexus.casework import external_run
from mino_nexus.core.http_util import ok
from mino_nexus.resources import project_store as ps

router = APIRouter(prefix="/case-runner/external", tags=["CaseRunner"])


class BeginBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    app_id: str
    case_id: str
    agent: str = ""
    client_request_id: str = ""
    sn: str = ""
    lease_minutes: int = 30


class ReportBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    case_id: str
    events: list[dict[str, Any]]


class FinishBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    case_id: str
    status: str
    summary: str = ""


class ScreenshotBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    case_id: str
    image_base64: str


def _guard(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc).strip("'")) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/begin")
def begin(body: BeginBody, _sess: dict = Depends(current_session)):
    try:
        app = ps.require_app(body.app_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="App not found") from exc
    return ok(_guard(external_run.begin, app, body.case_id, agent=body.agent,
                     client_request_id=body.client_request_id, sn=body.sn,
                     lease_minutes=body.lease_minutes))


@router.post("/{run_id}/events")
def report(run_id: str, body: ReportBody, _sess: dict = Depends(current_session)):
    return ok(_guard(external_run.report, run_id, body.case_id, body.events))


@router.post("/{run_id}/finish")
def finish(run_id: str, body: FinishBody, _sess: dict = Depends(current_session)):
    return ok(_guard(external_run.finish, run_id, body.case_id, status=body.status, summary=body.summary))


@router.post("/{run_id}/screenshot")
def screenshot(run_id: str, body: ScreenshotBody, _sess: dict = Depends(current_session)):
    return ok(_guard(external_run.save_screenshot, run_id, body.case_id, body.image_base64))
