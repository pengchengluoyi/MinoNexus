"""Mino Bots V7：项目协作 API。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from mino_nexus.core.http_util import ok
from mino_nexus.routers.deps import current_session, project_maintainer
from mino_nexus.services import mino_bots as mb
from mino_nexus.services import collab_extended as ce
from mino_nexus.services import collab_policy as cp
from mino_nexus.services.collab_openhands import openhands_status, save_openhands_api_url

from fastapi import Request
from mino_nexus.routers.rCollabWorkspace import router as workspace_router

def _legacy_read_only(request: Request):
    if request.method not in ('GET', 'HEAD', 'OPTIONS'):
        raise HTTPException(410, '工作区已升级，请刷新 Workbench 使用工作区入口')

router = APIRouter()
router.include_router(workspace_router)
legacy_router = APIRouter(prefix="/project/{project_id}/collab", tags=["MinoBots"], dependencies=[Depends(_legacy_read_only)])


class BotPatchBody(BaseModel):
    name: str | None = None
    app_ids: list[str] | None = None
    platforms: list[str] | None = None
    policy_override: dict[str, Any] | None = None


class BotCreateBody(BaseModel):
    name: str = ""
    template_id: str = "collab-default"
    app_ids: list[str] = Field(default_factory=list)
    platforms: list[str] = Field(default_factory=list)
    policy_override: dict[str, Any] = Field(default_factory=dict)


class TopicCreateBody(BaseModel):
    title: str
    goal: str = ""
    app_id: str = ""
    bot_ids: list[str] = Field(default_factory=list)


class TopicFromCaseBody(BaseModel):
    app_id: str
    case_id: str


class MessageBody(BaseModel):
    body: str
    acting_bot_id: str = ""


class TopicPatchBody(BaseModel):
    title: str | None = None
    goal: str | None = None
    status: str | None = None


class DecomposeBody(BaseModel):
    goal: str = ""
    input_version: str = ""
    items: list[dict[str, Any]] = Field(default_factory=list)
    default_action_kind: str = "llm_job"
    default_job: str = "analyze_req"
    use_llm: bool = True
    bump_version: bool = False
    include_case_run: bool = True


class PipelineRunBody(BaseModel):
    goal: str = ""
    bump_version: bool = True
    engine: str = "langgraph"


class AdvanceTopicBody(BaseModel):
    goal: str = ""
    message: str = ""
    sync_memory: bool = False
    engine: str = "langgraph"


class TransferBody(BaseModel):
    holder_user_id: str = ""
    assignee_kind: str = ""
    assignee_id: str = ""


class TriggerBody(BaseModel):
    force_retry: bool = False


class PolicyPutBody(BaseModel):
    app_id: str = ""
    auto_apply_drafts: bool | None = None
    auto_compile: bool | None = None
    auto_execute: bool | None = None
    execute_env_profiles: list[str] | None = None
    execute_surfaces: list[str] | None = None
    auto_publish: bool | None = None
    auto_retry_toolchain: int | None = None
    max_parallel_runs: int | None = None
    daily_llm_budget_tokens: int | None = None
    notify: list[str] | None = None
    watch_manual_runs: bool | None = None


class RedoBody(BaseModel):
    feedback: str = ""


class AnswerBody(BaseModel):
    fact: str


class MarketplaceInstallBody(BaseModel):
    market_id: str
    name: str = ""


class OpenHandsConfigBody(BaseModel):
    api_url: str = ""


class ImBindBody(BaseModel):
    channel: str
    chat_id: str
    node_id: str = ""
    label: str = ""


class ApplyDraftsBody(BaseModel):
    confirm: bool = False
    replace: bool = False


class ApprovalDecideBody(BaseModel):
    decision: str
    comment: str = ""


@legacy_router.get("/policy")
def get_collab_policy(project_id: str, app_id: str = "", sess: dict = Depends(project_maintainer)):
    return ok(cp.get_policy(project_id, app_id=app_id))


@legacy_router.put("/policy")
def put_collab_policy(project_id: str, body: PolicyPutBody, _sess: dict = Depends(project_maintainer)):
    payload = body.model_dump(exclude_none=True)
    return ok(cp.put_policy(project_id, payload, app_id=body.app_id))


@legacy_router.get("/templates")
def collab_templates(_sess: dict = Depends(current_session)):
    return ok({"items": mb.list_templates()})


@legacy_router.get("/marketplace")
def collab_marketplace(project_id: str, sess: dict = Depends(project_maintainer)):
    return ok({"items": mb.list_marketplace(project_id, sess)})


@legacy_router.post("/bots/install")
def install_marketplace_bot(project_id: str, body: MarketplaceInstallBody, sess: dict = Depends(project_maintainer)):
    try:
        return ok(mb.install_marketplace_bot(project_id, sess, market_id=body.market_id, name=body.name))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@legacy_router.get("/bots")
def list_bots(project_id: str, sess: dict = Depends(project_maintainer)):
    return ok({"items": mb.list_bots(project_id, sess)})


@legacy_router.patch("/bots/{bot_id}")
def patch_bot(project_id: str, bot_id: str, body: BotPatchBody, sess: dict = Depends(project_maintainer)):
    try:
        return ok(
            mb.patch_bot(
                project_id,
                bot_id,
                sess,
                name=body.name,
                app_ids=body.app_ids,
                platforms=body.platforms,
                policy_override=body.policy_override,
            )
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Bot 不存在") from exc


@legacy_router.post("/bots")
def create_bot(project_id: str, body: BotCreateBody, sess: dict = Depends(project_maintainer)):
    try:
        row = mb.create_bot(
            project_id,
            sess,
            name=body.name,
            template_id=body.template_id,
            app_ids=body.app_ids,
            platforms=body.platforms,
            policy_override=body.policy_override or None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ok(row)


@legacy_router.post("/bots/ensure-defaults")
def ensure_default_bots(project_id: str, sess: dict = Depends(project_maintainer)):
    return ok({"items": mb.ensure_default_bots(project_id, sess)})


@legacy_router.get("/topics")
def list_topics(project_id: str, sess: dict = Depends(project_maintainer)):
    return ok({"items": mb.list_topics(project_id, sess)})


@legacy_router.post("/topics/from-case")
def topic_from_case(project_id: str, body: TopicFromCaseBody, sess: dict = Depends(project_maintainer)):
    try:
        return ok(mb.open_topic_for_case(project_id, sess, app_id=body.app_id, case_id=body.case_id))
    except KeyError as exc:
        if str(exc) == "case_not_found":
            raise HTTPException(status_code=404, detail="用例不存在") from exc
        raise HTTPException(status_code=404, detail="议题不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@legacy_router.post("/topics")
def create_topic(project_id: str, body: TopicCreateBody, sess: dict = Depends(project_maintainer)):
    row = mb.create_topic(
        project_id,
        sess,
        title=body.title,
        goal=body.goal,
        app_id=body.app_id,
        bot_ids=body.bot_ids,
    )
    return ok(row)


@legacy_router.get("/topics/{topic_id}")
def get_topic(project_id: str, topic_id: str, sess: dict = Depends(project_maintainer)):
    try:
        return ok(mb.get_topic(project_id, topic_id, sess))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="议题不存在") from exc


@legacy_router.get("/topics/{topic_id}/agent-canvas")
def get_agent_canvas(project_id: str, topic_id: str, sess: dict = Depends(project_maintainer)):
    try:
        return ok(mb.get_agent_canvas(project_id, topic_id, sess))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="议题不存在") from exc


@legacy_router.get("/openhands/status")
def collab_openhands_status(_sess: dict = Depends(project_maintainer)):
    return ok(openhands_status())


@legacy_router.put("/openhands/config")
def collab_openhands_config(body: OpenHandsConfigBody, _sess: dict = Depends(project_maintainer)):
    try:
        return ok(save_openhands_api_url(body.api_url), msg="OpenHands 地址已保存")
    except ValueError as exc:
        if str(exc) == "openhands_env_locked":
            raise HTTPException(
                status_code=409,
                detail="已由服务器环境变量 OPENHANDS_API_URL 固定，无法在界面修改",
            ) from exc
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@legacy_router.post("/topics/{topic_id}/pause")
def pause_topic(project_id: str, topic_id: str, sess: dict = Depends(project_maintainer)):
    try:
        return ok(mb.pause_topic(project_id, topic_id, sess))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="议题不存在") from exc


@legacy_router.post("/topics/{topic_id}/resume")
def resume_topic(project_id: str, topic_id: str, sess: dict = Depends(project_maintainer)):
    try:
        return ok(mb.resume_topic(project_id, topic_id, sess))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="议题不存在") from exc


@legacy_router.post("/topics/{topic_id}/close")
def close_topic(project_id: str, topic_id: str, sess: dict = Depends(project_maintainer)):
    try:
        return ok(mb.close_topic(project_id, topic_id, sess))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="议题不存在") from exc


class TopicDeleteBody(BaseModel):
    force: bool = False


@legacy_router.delete("/topics/{topic_id}")
def delete_topic(project_id: str, topic_id: str, force: bool = False, sess: dict = Depends(project_maintainer)):
    try:
        return ok(mb.delete_topic(project_id, topic_id, sess, force=force))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="议题不存在") from exc
    except ValueError as exc:
        if str(exc) == "topic_has_running_work":
            raise HTTPException(status_code=409, detail="议题有工作项正在执行，请先暂停，或强制删除") from exc
        raise


@legacy_router.post("/topics/{topic_id}/delete")
def delete_topic_post(
    project_id: str,
    topic_id: str,
    body: TopicDeleteBody,
    sess: dict = Depends(project_maintainer),
):
    """部分部署环境对 DELETE 返回 405，Workbench 走 POST。"""
    try:
        return ok(mb.delete_topic(project_id, topic_id, sess, force=body.force))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="议题不存在") from exc
    except ValueError as exc:
        if str(exc) == "topic_has_running_work":
            raise HTTPException(status_code=409, detail="议题有工作项正在执行，请先暂停，或强制删除") from exc
        raise


@legacy_router.get("/topics/{topic_id}/activities")
def topic_activities(project_id: str, topic_id: str, sess: dict = Depends(project_maintainer)):
    from mino_nexus.services.collab_steward import list_activities

    try:
        return ok({"items": list_activities(project_id, topic_id, sess)})
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="议题不存在") from exc


@legacy_router.post("/topics/{topic_id}/steward/start")
def steward_start(project_id: str, topic_id: str, sess: dict = Depends(project_maintainer)):
    from mino_nexus.services.collab_steward import start_steward_flow

    try:
        return ok(start_steward_flow(project_id, topic_id, sess))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="议题不存在") from exc


@legacy_router.get("/topics/{topic_id}/digest")
def topic_digest(project_id: str, topic_id: str, sess: dict = Depends(project_maintainer)):
    try:
        return ok(mb.get_topic_digest(project_id, topic_id, sess))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="议题不存在") from exc


@legacy_router.post("/work-items/{work_item_id}/redo")
def redo_work_item(project_id: str, work_item_id: str, body: RedoBody, sess: dict = Depends(project_maintainer)):
    try:
        return ok(mb.redo_work_item(project_id, work_item_id, sess, feedback=body.feedback))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="工作项不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@legacy_router.post("/work-items/{work_item_id}/answer")
def answer_work_item(project_id: str, work_item_id: str, body: AnswerBody, sess: dict = Depends(project_maintainer)):
    try:
        return ok(mb.answer_work_item(project_id, work_item_id, sess, fact=body.fact))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="工作项不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@legacy_router.patch("/topics/{topic_id}")
def patch_topic(project_id: str, topic_id: str, body: TopicPatchBody, sess: dict = Depends(project_maintainer)):
    try:
        return ok(
            mb.update_topic(
                project_id,
                topic_id,
                sess,
                title=body.title,
                goal=body.goal,
                status=body.status,
            )
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="议题不存在") from exc


@legacy_router.get("/topics/{topic_id}/messages")
def list_messages(
    project_id: str,
    topic_id: str,
    limit: int = 100,
    sess: dict = Depends(project_maintainer),
):
    try:
        return ok({"items": mb.list_messages(project_id, topic_id, sess, limit=limit)})
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="议题不存在") from exc


@legacy_router.post("/topics/{topic_id}/messages")
def post_message(project_id: str, topic_id: str, body: MessageBody, sess: dict = Depends(project_maintainer)):
    try:
        return ok(mb.post_message(project_id, topic_id, sess, body=body.body, acting_bot_id=body.acting_bot_id, defer_orchestration=True))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="议题不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@legacy_router.get("/topics/{topic_id}/work-items")
def list_work_items(project_id: str, topic_id: str, sess: dict = Depends(project_maintainer)):
    try:
        return ok({"items": mb.list_work_items(project_id, topic_id, sess)})
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="议题不存在") from exc


@legacy_router.post("/topics/{topic_id}/advance")
def advance_topic(project_id: str, topic_id: str, body: AdvanceTopicBody, sess: dict = Depends(project_maintainer)):
    try:
        return ok(
            mb.advance_topic(
                project_id,
                topic_id,
                sess,
                goal=body.goal,
                message=body.message,
                sync_memory=body.sync_memory,
                engine=body.engine,
            )
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="议题不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@legacy_router.post("/topics/{topic_id}/pipeline/run")
def run_pipeline(project_id: str, topic_id: str, body: PipelineRunBody, sess: dict = Depends(project_maintainer)):
    try:
        return ok(
            mb.run_topic_pipeline(
                project_id,
                topic_id,
                sess,
                goal=body.goal,
                bump_version=body.bump_version,
                engine=body.engine,
            )
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="议题不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@legacy_router.post("/topics/{topic_id}/knowledge/refresh")
def refresh_knowledge(project_id: str, topic_id: str, sess: dict = Depends(project_maintainer)):
    try:
        return ok(ce.refresh_topic_knowledge(project_id, topic_id, sess))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="议题不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@legacy_router.post("/topics/{topic_id}/memory/refresh")
def refresh_memory(project_id: str, topic_id: str, sess: dict = Depends(project_maintainer)):
    try:
        return ok(mb.refresh_topic_memory(project_id, topic_id, sess))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="议题不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@legacy_router.post("/topics/{topic_id}/im/bind")
def bind_im(project_id: str, topic_id: str, body: ImBindBody, sess: dict = Depends(project_maintainer)):
    try:
        return ok(
            mb.bind_topic_im(
                project_id,
                topic_id,
                sess,
                channel=body.channel,
                chat_id=body.chat_id,
                node_id=body.node_id,
                label=body.label,
            )
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="议题不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@legacy_router.post("/topics/{topic_id}/decompose")
def decompose_topic(project_id: str, topic_id: str, body: DecomposeBody, sess: dict = Depends(project_maintainer)):
    try:
        return ok(
            mb.decompose_topic(
                project_id,
                topic_id,
                sess,
                goal=body.goal,
                input_version=body.input_version,
                items=body.items or None,
                default_action_kind=body.default_action_kind,
                default_job=body.default_job,
                use_llm=body.use_llm,
                bump_version=body.bump_version,
                include_case_run=body.include_case_run,
            )
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="议题不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@legacy_router.post("/work-items/{work_item_id}/transfer")
def transfer_work_item(
    project_id: str,
    work_item_id: str,
    body: TransferBody,
    sess: dict = Depends(project_maintainer),
):
    try:
        return ok(
            mb.transfer_work_item(
                project_id,
                work_item_id,
                sess,
                holder_user_id=body.holder_user_id,
                assignee_kind=body.assignee_kind,
                assignee_id=body.assignee_id,
            )
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="工作项不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@legacy_router.post("/work-items/{work_item_id}/apply-drafts")
def apply_work_item_drafts(
    project_id: str,
    work_item_id: str,
    body: ApplyDraftsBody,
    sess: dict = Depends(project_maintainer),
):
    try:
        return ok(
            mb.apply_work_item_drafts(
                project_id,
                work_item_id,
                sess,
                confirm=body.confirm,
                replace=body.replace,
            )
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="工作项不存在") from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail={"reason_code": "execution_owner_mismatch", "message": str(exc)}) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@legacy_router.get("/work-items/{work_item_id}/approvals")
def list_work_item_approvals(project_id: str, work_item_id: str, sess: dict = Depends(project_maintainer)):
    try:
        return ok({"items": ce.list_work_item_approvals(project_id, work_item_id, sess)})
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="工作项不存在") from exc


@legacy_router.post("/work-items/{work_item_id}/approvals/{approval_id}/decide")
def decide_work_item_approval(
    project_id: str,
    work_item_id: str,
    approval_id: str,
    body: ApprovalDecideBody,
    sess: dict = Depends(project_maintainer),
):
    try:
        return ok(
            ce.decide_work_item_approval(
                project_id,
                work_item_id,
                approval_id,
                sess,
                decision=body.decision,
                comment=body.comment,
            )
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="审批不存在") from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@legacy_router.post("/work-items/{work_item_id}/trigger")
def trigger_work_item(
    project_id: str,
    work_item_id: str,
    body: TriggerBody,
    sess: dict = Depends(project_maintainer),
):
    try:
        return ok(mb.trigger_work_item(project_id, work_item_id, sess, force_retry=body.force_retry))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="工作项不存在") from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail={"reason_code": "execution_owner_mismatch", "message": str(exc)}) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

router.include_router(legacy_router)
