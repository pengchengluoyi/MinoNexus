"""应用逻辑 MCP 安全读写路由：页面/控件/入口/缺口读取，入口重抽写。"""
from __future__ import annotations

from typing import Annotated, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from mino_nexus.api.deps import current_session, project_maintainer
from mino_nexus.casework import application_logic_view as view
from mino_nexus.core.http_util import ok

router = APIRouter(prefix="/project/{project_id}", tags=["ApplicationLogic"])


class LogicEntryExtractBody(BaseModel):
    platform: str = ""
    case_ids: list[str] = Field(default_factory=list)
    include_cases: bool = True
    include_capture: bool = True


ReviewText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]


class ReviewExampleBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: ReviewText
    seq: int = Field(ge=0)
    polarity: Literal["positive", "negative"]


class PageReviewProposalBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: ReviewText
    version: ReviewText = "v1"
    expected_revision: int = Field(ge=0)
    platform: Literal["web", "android", "ios", "pc"]
    build: ReviewText
    reason: ReviewText
    aliases: list[ReviewText] = Field(default_factory=list, max_length=30)
    positive_terms: list[ReviewText] = Field(min_length=1, max_length=30)
    negative_terms: list[ReviewText] = Field(min_length=1, max_length=30)
    examples: list[ReviewExampleBody] = Field(min_length=2, max_length=30)


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, KeyError):
        return HTTPException(status_code=404, detail={"reason_code": "not_found", "message": "项目或应用不存在"})
    if str(exc) == "app_project_mismatch":
        return HTTPException(status_code=422, detail={"reason_code": "app_project_mismatch", "message": "应用不属于该项目"})
    return HTTPException(status_code=422, detail={"reason_code": "invalid_request", "message": str(exc)})


@router.get("/apps/{app_id}/logic/overview")
def logic_overview(
    project_id: str,
    app_id: str,
    _sess: dict = Depends(current_session),
):
    try:
        return ok(view.overview(project_id, app_id))
    except Exception as exc:  # noqa: BLE001 - 统一映射为 HTTP 语义
        raise _error(exc) from exc


@router.get("/apps/{app_id}/logic/pages")
def logic_pages(
    project_id: str,
    app_id: str,
    status: str = "",
    q: str = "",
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _sess: dict = Depends(current_session),
):
    try:
        return ok(view.list_pages(
            project_id, app_id, status=status, q=q, limit=limit, offset=offset,
        ))
    except Exception as exc:  # noqa: BLE001
        raise _error(exc) from exc


@router.get("/apps/{app_id}/logic/pages/{state_id}")
def logic_page_detail(
    project_id: str,
    app_id: str,
    state_id: str,
    include: Optional[list[str]] = Query(default=None),
    controls_limit: int = Query(default=30, ge=1, le=100),
    refs_limit: int = Query(default=20, ge=1, le=50),
    _sess: dict = Depends(current_session),
):
    try:
        data = view.page_detail(
            project_id, app_id, state_id,
            include=include, controls_limit=controls_limit, refs_limit=refs_limit,
        )
    except Exception as exc:  # noqa: BLE001
        raise _error(exc) from exc
    if data is None:
        raise HTTPException(status_code=404, detail={"reason_code": "page_not_found", "message": "页面状态不存在"})
    return ok(data)


@router.get("/logic/controls")
def logic_controls(
    project_id: str,
    app_id: str = "",
    page_state_id: str = "",
    semantic_role: str = "",
    sensitive: str = "",
    q: str = "",
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _sess: dict = Depends(current_session),
):
    try:
        return ok(view.list_controls_view(
            project_id, app_id=app_id, page_state_id=page_state_id,
            semantic_role=semantic_role, sensitive=sensitive, q=q,
            limit=limit, offset=offset,
        ))
    except Exception as exc:  # noqa: BLE001
        raise _error(exc) from exc


@router.get("/logic/controls/{control_id}")
def logic_control_detail(
    project_id: str,
    control_id: str,
    _sess: dict = Depends(current_session),
):
    try:
        data = view.control_detail(project_id, control_id)
    except Exception as exc:  # noqa: BLE001
        raise _error(exc) from exc
    if data is None:
        raise HTTPException(status_code=404, detail={"reason_code": "control_not_found", "message": "控件不存在"})
    return ok(data)


@router.get("/logic/flow-blocks")
def logic_flow_blocks(
    project_id: str,
    channel: str = "",
    _sess: dict = Depends(current_session),
):
    try:
        return ok(view.list_flow_blocks(project_id, channel=channel))
    except Exception as exc:  # noqa: BLE001
        raise _error(exc) from exc


@router.get("/logic/flow-blocks/{block_id}")
def logic_flow_block_detail(
    project_id: str,
    block_id: str,
    _sess: dict = Depends(current_session),
):
    try:
        data = view.flow_block_detail(project_id, block_id)
    except Exception as exc:  # noqa: BLE001
        raise _error(exc) from exc
    if data is None:
        raise HTTPException(status_code=404, detail={"reason_code": "flow_block_not_found", "message": "流程块不存在"})
    return ok(data)


REVIEW_CONFLICTS = {
    "page_revision_conflict", "review_request_conflict",
    "page_changed_reproposal_required",
}


@router.post("/apps/{app_id}/logic/pages/{state_id}/review-proposals", dependencies=[Depends(project_maintainer)])
def logic_page_review_propose(
    project_id: str,
    app_id: str,
    state_id: str,
    body: PageReviewProposalBody,
    _sess: dict = Depends(current_session),
):
    try:
        return ok(view.propose_review(project_id, app_id, state_id, body))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail={"reason_code": "page_not_found", "message": "页面状态不存在"}) from exc
    except ValueError as exc:
        reason = str(exc)
        code = 409 if reason in REVIEW_CONFLICTS else 422
        raise HTTPException(status_code=code, detail={"reason_code": reason, "message": "页面评审提案被拒绝"}) from exc


@router.post("/apps/{app_id}/logic/entries/extract", dependencies=[Depends(project_maintainer)])
def logic_entry_extract(
    project_id: str,
    app_id: str,
    body: LogicEntryExtractBody,
    _sess: dict = Depends(current_session),
):
    try:
        return ok(view.extract_entries(
            project_id, app_id,
            platform=body.platform,
            case_ids=body.case_ids,
            include_cases=body.include_cases,
            include_capture=body.include_capture,
            actor=str((_sess or {}).get("user_id") or ""),
        ))
    except Exception as exc:  # noqa: BLE001
        raise _error(exc) from exc


@router.get("/apps/{app_id}/logic/entries")
def logic_entries(
    project_id: str,
    app_id: str,
    kind: str = "entry",
    target: str = "",
    from_state_id: str = "",
    status: str = "",
    platform: str = "",
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _sess: dict = Depends(current_session),
):
    try:
        return ok(view.list_entries_view(
            project_id, app_id, kind=kind, target=target, from_state_id=from_state_id,
            status=status, platform=platform, limit=limit, offset=offset,
        ))
    except Exception as exc:  # noqa: BLE001
        raise _error(exc) from exc
