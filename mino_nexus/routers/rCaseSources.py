from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from mino_nexus.core.http_util import ok
from mino_nexus.routers.deps import current_session, project_maintainer
from mino_nexus.services import case_sources

router = APIRouter(prefix="/project/{project_id}/case-sources", tags=["Case Sources"])


class SourceBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str
    configuration: dict[str, Any]
    expected_generation: int = Field(default=0, ge=0)
    confirm_rebind: bool = False


class SourceRefreshBody(BaseModel):
    expected_generation: int = Field(ge=1)
    table: list[list[Any]] | None = None


class SourceDetachBody(BaseModel):
    expected_generation: int = Field(ge=1)


class SourceDiscoveryBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    configuration: dict[str, Any]
    operation: str


def invoke(function, *args, **kwargs):
    try:
        return ok(function(*args, **kwargs))
    except ValueError as exc:
        code = str(exc)
        raise HTTPException(404 if code.endswith("missing") else 409 if "conflict" in code or "in_progress" in code else 400,
            {"reason_code": code}) from exc


@router.get("")
def list_sources(project_id: str, _session: dict = Depends(current_session)):
    return invoke(case_sources.listing, project_id)


@router.post("")
def create_source(project_id: str, body: SourceBody, _session: dict = Depends(project_maintainer)):
    return invoke(case_sources.save, project_id, body)


@router.post("/discover")
def discover_sources(project_id: str, body: SourceDiscoveryBody, _session: dict = Depends(project_maintainer)):
    return invoke(case_sources.discover, project_id, body.configuration, body.operation)


@router.put("/{source_id}")
def update_source(project_id: str, source_id: str, body: SourceBody, _session: dict = Depends(project_maintainer)):
    return invoke(case_sources.save, project_id, body, source_id)


@router.get("/{source_id}")
def source_history(project_id: str, source_id: str, limit: int = Query(50, ge=1, le=200),
                   offset: int = Query(0, ge=0), _session: dict = Depends(current_session)):
    return invoke(case_sources.history, project_id, source_id, limit=limit, offset=offset)


@router.post("/{source_id}/refresh")
def refresh_source(project_id: str, source_id: str, body: SourceRefreshBody, _session: dict = Depends(project_maintainer)):
    return invoke(case_sources.refresh, project_id, source_id, body.expected_generation, body.table)


@router.post("/{source_id}/cases/{case_id}/detach")
def detach_case(project_id: str, source_id: str, case_id: str, body: SourceDetachBody,
                _session: dict = Depends(project_maintainer)):
    return invoke(case_sources.detach, project_id, source_id, case_id, body.expected_generation)
