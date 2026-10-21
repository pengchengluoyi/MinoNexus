"""助手：绑定、档案、轨迹、订阅、MCP token。UI（Workbench）打这里。

这里的接口都是「用户管理自己的助手」，用普通登录会话鉴权。
"""
from __future__ import annotations

import time
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict

from mino_nexus.core.http_util import ok
from mino_nexus.routers.deps import current_session
from mino_nexus.services import assistant_store as store
from mino_nexus.services.node_registry import get_registry

router = APIRouter(prefix="/assistant", tags=["Assistant"])


class ProfileBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    persona: Optional[str] = None


class BindCodeBody(BaseModel):
    node_id: str
    channel: str
    instance_id: str


class McpTokenBody(BaseModel):
    name: str = ""


class ImHoldBody(BaseModel):
    channel: str  # wechat 或 langbot:<平台>
    node_id: str


def _uid(sess: dict[str, Any]) -> str:
    uid = str(sess.get("user_id") or "").strip()
    if not uid:
        raise HTTPException(status_code=401, detail="请先登录")
    return uid


def _mcp_url(request: Request) -> str:
    return str(request.base_url).rstrip("/") + "/mcp"


def _im_nodes(sess: dict[str, Any]) -> list[dict[str, Any]]:
    from mino_nexus.services.im_status import identity_channel
    from mino_nexus.services.node_store import filter_nodes
    from mino_nexus.services.ui_devices import ui_nodes

    bound = {(row["node_id"], identity_channel(row["channel"]), row["instance_id"])
             for row in store.list_identities(_uid(sess))}
    out: list[dict[str, Any]] = []
    for node in filter_nodes(ui_nodes(), sess):
        channels = [{**row, "bound": (node["node_id"], identity_channel(row["channel"]), row.get("instance_id") or "") in bound}
                    for row in node.get("im_channels") or []]
        if channels:
            out.append({
                "node_id": node["node_id"],
                "name": node.get("hostname") or node["node_id"],
                "alive": bool(node.get("alive")),
                "channels": channels,
            })
    return out


@router.get("/status")
def status(request: Request, sess: dict = Depends(current_session)):
    uid = _uid(sess)
    nodes = _im_nodes(sess)
    enabled = store.get_profile(uid)["enabled"]
    online = enabled and any(n["alive"] and any(c["connected"] is True and c["holding"] is not False and c["bound"]
                                               and c["connection_state"] == "connected" for c in n["channels"]) for n in nodes)
    return ok({
        "enabled": enabled,
        "online": online,
        "mcp_available": enabled,
        "mcp_url": _mcp_url(request),
        "im_nodes": nodes,
    })


def _profile_view(uid: str) -> dict[str, Any]:
    prof = store.get_profile(uid)
    return {
        "user_id": uid,
        "persona": prof["persona"],
        "enabled": prof["enabled"],
    }


@router.get("/profile")
def get_profile(sess: dict = Depends(current_session)):
    return ok(_profile_view(_uid(sess)))


@router.put("/profile")
def put_profile(body: ProfileBody, sess: dict = Depends(current_session)):
    uid = _uid(sess)
    store.save_profile(uid, body.model_dump(exclude_unset=True))
    return ok(_profile_view(uid))


@router.post("/bind/code")
def bind_code(body: BindCodeBody, sess: dict = Depends(current_session)):
    uid = _uid(sess)
    from mino_nexus.core.execution_user import execution_user
    # 前端提供的路由只作选择；必须与当前用户可见的节点快照一致。
    from mino_nexus.services.im_status import identity_channel
    if not any(n["node_id"] == body.node_id and any(
            identity_channel(c["channel"]) == body.channel and c.get("instance_id") == body.instance_id
            for c in n["channels"]) for n in _im_nodes(sess)):
        raise HTTPException(status_code=409, detail="渠道实例已变化或不可用，请刷新节点后重试")
    with execution_user(uid):
        return ok(store.create_bind_code(uid, **body.model_dump()))


@router.get("/identities")
def identities(sess: dict = Depends(current_session)):
    return ok({"items": store.list_identities(_uid(sess), merge_accounts=True)})


@router.delete("/identities/{identity_id}")
def delete_identity(identity_id: int, sess: dict = Depends(current_session)):
    if not store.delete_identity(_uid(sess), identity_id):
        raise HTTPException(status_code=404, detail="绑定不存在")
    return ok({"id": identity_id})


@router.get("/tools")
def tools(_sess: dict = Depends(current_session)):
    from mino_nexus.services.assistant_tools import list_tools_public

    return ok({"items": list_tools_public()})


@router.get("/turns")
def turns(
    limit: int = Query(30),
    offset: int = Query(0),
    surface: str = Query(""),
    node_id: str = Query(""),
    sess: dict = Depends(current_session),
):
    return ok(store.list_turns(_uid(sess), limit=limit, offset=offset, surface=surface, node_id=node_id))


@router.get("/turns/{turn_id}")
def turn_detail(turn_id: str, sess: dict = Depends(current_session)):
    row = store.get_turn(_uid(sess), turn_id)
    if row is None:
        raise HTTPException(status_code=404, detail="轨迹不存在")
    return ok({k: v for k, v in row.items() if not k.startswith("_")})


@router.get("/turns/{turn_id}/messages")
def turn_messages(turn_id: str, sess: dict = Depends(current_session)):
    """优先读取本轮限长证据；历史无正文的轮次仍从 Scout 获取。"""
    row = store.get_turn(_uid(sess), turn_id)
    if row is None:
        raise HTTPException(status_code=404, detail="轨迹不存在")
    detail = row.get("detail") or {}
    if row.get("user_text") or row.get("assistant_text"):
        messages = []
        if row.get("user_text"):
            messages.append({"role": "user", "text": row["user_text"], "ts": row["created_at"]})
        status = str(detail.get("delivery_status") or "unconfirmed")
        assistant_text = str(detail.get("sent_text") or row.get("assistant_text") or "")
        if assistant_text:
            if status != "sent":
                assistant_text = ("【仅部分回复已确认发送】" if detail.get("sent_text") else "【生成回复，未确认发送】") + "\n" + assistant_text
            messages.append({"role": "assistant", "text": assistant_text, "ts": row["created_at"]})
        return ok({"available": True, "reason": "", "source": "nexus_turn", "messages": messages,
                   "request_id": row.get("request_id") or "", "delivery_status": status,
                   "text_truncated": bool(detail.get("text_truncated"))})
    channel, chat_id, node_id = row["_channel"], row["_chat_id"], row["_node_id"]
    if row["surface"] != "im" or not channel or not chat_id:
        return ok({"available": False, "reason": "这条记录没有对话正文", "messages": []})
    from mino_nexus.services.node_plugin_call import PluginCallError, call_plugin
    ctx = row["_im_route"]
    try:
        store.require_im_binding(_uid(sess), channel, node_id, ctx)
    except PermissionError:
        return ok({"available": False, "reason": "该会话绑定已失效", "messages": []})
    binding = store.find_route_identity(channel, str(ctx.get("tenant") or ""), str(ctx.get("sender_id") or ""),
        instance_id=str(ctx.get("instance_id") or ""), node_id=node_id)

    try:
        data = call_plugin(
            kind="bot",
            plugin_id=channel,
            node_id=node_id,
            capability_id="plugin.im.history",
            params={"channel": channel, "chat_id": chat_id, "limit": 40, "reply_ctx": ctx,
                    "sender_id": ctx["sender_id"], "after_ts": binding["bound_at"]},
            timeout=15,
        )
    except PluginCallError:
        return ok({"available": False, "reason": "节点离线，正文暂不可见", "messages": []})
    try:
        store.require_im_binding(_uid(sess), channel, node_id, ctx)
    except PermissionError:
        return ok({"available": False, "reason": "该会话绑定已失效", "messages": []})
    from mino_nexus.services.assistant_gateway import confirmed_history
    raw = data.get("messages") if isinstance(data.get("messages"), list) else []
    msgs = confirmed_history({**ctx, "recent_turns": raw}, binding, limit=40, text_limit=4000)
    return ok({"available": True, "reason": "", "messages": msgs})


@router.get("/mcp-tokens")
def mcp_tokens(sess: dict = Depends(current_session)):
    return ok({"items": store.list_mcp_tokens(_uid(sess))})


@router.post("/mcp-tokens")
def create_mcp_token(body: McpTokenBody, request: Request, sess: dict = Depends(current_session)):
    out = store.create_mcp_token(_uid(sess), body.name)
    return ok({**out, "mcp_url": _mcp_url(request)})


@router.delete("/mcp-tokens/{token_id}")
def revoke_mcp_token(token_id: int, sess: dict = Depends(current_session)):
    if not store.revoke_mcp_token(_uid(sess), token_id):
        raise HTTPException(status_code=404, detail="token 不存在")
    return ok({"id": token_id})


@router.get("/subscriptions")
def subscriptions(node_id: str = Query(""), sess: dict = Depends(current_session)):
    return ok({"items": store.list_subscriptions(_uid(sess), node_id=node_id)})


@router.delete("/subscriptions/{sub_id}")
def delete_subscription(sub_id: int, sess: dict = Depends(current_session)):
    if not store.delete_subscription(_uid(sess), sub_id):
        raise HTTPException(status_code=404, detail="订阅不存在")
    return ok({"id": sub_id})


@router.post("/im-hold")
def set_im_hold(body: ImHoldBody, sess: dict = Depends(current_session)):
    """当前账号只能调整自己节点的机器人持有权。"""
    from mino_nexus.services import im_hold
    from mino_nexus.services.access_scope import require_owned_node
    require_owned_node(body.node_id.strip())
    channel = body.channel.strip()
    if channel != "wechat" and not channel.startswith("langbot:"):
        raise HTTPException(status_code=400, detail="渠道只能是 wechat 或 langbot:<平台>")
    node = get_registry().get_node(body.node_id.strip())
    if node is None or not node.alive:
        raise HTTPException(status_code=409, detail="目标节点不在线")
    im_hold.pin(channel, node.node_id)
    # 目标节点此前可能被让出，先让它重新持有，再让其它节点让出
    im_hold.push_hold(node.node_id, channel, True)
    yielded = im_hold.arbitrate()
    return ok({"channel": channel, "node_id": node.node_id, "yielded": [n for _, n in yielded]})


class RequestReplyBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: int
    values: dict[str, Any]


class RequestRevisionBody(BaseModel):
    revision: int


class PreferenceBody(BaseModel):
    value: str | None = None


class GroupPolicyBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    channel: str
    instance_id: str
    tenant: str = ''
    chat_id: str
    enabled: bool = False
    allow_execute: bool = False
    disclose_results: bool = False
    project_ids: list[str] = []


@router.get('/requests')
def assistant_requests(node_id: str = Query(""), sess: dict = Depends(current_session)):
    from mino_nexus.services.assistant_conversation import list_requests
    return ok(list_requests(_uid(sess), node_id=node_id))


@router.post('/requests/{request_id}/respond')
def respond_to_request(request_id: str, body: RequestReplyBody, sess: dict = Depends(current_session)):
    from mino_nexus.core.execution_user import execution_user
    from mino_nexus.services.assistant_runtime import continue_request
    with execution_user(_uid(sess)):
        try:
            result = continue_request(request_id, body.revision, body.values)
        except (ValueError, PermissionError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    return ok(result)


@router.post('/requests/{request_id}/cancel')
def cancel_pending_request(request_id: str, body: RequestRevisionBody, sess: dict = Depends(current_session)):
    from mino_nexus.core.execution_user import execution_user
    from mino_nexus.services.assistant_conversation import cancel
    with execution_user(_uid(sess)):
        if not cancel(request_id, body.revision):
            raise HTTPException(status_code=409, detail='请求已变化；已提交的任务请在任务页停止')
    return ok({'cancelled': True})


@router.get('/preferences')
def assistant_preferences(sess: dict = Depends(current_session)):
    from mino_nexus.services.assistant_conversation import preferences
    return ok(preferences(_uid(sess)))


@router.put('/preferences/{key}')
def update_preference(key: str, body: PreferenceBody, sess: dict = Depends(current_session)):
    from mino_nexus.services.assistant_conversation import set_preference
    try:
        result = set_preference(_uid(sess), key, body.value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ok(result)


@router.get('/group-policies')
def group_policies(sess: dict = Depends(current_session)):
    from mino_nexus.services.assistant_conversation import groups
    return ok({'items': groups(_uid(sess))})


@router.put('/group-policies')
def update_group_policy(body: GroupPolicyBody, sess: dict = Depends(current_session)):
    from mino_nexus.core.execution_user import execution_user
    from mino_nexus.services.assistant_conversation import save_group
    with execution_user(_uid(sess)):
        try:
            result = save_group(_uid(sess), body.model_dump())
        except (ValueError, KeyError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ok(result)


@router.get('/capabilities')
def assistant_capabilities(sess: dict = Depends(current_session)):
    from mino_nexus.services.assistant_envelope import capabilities
    nodes = _im_nodes(sess)
    return ok({'items': [{**channel, 'node_id': node['node_id'], 'node_name': node['name'],
                'capabilities': capabilities(channel.get('capabilities'))} for node in nodes for channel in node['channels']]})


class MediaSettingsBody(BaseModel):
    asr_provider_id: str = ''
    asr_model: str = ''


class ReportSettingsBody(BaseModel):
    workbench_url: str = ''


@router.get('/report-settings')
def report_settings(sess: dict = Depends(current_session)):
    from mino_nexus.services.assistant_reports import configuration
    return ok(configuration(_uid(sess)))


@router.put('/report-settings')
def save_report_settings(body: ReportSettingsBody, sess: dict = Depends(current_session)):
    from mino_nexus.services.assistant_reports import configure
    try:
        return ok(configure(_uid(sess), body.workbench_url))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get('/media-settings')
def media_settings(sess: dict = Depends(current_session)):
    from mino_nexus.services.assistant_media import configuration
    from mino_nexus.services.settings_store import list_ai_providers
    return ok({**configuration(_uid(sess)), 'providers': [
        {'id': row['id'], 'name': row.get('name') or row['id'], 'configured': row.get('configured')}
        for row in list_ai_providers(_uid(sess)).get('providers') or []]})


@router.put('/media-settings')
def update_media_settings(body: MediaSettingsBody, sess: dict = Depends(current_session)):
    from mino_nexus.core.execution_user import execution_user
    from mino_nexus.services.assistant_media import configure
    with execution_user(_uid(sess)):
        try:
            result = configure(_uid(sess), body.model_dump())
        except (ValueError, PermissionError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ok(result)



class RequestScopeBody(BaseModel):
    revision: int
    modules: list[str]


@router.get('/requests/{request_id}/modules')
def request_modules(request_id: str, sess: dict = Depends(current_session)):
    from mino_nexus.core.execution_user import execution_user
    from mino_nexus.services.assistant_conversation import available_modules
    with execution_user(_uid(sess)):
        try:
            result = available_modules(request_id)
        except (ValueError, KeyError) as exc:
            raise HTTPException(status_code=404, detail='请求或项目范围不存在') from exc
    return ok(result)


@router.post('/requests/{request_id}/scope')
def update_request_scope(request_id: str, body: RequestScopeBody, sess: dict = Depends(current_session)):
    from mino_nexus.core.execution_user import execution_user
    from mino_nexus.services.assistant_conversation import revise_scope
    from mino_nexus.services.assistant_runtime import execute_request
    with execution_user(_uid(sess)):
        try:
            result = execute_request(revise_scope(request_id, body.revision, body.modules))
        except (ValueError, PermissionError, KeyError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    return ok(result)


@router.post('/requests/{request_id}/focus')
def focus_assistant_request(request_id: str, sess: dict = Depends(current_session)):
    from mino_nexus.core.execution_user import execution_user
    from mino_nexus.services.assistant_conversation import focus_request
    with execution_user(_uid(sess)):
        try:
            result = focus_request(request_id)
        except (ValueError, PermissionError) as exc:
            raise HTTPException(status_code=404, detail='任务不存在或无权访问') from exc
    return ok(result)
