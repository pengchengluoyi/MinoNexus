"""节点列表、归属过滤、Scout 安装凭证与节点指令（EXECUTE node.stop 等）。"""
from __future__ import annotations

import inspect
import threading
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from mino_nexus.platform import auth_store
from mino_nexus.core import protocol as P
from mino_nexus.core.http_util import client_header, ok
from mino_nexus.core.runtime_mode import is_development
from mino_nexus.resources.node_registry import get_registry
from mino_nexus.resources.node_store import filter_nodes, get_node as stored_node, is_admin, node_visible
from mino_nexus.api.deps import current_session, require_packs_writer
from mino_nexus.resources.node_store import sanitize_id, upsert
from mino_nexus.resources.runtime_tokens import issue
from mino_nexus.resources.runtime_tokens import installation_status, revoke_node_credential
from mino_nexus.api.scout_install import install_commands, installation_release, public_nexus_url
from mino_nexus.resources.ui_devices import ui_assets, ui_devices, ui_nodes
from mino_nexus.resources.node_workload import workload_for_node_id

router = APIRouter(prefix="/runtime", tags=["Runtime"])

_SCOUT_UPDATE_LOCK = threading.Lock()
_SCOUT_UPDATES: set[str] = set()

_REMOTE_COMMANDS = frozenset({"stop", "restart", "sleep", "wake"})


class NodeCommandBody(BaseModel):
    command: str
    reason: str = ""


class ScoutUpdateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,100}$")
    build_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
    release_tag: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
    action: str = Field(default="update", pattern=r"^(update|rollback)$")
    rollback_of: str = Field(default="", max_length=100)
    wait_seconds: int = Field(default=1800, ge=1, le=1800)
    reason: str = Field(default="mcp", max_length=500)


class NodePatchBody(BaseModel):
    owner_user_id: str | None = None
    studio_id: str | None = None


class NodePluginConfigBody(BaseModel):
    kind: str
    plugin_id: str
    values: dict[str, Any] = {}
    clear: list[str] = []


class NodeConfigImportBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    archive: str = Field(min_length=1, max_length=2796208)
    overwrite: bool = False


class NodePluginActionBody(BaseModel):
    kind: str
    plugin_id: str


class NodePluginCallBody(BaseModel):
    capability_id: str
    params: dict[str, Any] = {}


class ChannelProgramBody(BaseModel):
    """把渠道程序安装转成节点下载中心已有请求。不接收临时地址，Nexus 不保存安装包。"""
    model_config = ConfigDict(extra='forbid')
    node_id: str = Field(min_length=1, max_length=128)
    tool_id: Literal['chromium', 'adb.keyboard', 'appium']
    action: Literal['install', 'update'] = 'install'
    operation_id: str = Field(default='', max_length=100)
    wait_ms: int = Field(default=0, ge=0, le=60000)


class PluginInstallBody(BaseModel):
    """插件安装/更新任务；不接收下载地址或密钥。"""
    model_config = ConfigDict(extra='forbid')
    node_id: str = Field(min_length=1, max_length=128)
    plugin: Literal['cli.feishu', 'cli.meego', 'langbot']
    action: Literal['install', 'update'] = 'install'
    operation_id: str = Field(default='', max_length=100)
    wait_ms: int = Field(default=0, ge=0, le=60000)


class CliAttachmentBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    ref: str = Field(pattern=r'^media-[0-9a-f]{32}$')
    field_key: str = Field(min_length=1, max_length=128)
    resource_type: Literal[15, 16] = 15
    filename: str = Field(default='', max_length=200)


class CliCommandBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    plugin: Literal['feishu', 'meego']
    node_id: str = Field(default='', max_length=128)
    command: list[str] = Field(min_length=1, max_length=8)
    options: dict[str, Any] = Field(default_factory=dict)
    attachment_ref: str = Field(default='', pattern=r'^(media-[0-9a-f]{32})?$')
    attachments: list[CliAttachmentBody] = Field(default_factory=list, max_length=4)


class CaseApiReadBody(BaseModel):
    """MCP 读取用例服务 API；配置里的密钥仍只留在节点上。"""
    model_config = ConfigDict(extra='forbid')
    project_id: str = Field(min_length=1, max_length=100)
    operation: Literal['projects', 'sessions', 'cases'] = 'cases'
    configuration: dict[str, Any] = Field(default_factory=dict)
    limit: int = Field(default=200, ge=1, le=500)


class ImSendBody(BaseModel):
    """向已上报的 IM 渠道发一条文本。渠道和机器人实例由服务端按节点配置校验。"""
    model_config = ConfigDict(extra='forbid')
    channel: str = Field(min_length=1, max_length=64)
    node_id: str = Field(default='', max_length=128)
    chat_id: str = Field(min_length=1, max_length=256)
    sender_id: str = Field(default='', max_length=256)
    text: str = Field(min_length=1, max_length=4000)
    chat_type: Literal['private', 'group'] = 'private'
    reply_ctx: dict[str, Any] = Field(default_factory=dict)
    client_request_id: str = Field(default='', max_length=160)


class GmailOtpBody(BaseModel):
    """从当前账号节点上的 Gmail 插件取一封邮件的验证码。"""
    model_config = ConfigDict(extra='forbid')
    node_id: str = Field(default='', max_length=128)
    to_address: str = Field(default='', max_length=320)
    since_ts: float | None = Field(default=None, ge=0)
    lookback_seconds: int = Field(default=600, ge=10, le=86400)
    max_wait_ms: int = Field(default=15000, ge=1000, le=45000)
    poll_interval_ms: int = Field(default=200, ge=200, le=10000)
    subject_contains: str = Field(default='', max_length=200)
    from_allowlist: list[str] = Field(default_factory=list, max_length=10)


async def _cli_command(body: CliCommandBody, sess: dict, mode: str):
    from mino_nexus.platform.identity import execution_user
    from mino_nexus.resources.node_plugin_call import PluginCallError, PluginCapabilityError, call_plugin
    import asyncio

    def execute():
        with execution_user(str(sess.get('user_id') or '')):
            params = {'action': 'command', 'mode': mode, 'command': body.command, 'options': body.options}
            node_id = body.node_id
            if body.attachment_ref or body.attachments:
                from mino_nexus.platform.llm.dispatch_log import ctx
                from mino_nexus.assistant.store import require_im_binding
                origin = ctx().get('assistant_origin') or {}
                if (mode != 'write' or body.plugin != 'meego' or not origin.get('channel')
                        or not origin.get('node_id') or (node_id and node_id != origin['node_id'])):
                    raise HTTPException(status_code=422, detail='附件必须来自当前已绑定会话，并在持有原附件的节点上传。')
                require_im_binding(str(sess.get('user_id') or ''), origin['channel'], origin['node_id'], origin)
                node_id = origin['node_id']
                params.update(attachment_origin=origin, attachment_ref=body.attachment_ref,
                              attachments=[item.model_dump() for item in body.attachments])
            return call_plugin(kind='cli', plugin_id=body.plugin, node_id=node_id,
                               capability_id='plugin.cli.' + body.plugin,
                               params=params, timeout=110 + 100 * len(body.attachments))
    try:
        data = await asyncio.to_thread(execute)
        data = {key: value for key, value in data.items() if key != 'plugins'}
        if data.get('ok') is False and not data.get('needs_input'):
            status = data.get('status')
            raise HTTPException(status_code=status if status in (403, 422, 503) else 503,
                                detail={**data, 'message': str(data.get('summary') or 'CLI 操作未完成')})
        return ok(data, msg=str(data.get('summary') or ''))
    except PluginCapabilityError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except PluginCallError as exc:
        # 写入可能已抵达第三方（含超时、CLI 部分成功），保留 uncertain 回执，禁止自动重放。
        raise HTTPException(status_code=503 if mode == 'write' else 409, detail=str(exc)) from exc


@router.post('/cli/query')
async def query_cli(body: CliCommandBody, sess: dict = Depends(current_session)):
    return await _cli_command(body, sess, 'read')


@router.post('/cli/execute')
async def execute_cli(body: CliCommandBody, sess: dict = Depends(current_session)):
    return await _cli_command(body, sess, 'write')


@router.post('/case-api/read')
def read_case_api(body: CaseApiReadBody, sess: dict = Depends(current_session)):
    """读取用例服务 API 的项目/会话/用例。只读，不触发导入或写第三方。"""
    from mino_nexus.casework.case_import_sources import read as read_source

    operation = 'read' if body.operation == 'cases' else body.operation
    try:
        data = read_source(body.project_id, 'case_api', body.configuration, operation)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={'reason_code': str(exc)}) from exc
    import json

    def size(value: Any) -> int:
        return len(json.dumps(value, ensure_ascii=False, default=str).encode('utf-8'))

    if body.operation == 'cases':
        rows = data.get('table') if isinstance(data.get('table'), list) else []
        total = max(0, len(rows) - 1)
        table = rows[:body.limit + 1]
        data = {**data, 'table': table, 'total_rows': total,
                'returned_rows': max(0, len(table) - 1), 'truncated': total > body.limit}
        # MCP 客户端对超大 JSON 很敏感；保留表头，按整行减半直到 240KB 以内。
        while len(table) > 2 and size(data) > 240_000:
            table = table[:max(2, len(table) // 2)]
            data = {**data, 'table': table, 'returned_rows': len(table) - 1, 'truncated': True}
        if len(table) > 1 and size(data) > 240_000:
            data = {**data, 'table': table[:1], 'returned_rows': 0, 'truncated': True}
    else:
        items = data.get('items') if isinstance(data.get('items'), list) else []
        data = {**data, 'items': items[:body.limit], 'total': len(items),
                'returned': min(len(items), body.limit), 'truncated': len(items) > body.limit}
        while len(items) > 1 and size(data) > 240_000:
            items = items[:max(1, len(items) // 2)]
            data = {**data, 'items': items, 'returned': len(items), 'truncated': True}
        if items and size(data) > 240_000:
            data = {**data, 'items': [], 'returned': 0, 'truncated': True}
    return ok(data)


def _im_route(channel: str) -> tuple[str, str, str, str]:
    """(kind, plugin_id, 节点渠道名, Scout 渠道名)。只接受已建模的三类宿主。"""
    key = str(channel or '').strip()
    if key == 'feishu_cli':
        return 'cli', 'feishu', 'feishu_cli', 'feishu_cli'
    if key == 'wechat':
        return 'bot', 'wechat', 'wechat', 'wechat'
    key = key.removeprefix('langbot:')
    if key in ('feishu', 'wecom', 'dingtalk'):
        return 'im', 'langbot', f'langbot:{key}', key
    raise HTTPException(status_code=400, detail='渠道只能是 feishu_cli、wechat、feishu、wecom 或 dingtalk')


@router.post('/im/send')
def send_im_message(body: ImSendBody, sess: dict = Depends(current_session)):
    """按节点已上报的机器人实例发送文本。微信主动发送仍要求入站 context_token。"""
    import hashlib
    import json

    from mino_nexus.platform.identity import execution_user
    from mino_nexus.platform.im_channels import normalize_im_channels
    from mino_nexus.resources.node_plugin_call import PluginCallError, call_plugin, node_for_plugin

    kind, plugin_id, row_channel, scout_channel = _im_route(body.channel)
    uid = str(sess.get('user_id') or '')
    with execution_user(uid):
        try:
            node = node_for_plugin(kind, plugin_id, body.node_id)
        except PluginCallError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        row = next((item for item in normalize_im_channels(node.plugins)
                    if item.get('channel') == row_channel), None)
        if row is None:
            raise HTTPException(status_code=409, detail='节点尚未上报该 IM 渠道，请先在 Workbench 配置并等待重新上报')
        if row.get('connection_state') in ('disabled', 'not_installed', 'yielded', 'error'):
            raise HTTPException(status_code=409, detail=str(row.get('error') or '该 IM 渠道当前不可发送'))
        instance = str(row.get('instance_id') or '')
        ctx = {str(key): value for key, value in (body.reply_ctx or {}).items()}
        ctx['instance_id'] = instance
        if scout_channel == 'wechat':
            if not str(ctx.get('context_token') or '').strip():
                raise HTTPException(status_code=422, detail='微信主动发送需要入站会话的 context_token；仅配置插件不足以新开对话')
        elif scout_channel == 'feishu_cli':
            if not instance:
                raise HTTPException(status_code=409, detail='飞书 CLI 尚未上报可用实例，请先完成配置')
        else:
            # 机器人实例只认节点上报值，不接受调用方伪造另一个 bot。
            bot_uuid = instance.strip()
            if not bot_uuid:
                raise HTTPException(status_code=409, detail='LangBot 机器人实例尚未就绪，请先完成配置')
            ctx['bot_uuid'] = bot_uuid
            ctx['target_type'] = 'group' if body.chat_type == 'group' else 'person'
            ctx['target_id'] = body.chat_id
            ctx.setdefault('tenant', '')
        delivery_id = body.client_request_id.strip() or hashlib.sha256(json.dumps(
            [uid, scout_channel, instance, body.chat_id, body.sender_id, body.chat_type, body.text],
            ensure_ascii=False).encode('utf-8')).hexdigest()[:48]
        try:
            data = call_plugin(
                kind=kind, plugin_id=plugin_id, node_id=node.node_id,
                capability_id='plugin.im.send',
                params={'channel': scout_channel, 'chat_id': body.chat_id, 'sender_id': body.sender_id,
                        'text': body.text, 'reply_ctx': ctx, 'delivery_id': delivery_id},
                timeout=45,
            )
        except PluginCallError as exc:
            raise HTTPException(status_code=503, detail={'message': str(exc), 'delivery_id': delivery_id}) from exc
    if data.get('ok') is not True:
        raise HTTPException(status_code=502, detail={'message': str(data.get('summary') or 'IM 发送未确认完成'),
                                                    'delivery_id': delivery_id})
    provider_message_id = str(data.get('provider_message_id') or '')
    return ok({'id': provider_message_id or delivery_id, 'node_id': node.node_id,
               'channel': body.channel, 'chat_id': body.chat_id,
               'provider_message_id': provider_message_id,
               'delivery_id': delivery_id}, msg=str(data.get('summary') or '已发送'))


@router.post('/gmail/otp')
def fetch_gmail_otp(body: GmailOtpBody, sess: dict = Depends(current_session)):
    """从当前账号节点上的 Gmail 插件取码；只返回验证码和有限诊断，不回传邮件正文。"""
    import re
    import time

    from mino_nexus.platform.identity import execution_user
    from mino_nexus.resources.node_plugin_call import PluginCallError, call_plugin_with_raw, node_for_plugin

    uid = str(sess.get('user_id') or '')
    with execution_user(uid):
        try:
            node = node_for_plugin('mail', 'gmail', body.node_id)
        except PluginCallError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        to_address = body.to_address.strip()
        if not to_address:
            for plugin in node.plugins or []:
                if plugin.get('class') == 'mail' and plugin.get('id') == 'gmail':
                    to_address = str((plugin.get('values') or {}).get('inbox_address') or '').strip()
                    break
        if to_address and ('@' not in to_address or any(ch.isspace() for ch in to_address)):
            raise HTTPException(status_code=422, detail='收件地址不合法')
        now = time.time()
        since_ts = float(body.since_ts) if body.since_ts else now - body.lookback_seconds
        import math
        if not math.isfinite(since_ts) or since_ts > now + 60 or since_ts < now - 86400 * 7:
            raise HTTPException(status_code=422, detail='since_ts 只能取最近 7 天，且不能晚于当前时间')
        try:
            data, raw, summary, _node = call_plugin_with_raw(
                kind='mail', plugin_id='gmail', node_id=node.node_id,
                capability_id='plugin.gmail.fetch_otp',
                params={'to_address': to_address, 'since_ts': since_ts,
                        'deadline_ts': now + body.max_wait_ms / 1000.0 + 5,
                        'poll_interval_ms': body.poll_interval_ms, 'max_wait_ms': body.max_wait_ms,
                        'from_allowlist': body.from_allowlist, 'subject_contains': body.subject_contains},
                timeout=body.max_wait_ms / 1000.0 + 10,
            )
        except PluginCallError as exc:
            error_text = str(exc)
            error_text = {
                'otp_mail_not_received': '等待窗口内没有收到验证码邮件',
                'otp_mail_filter_no_match': '有邮件，但没有匹配到指定发件人、主题或收件人',
                'otp_mail_code_not_found': '邮件里没有找到 4-8 位验证码',
                'otp_wait_stopped': '取码等待已停止，请缩短等待窗口后重试',
                'otp_request_invalid': '取码参数不合法，请核对收件地址和等待窗口',
            }.get(error_text, error_text)
            raise HTTPException(status_code=502, detail={'message': error_text}) from exc
    code = str(raw.get('code') or data.get('code') or '').strip()
    if not re.fullmatch(r'\d{4,8}', code):
        raise HTTPException(status_code=502, detail={'message': summary or '没有取到验证码，请确认收件邮箱和等待窗口'})
    raw_diag = raw.get('otp_diagnostics') if isinstance(raw.get('otp_diagnostics'), dict) else {}
    diagnostics = {key: raw_diag[key] for key in ('connection_attempts', 'reconnects', 'error_code', 'phase')
                   if isinstance(raw_diag.get(key), (str, int, float, bool))}
    return ok({'node_id': node.node_id, 'code': code, 'source': 'gmail',
               'summary': summary or '已取到验证码', 'diagnostics': diagnostics})


@router.get("/nodes")
def list_nodes(
    studio_id: str = Query(default=""),
    sess: dict = Depends(current_session),
):
    rows = ui_nodes()
    for row in rows:
        uid = str(row.get("owner_user_id") or "")
        pub = auth_store.public_user_by_id(uid) if uid else None
        if pub:
            row["owner_name"] = pub.get("name") or pub.get("username") or ""
    visible = filter_nodes(rows, sess, studio_id=studio_id)
    return ok({"nodes": visible, "count": len(visible)})


@router.get("/assets")
def list_runtime_assets(
    studio_id: str = Query(default=""),
    sess: dict = Depends(current_session),
    client: str = Depends(client_header),
):
    data = ui_assets()
    scouts = data.get("scouts") or []
    scouts = filter_nodes(scouts, sess, studio_id=studio_id)
    # studio 元信息也可能夹带其他账号设备；只根据本人 Scout 重建关联清单。
    owned_ids = {str(row.get("node_id") or "") for row in scouts}
    visible_studios = []
    for studio in data.get("studios") or []:
        devices = [d for d in studio.get("devices") or [] if str(d.get("node_id") or d.get("current_scout_id") or "") in owned_ids]
        if str(studio.get("owner_user_id") or "") != str(sess.get("user_id") or ""):
            continue
        visible_studios.append({**studio, "devices": devices, "device_count": len(devices)})
    data["studios"] = visible_studios
    data["scouts"] = scouts
    data["scout_count"] = len(scouts)
    data["studio_count"] = len(data.get("studios") or [])
    device_sns = set()
    for row in scouts:
        for dev in row.get("devices") or []:
            if dev.get("sn"):
                device_sns.add(dev["sn"])
    for studio in data.get("studios") or []:
        for dev in studio.get("devices") or []:
            if dev.get("sn"):
                device_sns.add(dev["sn"])
    data["device_count"] = len(device_sns)
    return ok(data)


@router.post("/nodes/install-token")
def create_install_token(request: Request, response: Response, sess: dict = Depends(current_session)):
    if is_development():
        from mino_nexus.api.scout_install import development_installation
        # Validate the source checkout before issuing any credential. No remote
        # release lookup, production bootstrap or copying production identities.
        development_installation("")
        tok = issue(user_id=str(sess.get("user_id") or ""))
        response.headers["Cache-Control"] = "no-store"
        return ok({**tok, **development_installation(tok["token"])})
    base = public_nexus_url(request)
    release = installation_release()
    tok = issue(user_id=str(sess.get("user_id") or ""))
    response.headers["Cache-Control"] = "no-store"
    return ok({**tok, "nexus_url": base, "targets": release["targets"],
               "commands": install_commands(release, token=tok["token"], nexus_url=base)})


@router.get("/nodes/installations/{install_id}")
def get_installation_status(install_id: str, sess: dict = Depends(current_session)):
    status = installation_status(install_id, user_id=str(sess.get("user_id") or ""))
    if status is None:
        raise HTTPException(status_code=404, detail="没有本次安装记录")
    node_id = status["node_id"]
    live = get_registry().get_node(node_id) if node_id else None
    status["online"] = bool(live and live.alive and not live.draining)
    status["hostname"] = live.hostname if live else str((stored_node(node_id) or {}).get("hostname") or "")
    return ok(status)


async def _close_node_connection(node_id: str, *, reason: str = "节点凭证已吊销") -> None:
    live = get_registry().get_node(node_id)
    connection = getattr(live.send, "__self__", None) if live else None
    if connection is not None and hasattr(connection, "ws"):
        connection.close()
        await connection.ws.close(code=1008, reason=reason)


@router.post("/nodes/{node_id}/revoke-token")
async def revoke_node_token(node_id: str, sess: dict = Depends(current_session)):
    nid = sanitize_id(node_id)
    row = stored_node(nid)
    if row is None or not node_visible(row, sess):
        raise HTTPException(status_code=404, detail="没有这个节点")
    revoke_node_credential(nid)
    await _close_node_connection(nid)
    return ok({"node_id": nid, "revoked": True})


@router.patch("/nodes/{node_id}")
def patch_node(
    node_id: str,
    body: NodePatchBody,
    sess: dict = Depends(current_session),
):
    nid = sanitize_id(node_id)
    _node_row_for_session(nid, studio_id="", sess=sess)
    # 本轮没有节点转移/共享流程，不能通过 PATCH 继承旧账号本地社交凭据。
    if body.owner_user_id is not None and body.owner_user_id != str(sess.get("user_id") or ""):
        raise HTTPException(status_code=409, detail="本版本不支持节点转移账号")
    if body.studio_id is not None:
        raise HTTPException(status_code=409, detail="本版本不支持变更节点关联")
    return ok({"node": stored_node(nid)})


@router.post("/nodes/{node_id}/claim")
async def claim_legacy_node(node_id: str, sess: dict = Depends(current_session)):
    from mino_nexus.resources.node_claim import claim
    nid = sanitize_id(node_id)
    claim(nid, sess)
    # Re-register with the persisted credential owner; previous plugin state is
    # not accepted as proof of an account identity.
    live = get_registry().get_node(nid)
    if live is not None:
        live.owner_user_id = str(sess["user_id"])
    await _close_node_connection(nid, reason="历史节点已认领，请重新注册")
    return ok({"node": stored_node(nid)})



def _node_row_for_session(
    node_id: str,
    *,
    studio_id: str,
    sess: dict,
) -> dict[str, Any]:
    snap = stored_node(node_id) or {"node_id": node_id}
    live = get_registry().get_node(node_id)
    row = {**(snap or {}), "node_id": node_id}
    if live is not None:
        row["studio_id"] = live.studio_id or row.get("studio_id") or ""
        row["owner_user_id"] = live.owner_user_id or row.get("owner_user_id") or ""
    if not node_visible(row, sess, studio_id=studio_id):
        raise HTTPException(status_code=404, detail="没有这个节点")
    return row


@router.get("/nodes/{node_id}/workload")
def node_workload(
    node_id: str,
    studio_id: str = Query(default=""),
    sess: dict = Depends(current_session),
):
    nid = sanitize_id(node_id)
    if not nid:
        raise HTTPException(status_code=400, detail="node_id 无效")
    _node_row_for_session(nid, studio_id=studio_id, sess=sess)
    body = workload_for_node_id(nid)
    if body is None:
        raise HTTPException(status_code=404, detail="没有这个节点")
    return ok(body)


@router.get("/nodes/{node_id}/logs")
async def node_logs(
    node_id: str,
    lines: int = Query(default=200, ge=1, le=2000),
    studio_id: str = Query(default=""),
    sess: dict = Depends(current_session),
):
    nid = sanitize_id(node_id)
    if not nid:
        raise HTTPException(status_code=400, detail="node_id 无效")
    _node_row_for_session(nid, studio_id=studio_id, sess=sess)
    live = get_registry().get_node(nid)
    if live is None or not live.alive or live.send is None:
        raise HTTPException(status_code=409, detail="节点离线，无法拉日志")

    payload = P.Execute(
        run_id="",
        step_idx=-1,
        sn="",
        capability_id="node.log_tail",
        params={"lines": int(lines)},
        timeout_sec=30.0,
    )
    result = live.send(P.MsgType.EXECUTE, payload, timeout=35.0)
    if inspect.isawaitable(result):
        result = await result
    if result is None:
        raise HTTPException(status_code=504, detail="节点未应答")
    raw = dict(getattr(result, "raw_response", None) or {})
    logs = raw.get("logs") or raw
    status = getattr(getattr(result, "status", None), "value", None) or getattr(result, "status", "")
    ok_flag = str(status).lower() in ("pass", "passed", "ok", "success")
    if not ok_flag:
        raise HTTPException(
            status_code=502,
            detail=getattr(result, "error", "") or getattr(result, "summary", "") or "拉日志失败",
        )
    return ok({"node_id": nid, "logs": logs, "summary": getattr(result, "summary", "") or ""})


def _require_live_node(node_id: str, sess: dict, studio_id: str):
    snap = stored_node(node_id) or {"node_id": node_id}
    live = get_registry().get_node(node_id)
    row = {**(snap or {}), "node_id": node_id}
    if live is not None:
        row["studio_id"] = live.studio_id or row.get("studio_id") or ""
        row["owner_user_id"] = live.owner_user_id or row.get("owner_user_id") or ""
    if not node_visible(row, sess, studio_id=studio_id):
        raise HTTPException(status_code=404, detail="没有这个节点")
    if live is None or not live.alive or live.send is None:
        raise HTTPException(status_code=409, detail="节点离线，无法下发")
    return live


async def _relay_plugin(
    node_id: str,
    live: Any,
    *,
    capability_id: str,
    params: dict[str, Any],
    timeout: float,
) -> dict[str, Any]:
    payload = P.Execute(
        run_id="",
        step_idx=-1,
        sn="",
        capability_id=capability_id,
        params=params,
        timeout_sec=timeout,
    )
    result = live.send(P.MsgType.EXECUTE, payload, timeout=timeout + 30.0)
    if inspect.isawaitable(result):
        result = await result
    if result is None:
        raise HTTPException(status_code=504, detail="节点未应答")
    status = getattr(getattr(result, "status", None), "value", None) or getattr(result, "status", "")
    summary = str(getattr(result, "summary", "") or "")
    error = str(getattr(result, "error", "") or "")
    data = dict(getattr(result, "data", None) or {})
    extra = dict(getattr(result, "extra", None) or {})
    plugins = data.get("plugins") if isinstance(data.get("plugins"), list) else extra.get("plugins")
    if isinstance(plugins, list):
        get_registry().note_plugins(node_id, plugins)
    if str(status).lower() not in ("pass", "passed", "ok", "success"):
        raise HTTPException(status_code=502, detail=error or summary or "节点拒绝了插件操作")
    configured = data.get("configured")
    if configured is None:
        configured = extra.get("configured")
    hidden = ("secret", "password", "webhook", "bot_token", "app_secret", "plugin_secret")
    public: dict[str, Any] = {}
    for key, value in {**extra, **data}.items():
        name = str(key)
        if name in ("plugins", "command") or any(flag in name for flag in hidden):
            continue
        public[name] = value
    return {
        "node_id": node_id,
        "kind": str(params.get("class") or params.get("kind") or ""),
        "plugin_id": str(params.get("id") or ""),
        "status": status,
        "summary": summary,
        "configured": bool(configured),
        "data": public,
    }


def _config_transfer_node(node_id: str, sess: dict):
    live = _require_live_node(node_id, sess, "")
    if not sess.get("user_id") or live.owner_user_id != sess["user_id"]:
        raise HTTPException(status_code=404, detail="没有这个节点")
    if live.plugins_ready is not True:
        raise HTTPException(status_code=409, detail="节点尚未就绪，无法迁移配置")
    return live


@router.post("/nodes/{node_id}/configuration/export")
async def export_node_configuration(node_id: str, response: Response, sess: dict = Depends(current_session)):
    live = _config_transfer_node(node_id, sess)
    out = await _relay_plugin(node_id, live, capability_id="node.config_export", params={}, timeout=60.0)
    if not isinstance(out["data"].get("archive"), str):
        raise HTTPException(status_code=502, detail="Scout 未返回配置文件，请升级 Scout")
    response.headers["Cache-Control"] = "no-store"
    return ok(out["data"])


@router.post("/nodes/{node_id}/configuration/import")
async def import_node_configuration(node_id: str, body: NodeConfigImportBody, response: Response, sess: dict = Depends(current_session)):
    live = _config_transfer_node(node_id, sess)
    if not body.overwrite:
        raise HTTPException(status_code=400, detail="请明确确认覆盖目标节点配置")
    out = await _relay_plugin(node_id, live, capability_id="node.config_import", params={"archive": body.archive}, timeout=60.0)
    response.headers["Cache-Control"] = "no-store"
    return ok(out["data"])


@router.post("/nodes/{node_id}/plugins/config")
async def config_node_plugin(
    node_id: str,
    body: NodePluginConfigBody,
    studio_id: str = Query(default=""),
    sess: dict = Depends(current_session),
):
    _node_row_for_session(node_id, studio_id=studio_id, sess=sess)
    kind = str(body.kind or "").strip()
    plugin_id = str(body.plugin_id or "").strip()
    if not kind or not plugin_id:
        raise HTTPException(status_code=400, detail="缺少插件分类或 id")
    live = _require_live_node(node_id, sess, studio_id)
    values = {str(k): str(v) for k, v in (body.values or {}).items() if str(k).strip()}
    cleared = [str(x).strip() for x in (body.clear or []) if str(x).strip()]
    out = await _relay_plugin(
        node_id,
        live,
        capability_id="node.plugin_config",
        params={"class": kind, "id": plugin_id, "values": values, "clear": cleared},
        timeout=30.0,
    )
    return ok(out)


_PLUGIN_STATUS_KEYS = (
    'state', 'enabled', 'connected', 'connection_verified', 'connection_source',
    'holding', 'last_message_at', 'error', 'version', 'healthy', 'restarts',
)
_COMPONENT_KEYS = (
    'tool_id', 'label', 'purpose', 'state', 'version', 'installable', 'updatable',
    'removable', 'configured', 'plugin_class', 'plugin_id', 'detail',
    'runtime_environment', 'running_version', 'source_version', 'source_changed',
    'restart_required', 'required_by_config', 'config_key',
)


def _compact_plugin_status(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    out = {key: raw[key] for key in _PLUGIN_STATUS_KEYS if key in raw}
    if isinstance(raw.get('platforms'), dict):
        out['platforms'] = {
            str(platform): _compact_plugin_status(value)
            for platform, value in raw['platforms'].items()
            if isinstance(value, dict)
        }
    return out


def _compact_platforms(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    return {
        str(platform): _compact_plugin_status(value)
        for platform, value in raw.items() if isinstance(value, dict)
    }


def _compact_component(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    return {key: raw[key] for key in _COMPONENT_KEYS if key in raw}


def _compact_task(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    keys = ('task_id', 'operation_id', 'tool_id', 'action', 'state', 'active', 'stage',
            'label', 'percent', 'error', 'created_at', 'updated_at')
    return {key: raw[key] for key in keys if key in raw}


def _plugin_status_row(row: dict[str, Any], tools: dict[str, Any] | None, tools_error: str) -> dict[str, Any]:
    plugin_tool_ids = {'cli.feishu', 'cli.meego', 'langbot'}
    components = [
        _compact_component(item) for item in (tools or {}).get('items', [])
        if isinstance(item, dict) and item.get('tool_id') in plugin_tool_ids
    ]
    by_plugin = {
        (item.get('plugin_class'), item.get('plugin_id')): item
        for item in components if item.get('plugin_class') and item.get('plugin_id')
    }
    plugins = []
    raw_plugins = row.get('plugins') if isinstance(row.get('plugins'), list) else None
    for raw in raw_plugins or []:
        if not isinstance(raw, dict):
            continue
        kind = str(raw.get('class') or '')
        plugin_id = str(raw.get('id') or '')
        if not kind or not plugin_id:
            continue
        component = by_plugin.get((kind, plugin_id), {})
        installed = bool(raw.get('installed'))
        entry = {
            'class': kind,
            'id': plugin_id,
            'label': str(raw.get('label') or plugin_id),
            'installed': installed,
            'configured': bool(raw.get('configured')),
            'import_blocked': bool(raw.get('import_blocked')),
            'installable': component.get('installable'),
            'updatable': component.get('updatable'),
            'removable': component.get('removable'),
            'install_state': component.get('state') or ('installed' if installed else 'missing'),
            'version': component.get('version'),
            'detail': component.get('detail') or ('' if installed else '需要安装后才能保存配置'),
            'status': _compact_plugin_status(raw.get('status')),
        }
        platforms = _compact_platforms(raw.get('platforms'))
        if platforms:
            entry['platforms'] = platforms
        plugins.append(entry)
    return {
        'node_id': str(row.get('node_id') or ''),
        'hostname': str(row.get('hostname') or ''),
        'online': bool(row.get('online')),
        'plugins_known': raw_plugins is not None,
        'plugins_ready': row.get('plugins_ready'),
        'plugin_warning': str(row.get('plugin_warning') or ''),
        'plugin_job': dict(row.get('plugin_job') or {}) if isinstance(row.get('plugin_job'), dict) else {},
        'plugins': plugins,
        'components': components,
        'tasks': [_compact_task(task) for task in (tools or {}).get('tasks', [])
                  if isinstance(task, dict) and task.get('tool_id') in plugin_tool_ids],
        'tools_checked': tools is not None,
        'tools_supported': bool((tools or {}).get('supported')),
        'tools_contract_version': int((tools or {}).get('contract_version') or 0),
        'tools_stale': bool((tools or {}).get('stale')),
        'tools_error': tools_error,
    }


@router.get('/plugins/status')
async def plugin_status(node_id: str = Query(default=''), sess: dict = Depends(current_session)):
    """插件安装/配置状态；指定 node_id 时同时刷新下载中心的安装项。只读。"""
    from mino_nexus.resources import node_tools as service

    rows = filter_nodes(ui_nodes(), sess, studio_id='')
    if node_id:
        nid = sanitize_id(node_id)
        rows = [row for row in rows if str(row.get('node_id') or '') == nid] if nid else []
        if not rows:
            raise HTTPException(status_code=404, detail='没有这个节点')
    uid = str(sess.get('user_id') or '')
    items = []
    for row in rows:
        nid = str(row.get('node_id') or '')
        tools = None
        tools_error = ''
        # node_tools 只允许 owner 读取安装合同；管理员查看他人节点时只返回插件快照。
        if node_id and nid and str(row.get('owner_user_id') or '') == uid:
            try:
                tools = await service.tools(nid, uid)
            except HTTPException as exc:
                tools_error = str(exc.detail)
            except Exception:
                tools_error = '读取下载中心状态失败'
        items.append(_plugin_status_row(row, tools, tools_error))
    return ok({'items': items, 'count': len(items)})


@router.post('/channel-programs/install', status_code=202)
async def install_channel_program(body: ChannelProgramBody, sess: dict = Depends(current_session)):
    """安装或更新渠道程序。请求转给该节点下载中心，Nexus 不下载安装包。"""
    import re
    import uuid
    from mino_nexus.resources import node_tools as service

    if body.tool_id not in service.CHANNEL_PROGRAMS:
        raise HTTPException(status_code=422, detail='unsupported_tool_action')
    operation_id = str(body.operation_id or '').strip() or uuid.uuid4().hex
    if not re.fullmatch(r'[A-Za-z0-9_-]{8,100}', operation_id):
        raise HTTPException(status_code=422, detail='operation_id 必须为 8-100 位字母、数字、下划线或短横线')
    task = await service.submit(str(body.node_id), str(sess.get('user_id') or ''), operation_id, body.tool_id, body.action)
    return ok({
        'task_id': str(task.get('task_id') or ''),
        'state': str(task.get('state') or ''),
        'active': bool(task.get('active')),
        'tool_id': body.tool_id,
        'action': body.action,
        'task': task,
    })


@router.post('/plugins/install', status_code=202)
async def install_plugin(body: PluginInstallBody, sess: dict = Depends(current_session)):
    """提交插件安装/更新任务；真正下载和安装由 Scout 下载中心执行。"""
    import asyncio
    import re
    import time
    import uuid
    from mino_nexus.resources import node_tools as service

    operation_id = str(body.operation_id or '').strip() or uuid.uuid4().hex
    if not re.fullmatch(r'[A-Za-z0-9_-]{8,100}', operation_id):
        raise HTTPException(status_code=422, detail='operation_id 必须为 8-100 位字母、数字、下划线或短横线')
    task = await service.submit(str(body.node_id), str(sess.get('user_id') or ''), operation_id,
                                body.plugin, body.action)
    deadline = time.monotonic() + body.wait_ms / 1000.0
    while body.wait_ms and task.get('state') in service.ACTIVE:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        await asyncio.sleep(min(1.0, remaining))
        try:
            task = await service.refresh_task(str(body.node_id), str(sess.get('user_id') or ''), str(task['task_id']))
        except Exception:
            # 任务已提交，轮询失败不能把已完成/进行中的安装变成请求失败。
            break
    state = str(task.get('state') or '')
    if state == 'succeeded':
        message = '插件安装完成' if body.action == 'install' else '插件更新完成'
    elif state == 'failed':
        message = '插件任务失败，请查看任务 error'
    elif state == 'unknown':
        message = '插件任务结果待确认，请按 task_id 查询'
    else:
        message = '已提交安装任务' if body.action == 'install' else '已提交更新任务'
    return ok({
        'task_id': str(task.get('task_id') or ''),
        'state': state,
        'active': bool(task.get('active')),
        'plugin': body.plugin,
        'action': body.action,
        'task': task,
    }, msg=message)


@router.post("/nodes/{node_id}/plugins/install")
async def install_node_plugin(
    node_id: str,
    body: NodePluginActionBody,
    studio_id: str = Query(default=""),
    sess: dict = Depends(current_session),
):
    _node_row_for_session(node_id, studio_id=studio_id, sess=sess)
    kind = str(body.kind or "").strip()
    plugin_id = str(body.plugin_id or "").strip()
    if kind not in ("cli", "mcp", "im"):
        raise HTTPException(status_code=400, detail="只有 CLI、MCP 和对话渠道（LangBot）需要安装")
    live = _require_live_node(node_id, sess, studio_id)
    out = await _relay_plugin(
        node_id,
        live,
        capability_id="node.plugin_install",
        params={"class": kind, "id": plugin_id},
        timeout=600.0,
    )
    return ok(out)


@router.post("/nodes/{node_id}/plugins/remove")
async def remove_node_plugin(
    node_id: str,
    body: NodePluginActionBody,
    studio_id: str = Query(default=""),
    sess: dict = Depends(current_session),
):
    _node_row_for_session(node_id, studio_id=studio_id, sess=sess)
    kind = str(body.kind or "").strip()
    plugin_id = str(body.plugin_id or "").strip()
    live = _require_live_node(node_id, sess, studio_id)
    out = await _relay_plugin(
        node_id,
        live,
        capability_id="node.plugin_remove",
        params={"class": kind, "id": plugin_id},
        timeout=120.0,
    )
    return ok(out)


_PLUGIN_CALLS = frozenset({
    "plugin.cli.feishu",
    "plugin.cli.meego",
    "plugin.bot.send",
    "plugin.bot.wechat",
})


@router.post("/nodes/{node_id}/plugins/call")
async def call_node_plugin(
    node_id: str,
    body: NodePluginCallBody,
    studio_id: str = Query(default=""),
    sess: dict = Depends(current_session),
):
    _node_row_for_session(node_id, studio_id=studio_id, sess=sess)
    cap = str(body.capability_id or "").strip()
    if cap not in _PLUGIN_CALLS:
        raise HTTPException(status_code=400, detail="不支持的插件调用")
    kind = "cli" if cap.startswith("plugin.cli.") else "bot"
    plugin_id = cap.rsplit(".", 1)[-1]
    if cap == "plugin.bot.send":
        plugin_id = str((body.params or {}).get("kind") or "")
    if cap == "plugin.bot.wechat":
        plugin_id = "wechat"
        kind = "im"
    live = _require_live_node(node_id, sess, studio_id)
    params = dict(body.params or {})
    timeout = 90.0 if cap == "plugin.cli.feishu" else 50.0
    out = await _relay_plugin(
        node_id,
        live,
        capability_id=cap,
        params=params,
        timeout=timeout,
    )
    data = dict(out)
    return ok(data)


@router.post("/nodes/{node_id}/command")
async def command_node(
    node_id: str,
    body: NodeCommandBody,
    studio_id: str = Query(default=""),
    sess: dict = Depends(current_session),
):
    _node_row_for_session(node_id, studio_id=studio_id, sess=sess)
    cmd = str(body.command or "").strip().lower()
    if cmd == "update":
        raise HTTPException(409, "请使用固定版本更新接口，并提供 operation_id 和 build_id")
    if cmd == "start":
        raise HTTPException(status_code=400, detail="离线专机无法远程启动")
    if cmd not in _REMOTE_COMMANDS:
        raise HTTPException(status_code=400, detail="不支持的节点指令")

    snap = stored_node(node_id) or {"node_id": node_id}
    live = get_registry().get_node(node_id)
    row = {**(snap or {}), "node_id": node_id}
    if live is not None:
        row["studio_id"] = live.studio_id or row.get("studio_id") or ""
        row["owner_user_id"] = live.owner_user_id or row.get("owner_user_id") or ""
    if not node_visible(row, sess, studio_id=studio_id):
        raise HTTPException(status_code=404, detail="没有这个节点")

    if live is None or not live.alive or live.send is None:
        raise HTTPException(status_code=409, detail="节点离线，无法下发")

    if cmd == "update":
        update_timeout = 600.0
    else:
        update_timeout = 20.0
    payload = P.Execute(
        run_id="",
        step_idx=-1,
        sn="",
        capability_id=f"node.{cmd}",
        params={"command": cmd, "reason": str(body.reason or "studio")},
        timeout_sec=update_timeout,
    )
    result = live.send(P.MsgType.EXECUTE, payload, timeout=update_timeout + 30.0)
    if inspect.isawaitable(result):
        result = await result
    if result is None:
        hint = "节点未应答"
        if cmd == "update":
            hint = (
                "节点未应答（更新可能仍在下载/安装，或进程已退出未重连）。"
                "请到执行机查看 Scout 日志并执行 mino-scout start 或 launchctl kickstart；"
                "勿用 PATH 里旧的 pip mino-scout（无 update）。"
                "升级请用 Application Support 下 bin/mino-scout update 或 Studio 远程安装命令。"
            )
        raise HTTPException(status_code=504, detail=hint)
    extra: dict[str, Any] = dict(getattr(result, "extra", None) or {})
    status = getattr(getattr(result, "status", None), "value", None) or getattr(result, "status", "")
    if str(status).lower() in ("pass", "passed", "ok", "success") and cmd in ("sleep", "wake"):
        get_registry().note_host_mode(node_id, "asleep" if cmd == "sleep" else "running")
    return ok({
        "node_id": node_id,
        "command": cmd,
        "status": status,
        "summary": getattr(result, "summary", "") or "",
        "error": getattr(result, "error", "") or "",
        "extra": extra,
    })


@router.get("/nodes/{node_id}/update-status")
def scout_update_status(node_id: str, operation_id: str = "", sess: dict = Depends(current_session)):
    _node_row_for_session(node_id, studio_id="", sess=sess)
    from mino_nexus.resources.scout_updates import get
    live = get_registry().get_node(node_id)
    return ok({"node_id": node_id, "online": bool(live and live.alive),
        "running_version": live.scout_version if live and live.alive else None,
        "operation": get(node_id, operation_id)})


@router.post("/nodes/{node_id}/update")
async def update_scout(node_id: str, body: ScoutUpdateBody, sess: dict = Depends(current_session)):
    _node_row_for_session(node_id, studio_id="", sess=sess)
    from mino_nexus.resources.scout_updates import request
    if body.build_id.lower() == "latest" or body.release_tag.lower() == "latest":
        raise HTTPException(422, "必须指定固定 release tag/build_id")
    if (body.action == "rollback") != bool(body.rollback_of):
        raise HTTPException(422, "rollback 动作必须指定原成功 operation_id")
    params = body.model_dump(exclude={"operation_id"})
    return ok(request(node_id, str(sess["user_id"]), body.operation_id, params))


@router.post("/nodes/{node_id}/updates/{operation_id}/cancel")
def cancel_scout_update(node_id: str, operation_id: str, sess: dict = Depends(current_session)):
    _node_row_for_session(node_id, studio_id="", sess=sess)
    from mino_nexus.resources.scout_updates import cancel
    return ok(cancel(node_id, operation_id))


class NodeToolTaskBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,100}$")
    tool_id: str = Field(pattern=r"^(cli\.(feishu|meego)|langbot|chromium|adb\.keyboard|appium)$")
    action: str = Field(pattern=r"^(install|update)$")


@router.get("/nodes/{node_id}/tools")
async def node_tools(node_id: str, sess: dict = Depends(current_session)):
    from mino_nexus.resources import node_tools as service
    return ok(await service.tools(node_id, str(sess.get("user_id") or "")))


@router.post("/nodes/{node_id}/tool-tasks", status_code=202)
async def create_node_tool_task(node_id: str, body: NodeToolTaskBody, sess: dict = Depends(current_session)):
    from mino_nexus.resources import node_tools as service
    return ok(await service.submit(node_id, str(sess.get("user_id") or ""), body.operation_id, body.tool_id, body.action))


@router.get("/nodes/{node_id}/tool-tasks/{task_id}")
async def read_node_tool_task(node_id: str, task_id: str, sess: dict = Depends(current_session)):
    from mino_nexus.resources import node_tools as service
    return ok(await service.refresh_task(node_id, str(sess.get("user_id") or ""), task_id))


@router.get("/execution-version")
def execution_version(_session=Depends(current_session)):
    """Report only an actually imported execution module, never a disk VERSION."""
    import sys
    module = sys.modules.get("mino_nexus.loop.agent_loop")
    revision = getattr(module, "EXECUTION_REVISION", "") if module else ""
    return ok({"execution_module_loaded": module is not None, "execution_revision": revision,
               "resolved_contract_loaded": bool(module and "resolved_case" in module._run_loop.__code__.co_varnames)})
