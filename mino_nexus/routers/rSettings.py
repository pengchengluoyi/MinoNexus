"""设置：发信、模型 Key、角色目录、Figma Token、集成插件、调用记录。"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from mino_nexus.core.http_util import http_error, ok
from mino_nexus.routers.deps import current_session
from mino_nexus.services import knowledge_jobs_store as kjs
from mino_nexus.services import plugins_store as ps
from mino_nexus.services import settings_store as ss

router = APIRouter(prefix="/settings", tags=["Settings"])


class MailBody(BaseModel):
    host: str = ""
    port: int = 587
    username: str = ""
    password: str = ""
    clear_password: bool = False
    from_email: str = ""
    from_name: str = ""
    use_tls: bool = True


class MailTestBody(BaseModel):
    to: str = ""


class AIProviderSaveBody(BaseModel):
    name: str = ""
    api_type: str = ""
    api_key: str = ""
    base_url: str = ""
    model: str = ""
    enabled: bool = True
    clear_key: bool = False
    set_default: bool = False
    plan_compress_ratio: float = 3.0
    web_compress_ratio: float = 2.0
    case_execution_use: bool = False


class AIUsageSaveBody(BaseModel):
    copilot_enabled: bool = False
    case_execution_enabled: bool = False
    case_execution_provider_id: str = ""
    mode: str = "local_first"
    plan_compress_image: bool = True


class LayerStackBody(BaseModel):
    skill_drivers: dict[str, list[str]] | None = None
    role_skills: dict[str, list[str]] | None = None
    trigger_roles: dict[str, dict[str, str]] | None = None
    trigger_skills: dict[str, dict[str, str]] | None = None
    skills: list[Any] | None = None
    roles: list[Any] | None = None
    skill_categories: list[Any] | None = None
    drivers: list[Any] | None = None
    triggers: list[Any] | None = None
    reset: bool = False


class RolePromptBody(BaseModel):
    system_prompt: str = ""
    reset: bool = False


class RoleChatBody(BaseModel):
    role_id: str = ""
    messages: list[dict[str, Any]] = []
    explain_mode: bool = False


class JobSaveBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    label: str = ""
    summary: str = ""
    system_blocks: list[dict[str, Any]] | None = None
    user_blocks: list[dict[str, Any]] | None = None
    image: dict[str, Any] | None = None
    call: dict[str, Any] | None = None
    enabled: bool | None = None
    sort_order: int | None = None
    reset: bool = False
    activate_version: int | None = None


class JobPreviewBody(BaseModel):
    slots: dict[str, str] | None = None
    flags: dict[str, bool] | None = None
    system_blocks: list[dict[str, Any]] | None = None
    user_blocks: list[dict[str, Any]] | None = None


class SkillSaveBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = ""
    label: str = ""
    summary: str = ""
    category: str = ""
    engine: str = ""
    system_prompt: str = ""
    sop: dict[str, Any] | None = None
    input: dict[str, Any] | None = None
    view: dict[str, Any] | None = None
    view_id: str = ""
    role: dict[str, Any] | None = None
    role_id: str = ""
    role_label: str = ""
    triggers: list[str] | None = None
    enabled: bool | None = None
    sort_order: int | None = None
    reset: bool = False


class FigmaSettingsBody(BaseModel):
    access_token: str = ""
    clear_token: bool = False


@router.get("/mail")
def get_mail(_sess: dict = Depends(current_session)):
    return ok(ss.get_mail_settings())


@router.put("/mail")
def save_mail(body: MailBody, _sess: dict = Depends(current_session)):
    data = ss.save_mail_settings(
        host=body.host,
        port=body.port,
        username=body.username,
        password=body.password,
        clear_password=body.clear_password,
        from_email=body.from_email,
        from_name=body.from_name,
        use_tls=body.use_tls,
    )
    return ok(data, msg="已保存")


@router.post("/mail/test")
def test_mail(body: MailTestBody, _sess: dict = Depends(current_session)):
    try:
        data = ss.test_mail(body.to)
    except Exception as e:
        http_error(e)
    return ok(data, msg="测试信已发出")


@router.get("/ai/providers")
def list_ai_providers(_sess: dict = Depends(current_session)):
    return ok(ss.list_ai_providers())


@router.put("/ai/providers/{provider_id}")
def save_ai_provider(provider_id: str, body: AIProviderSaveBody, _sess: dict = Depends(current_session)):
    try:
        data = ss.save_ai_provider(provider_id, body.model_dump())
    except Exception as e:
        http_error(e)
    return ok(data, msg="已保存")


@router.delete("/ai/providers/{provider_id}")
def delete_ai_provider(provider_id: str, _sess: dict = Depends(current_session)):
    ss.delete_ai_provider(provider_id)
    return ok(msg="已删除")


@router.put("/ai/usage")
def save_ai_usage(body: AIUsageSaveBody, _sess: dict = Depends(current_session)):
    return ok(ss.save_ai_usage(body.model_dump()), msg="已保存")


@router.get("/ai/roles")
def list_ai_roles(_sess: dict = Depends(current_session)):
    from mino_nexus.ai.roles_catalog import list_roles

    return ok(list_roles())


@router.get("/ai/skills")
def list_ai_skills(_sess: dict = Depends(current_session)):
    from mino_nexus.services import skill_store as sks

    return ok(sks.catalog_payload())


@router.get("/ai/skills/{skill_id}")
def get_ai_skill(skill_id: str, _sess: dict = Depends(current_session)):
    from mino_nexus.services import skill_store as sks

    row = sks.get_skill(skill_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"未知技能：{skill_id}")
    return ok(row)


@router.post("/ai/skills")
def create_ai_skill(body: SkillSaveBody, _sess: dict = Depends(current_session)):
    from mino_nexus.services import skill_store as sks

    payload = body.model_dump(exclude_none=True)
    payload.pop("reset", None)
    try:
        data = sks.save_skill(payload.get("id") or payload.get("label") or "", payload, create=True)
    except Exception as e:
        http_error(e)
    return ok(data, msg="已新建技能")


@router.put("/ai/skills/{skill_id}")
def save_ai_skill(skill_id: str, body: SkillSaveBody, _sess: dict = Depends(current_session)):
    from mino_nexus.services import skill_store as sks

    payload = body.model_dump(exclude_none=True)
    reset = bool(payload.pop("reset", False))
    try:
        if reset:
            data = sks.reset_skill_prompt(skill_id)
        else:
            data = sks.save_skill(skill_id, payload, create=False)
    except Exception as e:
        http_error(e)
    return ok(data, msg="已恢复默认" if reset else "已保存")


@router.get("/ai/jobs")
def list_ai_jobs(_sess: dict = Depends(current_session)):
    from mino_nexus.services import job_store as js

    return ok({"jobs": js.list_jobs()})


@router.get("/ai/jobs/health")
def ai_jobs_health(_sess: dict = Depends(current_session)):
    from mino_nexus.services import job_store as js

    return ok(js.startup_health())


@router.get("/ai/jobs/{job_id}")
def get_ai_job(job_id: str, _sess: dict = Depends(current_session)):
    from mino_nexus.services import job_store as js

    row = js.get_job(job_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"未知 job：{job_id}")
    return ok(row)


@router.put("/ai/jobs/{job_id}")
def save_ai_job(job_id: str, body: JobSaveBody, _sess: dict = Depends(current_session)):
    from mino_nexus.services import job_store as js

    payload = body.model_dump(exclude_none=True)
    reset = bool(payload.pop("reset", False))
    if reset:
        payload = {"reset": True}
    try:
        data = js.save_job(job_id, payload)
    except Exception as e:
        http_error(e)
    return ok(data, msg="已恢复上一版" if reset else "已保存")


@router.post("/ai/jobs/{job_id}/preview")
def preview_ai_job(job_id: str, body: JobPreviewBody, _sess: dict = Depends(current_session)):
    from mino_nexus.services import job_store as js

    try:
        data = js.preview_job(
            job_id,
            slots=body.slots,
            flags=body.flags,
            system_blocks=body.system_blocks,
            user_blocks=body.user_blocks,
        )
    except Exception as e:
        http_error(e)
    return ok(data)


@router.get("/ai/roles/{role_id}")
def get_ai_role(role_id: str, _sess: dict = Depends(current_session)):
    from mino_nexus.ai.roles_catalog import get_role

    row = get_role(role_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"未知角色：{role_id}")
    return ok(row)


@router.get("/ai/stack")
def get_stack(_sess: dict = Depends(current_session)):
    from mino_nexus.ai.layer_stack import get_stack

    return ok(get_stack())


@router.put("/ai/stack")
def save_stack(body: LayerStackBody, _sess: dict = Depends(current_session)):
    from mino_nexus.ai.layer_stack import save_bindings

    payload = body.model_dump(exclude_none=True)
    reset = bool(payload.pop("reset", False))
    return ok(save_bindings(payload, reset=reset), msg="已恢复默认" if reset else "已保存")


@router.put("/ai/roles/{role_id}/prompt")
def save_role_prompt(role_id: str, body: RolePromptBody, _sess: dict = Depends(current_session)):
    try:
        data = ss.save_role_prompt(role_id, system_prompt=body.system_prompt, reset=body.reset)
    except Exception as e:
        http_error(e)
    return ok(data, msg="已恢复默认" if body.reset else "已保存")


@router.post("/ai/roles/chat")
def chat_role(body: RoleChatBody, _sess: dict = Depends(current_session)):
    from mino_nexus.ai.roles_catalog import chat_with_role
    from mino_nexus.ai import dispatch_log as dispatch

    tok = dispatch.bind(trigger="settings_chat", source="settings_role_chat", role=body.role_id, job="role_chat")
    try:
        data = chat_with_role(role_id=body.role_id, messages=body.messages, explain_mode=body.explain_mode)
    except Exception as e:
        http_error(e)
    finally:
        dispatch.reset(tok)
    return ok(data)


@router.get("/figma")
def get_figma(_sess: dict = Depends(current_session)):
    return ok(ss.get_figma_settings())


@router.put("/figma")
def save_figma(body: FigmaSettingsBody, _sess: dict = Depends(current_session)):
    return ok(ss.save_figma_settings(access_token=body.access_token, clear_token=body.clear_token), msg="已保存")


@router.post("/figma/test")
def test_figma(body: FigmaSettingsBody, _sess: dict = Depends(current_session)):
    from mino_nexus.services import figma_service as fs

    try:
        info = fs.test_figma_token(body.access_token or None)
    except Exception as e:
        http_error(e)
    return ok(info, msg="Token 可用")


@router.get("/dispatch")
def list_dispatch_calls(
    limit: int = 80,
    kind: str = "",
    role: str = "",
    trigger: str = "",
    app_id: str = "",
    pipeline_id: str = "",
    _sess: dict = Depends(current_session),
):
    from mino_nexus.ai.dispatch_log import list_calls

    rows = list_calls(limit=limit, kind=kind, role=role, trigger=trigger, app_id=app_id, pipeline_id=pipeline_id)
    return ok({"calls": rows, "total": len(rows)})


@router.get("/dispatch/{call_id}")
def get_dispatch_call(call_id: str, _sess: dict = Depends(current_session)):
    from mino_nexus.ai.dispatch_log import get_call

    row = get_call(call_id)
    if not row:
        raise HTTPException(status_code=404, detail="没有这条调度记录")
    return ok(row)


class RobotIntegrationCreate(BaseModel):
    platform: str = "lark"
    name: str = ""
    credentials: dict[str, Any] = Field(default_factory=dict)


class RobotIntegrationUpdate(BaseModel):
    platform: Optional[str] = None
    name: Optional[str] = None
    credentials: dict[str, Any] = Field(default_factory=dict)
    clear_secret: bool = False


@router.get("/robots/bots")
def list_robot_bots(_sess: dict = Depends(current_session)):
    return ok({"bots": ps.list_robot_integrations()})


@router.post("/robots/bots")
def create_robot_bot(body: RobotIntegrationCreate, _sess: dict = Depends(current_session)):
    try:
        row = ps.create_robot_integration(platform=body.platform, name=body.name, credentials=body.credentials)
    except Exception as e:
        http_error(e)
    return ok(row, msg="机器人已添加")


@router.put("/robots/bots/{bot_id}")
def update_robot_bot(bot_id: str, body: RobotIntegrationUpdate, _sess: dict = Depends(current_session)):
    try:
        row = ps.update_robot_integration(
            bot_id,
            platform=body.platform,
            name=body.name,
            credentials=body.credentials,
            clear_secret=body.clear_secret,
        )
    except Exception as e:
        http_error(e)
    return ok(row, msg="已保存")


@router.delete("/robots/bots/{bot_id}")
def delete_robot_bot(bot_id: str, _sess: dict = Depends(current_session)):
    try:
        ps.delete_robot_integration(bot_id)
    except Exception as e:
        http_error(e)
    return ok(msg="已删除")


class PluginSaveBody(BaseModel):
    model_config = ConfigDict(extra="allow")
    enabled: Optional[bool] = None
    capabilities: Optional[dict[str, Any]] = None
    wiki: Optional[dict[str, Any]] = None
    notify: Optional[dict[str, Any]] = None
    writeback: Optional[dict[str, Any]] = None
    flow: Optional[dict[str, Any]] = None
    templates: Optional[list[dict[str, Any]]] = None
    bindings: Optional[list[dict[str, Any]]] = None
    url: Optional[str] = None
    account: Optional[str] = None
    token: Optional[str] = None
    clear_token: bool = False
    access_token: Optional[str] = None
    default_file_url: Optional[str] = None
    chat: Optional[dict[str, Any]] = None


class PluginChatBody(BaseModel):
    text: str = ""
    mode: str = ""
    history: list[dict[str, Any]] = Field(default_factory=list)


class WikiDebugBody(BaseModel):
    action: str = "ping"


class WechatVerifyBody(BaseModel):
    verify_code: str = ""


class ZentaoTestBody(BaseModel):
    url: str = ""
    account: str = ""
    token: str = ""


class ZentaoTokenBody(BaseModel):
    url: str = ""
    account: str = ""
    password: str = ""


class ZentaoBugTestBody(BaseModel):
    project_id: str = ""
    product_id: str = ""
    template_id: str = ""
    title: str = ""


@router.get("/plugins")
def list_plugins(_sess: dict = Depends(current_session)):
    return ok(ps.list_integration_plugins())


@router.post("/plugins/feishu/wiki/debug")
def debug_feishu_wiki(_body: WikiDebugBody, _sess: dict = Depends(current_session)):
    raise HTTPException(status_code=400, detail=ps.FEISHU_LIVE_NOT_PORTED)


@router.post("/plugins/feishu/listener/sync")
def sync_feishu_listener(_sess: dict = Depends(current_session)):
    raise HTTPException(status_code=400, detail=ps.FEISHU_LIVE_NOT_PORTED)


@router.post("/plugins/wechat/login")
def start_wechat_login(_sess: dict = Depends(current_session)):
    raise HTTPException(status_code=400, detail=ps.WECHAT_NOT_PORTED)


@router.get("/plugins/wechat/login")
def get_wechat_login(_sess: dict = Depends(current_session)):
    return ok({
        "logged_in": False,
        "status": "idle",
        "qrcode_img": "",
        "need_verify": False,
        "error": ps.WECHAT_NOT_PORTED,
    })


@router.post("/plugins/wechat/login/verify")
def verify_wechat_login(_body: WechatVerifyBody, _sess: dict = Depends(current_session)):
    raise HTTPException(status_code=400, detail=ps.WECHAT_NOT_PORTED)


@router.post("/plugins/wechat/logout")
def logout_wechat_plugin(_sess: dict = Depends(current_session)):
    raise HTTPException(status_code=400, detail=ps.WECHAT_NOT_PORTED)


@router.post("/plugins/wechat/listener/sync")
def sync_wechat_listener_api(_sess: dict = Depends(current_session)):
    raise HTTPException(status_code=400, detail=ps.WECHAT_NOT_PORTED)


@router.post("/plugins/zentao/test")
def test_zentao_plugin(body: ZentaoTestBody, _sess: dict = Depends(current_session)):
    try:
        info = ps.test_zentao_connection(url=body.url, account=body.account, token=body.token)
    except Exception as e:
        http_error(e)
    return ok(info, msg="已连通")


@router.post("/plugins/zentao/token")
def fetch_zentao_plugin_token(body: ZentaoTokenBody, _sess: dict = Depends(current_session)):
    try:
        info = ps.fetch_zentao_token(url=body.url, account=body.account, password=body.password)
        plugin = ps.save_integration_plugin(
            "zentao",
            {"url": info.get("url") or "", "account": info.get("account") or "", "token": info.get("token") or ""},
        )
    except Exception as e:
        http_error(e)
    return ok(
        {
            "ok": True,
            "url": info.get("url") or "",
            "account": info.get("account") or "",
            "has_token": True,
            "plugin": plugin,
        },
        msg="已获取并保存 Token",
    )


@router.post("/plugins/zentao/bugs/test")
def test_zentao_plugin_bug(_body: ZentaoBugTestBody, _sess: dict = Depends(current_session)):
    raise HTTPException(status_code=400, detail=ps.ZENTAO_BUG_NOT_PORTED)


@router.get("/plugins/{plugin_id}")
def get_plugin(plugin_id: str, _sess: dict = Depends(current_session)):
    try:
        data = ps.get_integration_plugin(plugin_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return ok(data)


@router.put("/plugins/{plugin_id}")
def save_plugin(plugin_id: str, body: PluginSaveBody, _sess: dict = Depends(current_session)):
    try:
        data = ps.save_integration_plugin(plugin_id, body.model_dump(exclude_unset=True))
    except Exception as e:
        http_error(e)
    return ok(data, msg="已保存")


@router.post("/plugins/{plugin_id}/chat")
def chat_plugin(plugin_id: str, body: PluginChatBody, _sess: dict = Depends(current_session)):
    try:
        data = ps.chat_integration_plugin(
            plugin_id,
            text=body.text,
            history=body.history,
            mode=body.mode,
        )
    except Exception as e:
        http_error(e)
    return ok(data)


class KnowledgeItemBody(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str = ""
    title: str = ""
    content: str = ""
    category: str = ""
    tags: list[str] = []
    app_ids: list[str] = []
    enabled: bool = True
    source: str = "manual"
    review_status: str = "approved"
    account_id: str = ""
    account_ident: str = ""


class KnowledgeSaveBody(BaseModel):
    items: list[dict[str, Any]] = []


class KnowledgeJobsBody(BaseModel):
    capture_enabled: bool = True
    review_enabled: bool = True
    account_id: str = ""
    account_ident: str = ""
    project_id: str = ""
    app_id: str = ""


class KnowledgeReviewBody(BaseModel):
    action: str = "approve"
    updates: dict[str, Any] | None = None


class KnowledgeAppendBody(BaseModel):
    app_id: str = ""
    item: dict[str, Any] = {}


class KnowledgeAutoReviewBody(BaseModel):
    app_id: str = ""
    account_id: str = ""
    account_ident: str = ""


class FailureAnalyzeBody(BaseModel):
    app_id: str = ""
    account_id: str = ""
    case_name: str = ""
    command: str = ""
    step_text: str = ""
    action_text: str = ""
    expected_text: str = ""
    title: str = ""
    msg: str = ""
    method: str = ""
    role: str = "action"
    ok: bool = False
    assert_invalid: str = ""


@router.get("/knowledge")
def get_testing_knowledge(
    app_id: str = "",
    account_id: str = "",
    account_ident: str = "",
    _sess: dict = Depends(current_session),
):
    return ok({"items": ss.list_testing_knowledge(app_id=app_id, account_id=account_id, account_ident=account_ident)})


@router.put("/knowledge")
def save_testing_knowledge(body: KnowledgeSaveBody, _sess: dict = Depends(current_session)):
    items = ss.save_testing_knowledge(list(body.items or []))
    return ok({"items": items}, msg="已保存")


@router.get("/knowledge/jobs")
def knowledge_jobs(
    account_id: str = "",
    account_ident: str = "",
    project_id: str = "",
    app_id: str = "",
    _sess: dict = Depends(current_session),
):
    resolved_id = str(account_id or "").strip()
    resolved_ident = str(account_ident or "").strip()
    if resolved_id or resolved_ident:
        try:
            found = kjs.resolve_app_account(
                account_id=resolved_id,
                account_ident=resolved_ident,
                project_id=project_id,
                app_id=app_id,
            )
        except Exception as e:
            http_error(e)
        resolved_id = found["account_id"]
        resolved_ident = found["account_ident"]
    return ok(kjs.get_knowledge_job_settings(account_id=resolved_id, account_ident=resolved_ident))


@router.api_route("/knowledge/jobs", methods=["PUT", "POST", "PATCH"])
def save_knowledge_jobs(body: KnowledgeJobsBody, _sess: dict = Depends(current_session)):
    try:
        found = kjs.resolve_app_account(
            account_id=body.account_id,
            account_ident=body.account_ident,
            project_id=body.project_id,
            app_id=body.app_id,
        )
        data = kjs.save_knowledge_job_settings(
            capture_enabled=body.capture_enabled,
            review_enabled=body.review_enabled,
            account_id=found["account_id"],
            account_ident=found["account_ident"],
        )
    except Exception as e:
        http_error(e)
    return ok(data, msg="已保存")


@router.post("/knowledge/auto-review")
def auto_review_knowledge(body: KnowledgeAutoReviewBody, _sess: dict = Depends(current_session)):
    jobs = kjs.get_knowledge_job_settings(account_id=body.account_id, account_ident=body.account_ident)
    pending = [
        x for x in ss.list_testing_knowledge(app_id=body.app_id, account_id=body.account_id, account_ident=body.account_ident)
        if str(x.get("review_status") or "") == "pending"
    ]
    if not jobs.get("review_enabled"):
        return ok({
            "approved": 0,
            "rejected": 0,
            "held": 0,
            "skipped": len(pending),
            "disabled": True,
        }, msg="知识机审已关闭")
    return ok({
        "approved": 0,
        "rejected": 0,
        "held": len(pending),
        "skipped": 0,
        "disabled": False,
    }, msg="机审完成")


@router.post("/knowledge/analyze-failure")
def analyze_failure_for_knowledge(body: FailureAnalyzeBody, _sess: dict = Depends(current_session)):
    return ok({
        "items": [],
        "held": True,
        "reason": "失败分析尚未接入",
        "account_id": str(body.account_id or "").strip(),
        "app_id": str(body.app_id or "").strip(),
    })


@router.get("/knowledge/app/{app_id}")
def list_app_knowledge(
    app_id: str,
    account_id: str = "",
    account_ident: str = "",
    _sess: dict = Depends(current_session),
):
    return ok({"items": ss.list_testing_knowledge(app_id=app_id, account_id=account_id, account_ident=account_ident)})


@router.post("/knowledge/append")
def append_app_knowledge(body: KnowledgeAppendBody, _sess: dict = Depends(current_session)):
    item = dict(body.item or {})
    if body.app_id:
        ids = [str(x) for x in (item.get("app_ids") or []) if str(x).strip()]
        if body.app_id not in ids:
            ids.append(body.app_id)
        try:
            from mino_nexus.services import project_store as ps

            app = ps.find_app(body.app_id)
            pid = str((app or {}).get("project_id") or "").strip()
            if pid and pid not in ids:
                ids.append(pid)
        except Exception:
            pass
        item["app_ids"] = ids
    try:
        row = ss.upsert_knowledge_item(item)
    except Exception as e:
        http_error(e)
    return ok(row, msg="已写入")


@router.post("/knowledge/backfill-situations")
def backfill_knowledge_situations(
    app_id: str = "",
    project_id: str = "",
    limit: int = 8,
    _sess: dict = Depends(current_session),
):
    """给缺情境指纹的已审核知识补 LLM 情境卡（需配置 provider）。"""
    from mino_nexus.services.knowledge_situation import backfill_situations

    try:
        n = backfill_situations(app_id=app_id, project_id=project_id, limit=limit)
    except Exception as e:
        http_error(e)
    return ok({"updated": n}, msg=f"已补卡 {n} 条")


@router.put("/knowledge/{kid}")
def upsert_knowledge_item(kid: str, body: KnowledgeItemBody, _sess: dict = Depends(current_session)):
    payload = body.model_dump()
    if kid and kid != "new":
        payload["id"] = kid
    try:
        row = ss.upsert_knowledge_item(payload)
    except Exception as e:
        http_error(e)
    return ok(row, msg="已保存")


@router.delete("/knowledge/{kid}")
def delete_knowledge_item(kid: str, _sess: dict = Depends(current_session)):
    if not ss.delete_knowledge_item(kid):
        raise HTTPException(status_code=404, detail="知识条目不存在")
    return ok({"id": kid}, msg="已删除")


@router.post("/knowledge/{kid}/review")
def review_knowledge_item(kid: str, body: KnowledgeReviewBody, _sess: dict = Depends(current_session)):
    try:
        row = ss.review_knowledge_item(kid, action=body.action, updates=body.updates)
    except KeyError:
        raise HTTPException(status_code=404, detail="知识条目不存在")
    except Exception as e:
        http_error(e)
    return ok(row, msg="已审核")
