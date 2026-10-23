"""工作层 HTTP API。

路由只做鉴权、参数校验和调用 work_store；Runner/Handler 不在请求线程里执行。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from mino_nexus.api.deps import project_maintainer
from mino_nexus.assistant.work import work_store
from mino_nexus.core.http_util import ok

router = APIRouter(prefix="/project/{project_id}/work", tags=["Work"])


class WorkCreateBody(BaseModel):
    goal: str
    title: str = ""
    topic_id: str = ""
    source_message_id: str = ""
    create_key: str = ""
    acceptance: list[Any] = Field(default_factory=list)
    budget: dict[str, Any] = Field(default_factory=dict)
    instructions: list[str] = Field(default_factory=list)
    policy: dict[str, Any] = Field(default_factory=dict)


class PlanBody(BaseModel):
    plan: dict[str, Any]
    acceptance: list[Any] = Field(default_factory=list)
    expected_revision: int


class ApprovalBody(BaseModel):
    decision: str
    option: str = ""
    comment: str = ""


class InstructionBody(BaseModel):
    text: str


class ResolveBody(BaseModel):
    decision: str
    note: str = ""


def _uid(sess: dict[str, Any]) -> str:
    uid = str((sess or {}).get("user_id") or "")
    if not uid:
        raise HTTPException(status_code=401, detail={"reason_code": "login_required",
                                                     "message": "请先登录"})
    return uid


def _owned(work_id: str, project_id: str, sess: dict[str, Any]) -> dict[str, Any]:
    data = work_store.detail(work_id)
    if not data or str(data.get("project_id") or "") != project_id:
        raise HTTPException(status_code=404, detail={"reason_code": "work_not_found",
                                                     "message": "工作项不存在"})
    if str(data.get("owner_user_id") or "") != _uid(sess):
        raise HTTPException(status_code=404, detail={"reason_code": "work_not_found",
                                                     "message": "工作项不存在"})
    return data


def _fail(exc: Exception) -> HTTPException:
    if isinstance(exc, KeyError):
        return HTTPException(status_code=404, detail={"reason_code": "work_not_found",
                                                      "message": "工作项不存在"})
    return HTTPException(status_code=409, detail={"reason_code": str(exc),
                                                  "message": str(exc)})


@router.post("")
def create_work(project_id: str, body: WorkCreateBody,
                sess: dict = Depends(project_maintainer)) -> dict[str, Any]:
    uid = _uid(sess)
    try:
        data, created = work_store.create_work(
            project_id=project_id,
            owner_user_id=uid,
            goal=body.goal,
            title=body.title,
            topic_id=body.topic_id,
            source_message_id=body.source_message_id,
            create_key=body.create_key,
            acceptance=body.acceptance,
            budget=body.budget,
            instructions=body.instructions,
            policy=body.policy,
        )
    except ValueError as exc:
        raise _fail(exc) from exc
    return ok(data, created=created)


@router.get("")
def list_work(project_id: str, topic_id: str = "", status: str = "",
              limit: int = Query(50, ge=1, le=200),
              offset: int = Query(0, ge=0),
              sess: dict = Depends(project_maintainer)) -> dict[str, Any]:
    data = work_store.list_work(owner_user_id=_uid(sess), project_id=project_id,
                               topic_id=topic_id, status=status, limit=limit, offset=offset)
    return ok(data)


@router.get("/{work_id}")
def get_work(project_id: str, work_id: str,
             sess: dict = Depends(project_maintainer)) -> dict[str, Any]:
    return ok(_owned(work_id, project_id, sess))


@router.get("/{work_id}/events")
def get_events(project_id: str, work_id: str, limit: int = Query(200, ge=1, le=500),
               sess: dict = Depends(project_maintainer)) -> dict[str, Any]:
    _owned(work_id, project_id, sess)
    data = work_store.detail(work_id, event_limit=limit) or {}
    return ok({"items": data.get("events") or [], "work_id": work_id})


@router.post("/{work_id}/pause")
def pause_work(project_id: str, work_id: str,
               sess: dict = Depends(project_maintainer)) -> dict[str, Any]:
    _owned(work_id, project_id, sess)
    return ok(work_store.pause_work(work_id, actor=_uid(sess)))


@router.post("/{work_id}/resume")
def resume_work(project_id: str, work_id: str,
                sess: dict = Depends(project_maintainer)) -> dict[str, Any]:
    _owned(work_id, project_id, sess)
    try:
        return ok(work_store.resume_work(work_id, actor=_uid(sess)))
    except ValueError as exc:
        raise _fail(exc) from exc


@router.post("/{work_id}/cancel")
def cancel_work(project_id: str, work_id: str,
                sess: dict = Depends(project_maintainer)) -> dict[str, Any]:
    _owned(work_id, project_id, sess)
    return ok(work_store.cancel_work(work_id, actor=_uid(sess)))


@router.post("/{work_id}/instruct")
def instruct_work(project_id: str, work_id: str, body: InstructionBody,
                  sess: dict = Depends(project_maintainer)) -> dict[str, Any]:
    _owned(work_id, project_id, sess)
    try:
        return ok(work_store.add_instruction(work_id, body.text, actor=_uid(sess)))
    except ValueError as exc:
        raise _fail(exc) from exc


@router.patch("/{work_id}/plan")
def patch_plan(project_id: str, work_id: str, body: PlanBody,
               sess: dict = Depends(project_maintainer)) -> dict[str, Any]:
    _owned(work_id, project_id, sess)
    try:
        return ok(work_store.patch_plan(work_id, body.plan, body.acceptance,
                                        expected_revision=body.expected_revision,
                                        actor=_uid(sess)))
    except ValueError as exc:
        raise _fail(exc) from exc


@router.post("/{work_id}/approvals/{approval_id}")
def decide_approval(project_id: str, work_id: str, approval_id: str,
                    body: ApprovalBody,
                    sess: dict = Depends(project_maintainer)) -> dict[str, Any]:
    _owned(work_id, project_id, sess)
    try:
        data = work_store.decide_approval(work_id=work_id, approval_id=approval_id,
                                         decision=body.decision, decided_by=_uid(sess),
                                         comment=body.comment, option=body.option)
    except (KeyError, ValueError) as exc:
        raise _fail(exc) from exc
    return ok(data)


@router.post("/{work_id}/steps/{step_id}/retry")
def retry_step(project_id: str, work_id: str, step_id: str,
               sess: dict = Depends(project_maintainer)) -> dict[str, Any]:
    _owned(work_id, project_id, sess)
    try:
        return ok(work_store.retry_step(work_id, step_id, actor=_uid(sess)))
    except ValueError as exc:
        raise _fail(exc) from exc


@router.post("/{work_id}/steps/{step_id}/skip")
def skip_step(project_id: str, work_id: str, step_id: str,
              sess: dict = Depends(project_maintainer)) -> dict[str, Any]:
    _owned(work_id, project_id, sess)
    try:
        return ok(work_store.skip_step(work_id, step_id, actor=_uid(sess)))
    except ValueError as exc:
        raise _fail(exc) from exc


@router.post("/{work_id}/steps/{step_id}/takeover")
def takeover_step(project_id: str, work_id: str, step_id: str,
                  sess: dict = Depends(project_maintainer)) -> dict[str, Any]:
    _owned(work_id, project_id, sess)
    try:
        return ok(work_store.takeover_step(work_id, step_id, actor=_uid(sess)))
    except ValueError as exc:
        raise _fail(exc) from exc


@router.post("/{work_id}/steps/{step_id}/resolve")
def resolve_unknown(project_id: str, work_id: str, step_id: str,
                    body: ResolveBody,
                    sess: dict = Depends(project_maintainer)) -> dict[str, Any]:
    _owned(work_id, project_id, sess)
    try:
        return ok(work_store.resolve_unknown(work_id, step_id, decision=body.decision,
                                             actor=_uid(sess), note=body.note))
    except ValueError as exc:
        raise _fail(exc) from exc
