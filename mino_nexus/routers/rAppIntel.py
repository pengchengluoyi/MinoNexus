"""AppIntel 信息基座 HTTP API。设计稿 docs/9月12日_AppIntel信息基座.md §7。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from mino_nexus.core.http_util import ok
from mino_nexus.routers.deps import current_session
from mino_nexus.services import app_intel as intel

router = APIRouter(prefix="/apps", tags=["AppIntel"])


class ContextPackBody(BaseModel):
    intent: str = ""
    state_id: str = ""
    hierarchy_excerpt: str = ""
    policy: str = "wiki_first"
    budgets: dict[str, int] = Field(default_factory=dict)
    project_id: str = ""


class AskBody(BaseModel):
    question: str = ""
    project_id: str = ""
    kinds: str = "nav,knowledge,doc"
    limit: int = 8


class LinkBody(BaseModel):
    from_ref: str = ""
    to_ref: str = ""
    rel: str = ""
    meta: dict[str, Any] = Field(default_factory=dict)


class ProposalBody(BaseModel):
    kind: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    note: str = ""


class ProposalReviewBody(BaseModel):
    status: str = "approved"
    note: str = ""


@router.get("/{app_id}/intel/search")
def intel_search(
    app_id: str,
    q: str = "",
    kinds: str = "nav,knowledge,doc",
    state_id: str = "",
    project_id: str = "",
    limit: int = 20,
    _sess: dict = Depends(current_session),
):
    aid = str(app_id or "").strip()
    query = str(q or "").strip()
    if not aid:
        raise HTTPException(status_code=400, detail="app_id 不能为空")
    if not query:
        raise HTTPException(status_code=400, detail="q 不能为空")
    return ok({
        "items": intel.search(
            app_id=aid,
            q=query,
            kinds=kinds,
            state_id=state_id,
            project_id=project_id,
            limit=limit,
        ),
    })


@router.post("/{app_id}/intel/context-pack")
def intel_context_pack(
    app_id: str,
    body: ContextPackBody,
    _sess: dict = Depends(current_session),
):
    aid = str(app_id or "").strip()
    if not aid:
        raise HTTPException(status_code=400, detail="app_id 不能为空")
    pack = intel.context_pack_http(
        app_id=aid,
        project_id=body.project_id,
        intent=body.intent,
        state_id=body.state_id,
        hierarchy_excerpt=body.hierarchy_excerpt,
        policy=body.policy,
        budgets=body.budgets or None,
    )
    return ok(pack)


@router.get("/{app_id}/intel/route")
def intel_route(
    app_id: str,
    from_state: str = "",
    to_state: str = "",
    project_id: str = "",
    use_live: int = 1,
    _sess: dict = Depends(current_session),
):
    aid = str(app_id or "").strip()
    if not aid:
        raise HTTPException(status_code=400, detail="app_id 不能为空")
    if not str(from_state or "").strip() or not str(to_state or "").strip():
        raise HTTPException(status_code=400, detail="from_state / to_state 不能为空")
    return ok(intel.route_plan(
        app_id=aid,
        from_state=from_state,
        to_state=to_state,
        project_id=project_id,
        use_live=bool(use_live),
    ))


@router.post("/{app_id}/intel/ask")
def intel_ask(
    app_id: str,
    body: AskBody,
    _sess: dict = Depends(current_session),
):
    aid = str(app_id or "").strip()
    if not aid:
        raise HTTPException(status_code=400, detail="app_id 不能为空")
    if not str(body.question or "").strip():
        raise HTTPException(status_code=400, detail="question 不能为空")
    result = intel.ask(
        app_id=aid,
        question=body.question,
        project_id=body.project_id,
        kinds=body.kinds,
        limit=body.limit,
    )
    return ok(result)


@router.get("/{app_id}/intel/graph")
def intel_graph(
    app_id: str,
    center: str = "",
    depth: int = 1,
    project_id: str = "",
    _sess: dict = Depends(current_session),
):
    aid = str(app_id or "").strip()
    if not aid:
        raise HTTPException(status_code=400, detail="app_id 不能为空")
    if not str(center or "").strip():
        raise HTTPException(status_code=400, detail="center 不能为空")
    return ok(intel.graph(
        app_id=aid,
        center=center,
        depth=depth,
        project_id=project_id,
    ))


@router.get("/{app_id}/intel/gaps")
def intel_gaps(
    app_id: str,
    project_id: str = "",
    _sess: dict = Depends(current_session),
):
    aid = str(app_id or "").strip()
    if not aid:
        raise HTTPException(status_code=400, detail="app_id 不能为空")
    return ok(intel.gaps(app_id=aid, project_id=project_id))


@router.get("/{app_id}/intel/proposals")
def intel_list_proposals(
    app_id: str,
    status: str = "",
    limit: int = 50,
    _sess: dict = Depends(current_session),
):
    aid = str(app_id or "").strip()
    if not aid:
        raise HTTPException(status_code=400, detail="app_id 不能为空")
    return ok({"items": intel.list_proposals(app_id=aid, status=status, limit=limit)})


@router.post("/{app_id}/intel/proposals")
def intel_create_proposal(
    app_id: str,
    body: ProposalBody,
    _sess: dict = Depends(current_session),
):
    aid = str(app_id or "").strip()
    if not aid:
        raise HTTPException(status_code=400, detail="app_id 不能为空")
    if not str(body.kind or "").strip():
        raise HTTPException(status_code=400, detail="kind 不能为空")
    result = intel.create_proposal(
        app_id=aid,
        kind=body.kind,
        payload=body.payload,
        note=body.note,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=str(result.get("error") or "创建失败"))
    return ok(result)


@router.post("/{app_id}/intel/proposals/{proposal_id}/review")
def intel_review_proposal(
    app_id: str,
    proposal_id: str,
    body: ProposalReviewBody,
    _sess: dict = Depends(current_session),
):
    aid = str(app_id or "").strip()
    if not aid:
        raise HTTPException(status_code=400, detail="app_id 不能为空")
    result = intel.review_proposal(
        app_id=aid,
        proposal_id=proposal_id,
        status=body.status,
        note=body.note,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=str(result.get("error") or "审核失败"))
    return ok(result)


@router.get("/{app_id}/intel/links")
def intel_list_links(
    app_id: str,
    from_ref: str = "",
    to_ref: str = "",
    rel: str = "",
    _sess: dict = Depends(current_session),
):
    aid = str(app_id or "").strip()
    if not aid:
        raise HTTPException(status_code=400, detail="app_id 不能为空")
    return ok({
        "items": intel.list_links(
            app_id=aid,
            from_ref=from_ref,
            to_ref=to_ref,
            rel=rel,
        ),
    })


@router.post("/{app_id}/intel/links")
def intel_add_link(
    app_id: str,
    body: LinkBody,
    _sess: dict = Depends(current_session),
):
    aid = str(app_id or "").strip()
    if not aid:
        raise HTTPException(status_code=400, detail="app_id 不能为空")
    result = intel.add_link(
        app_id=aid,
        from_ref=body.from_ref,
        to_ref=body.to_ref,
        rel=body.rel,
        meta=body.meta,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=str(result.get("error") or "创建失败"))
    return ok(result)
