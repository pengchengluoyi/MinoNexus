"""节点列表、归属过滤、Scout 安装凭证与节点指令（EXECUTE node.stop 等）。"""
from __future__ import annotations

import inspect
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from mino_nexus.services import auth_store
from mino_nexus.core import protocol as P
from mino_nexus.core.http_util import client_header, ok
from mino_nexus.services.node_registry import get_registry
from mino_nexus.services.node_store import filter_nodes, get_node as stored_node, node_visible
from mino_nexus.routers.deps import current_session, require_packs_writer
from mino_nexus.services.node_store import sanitize_id, upsert
from mino_nexus.services.runtime_tokens import issue
from mino_nexus.services.ui_devices import ui_assets, ui_devices, ui_nodes
from mino_nexus.services.node_workload import workload_for_node_id

router = APIRouter(prefix="/runtime", tags=["Runtime"])

_REMOTE_COMMANDS = frozenset({"stop", "restart", "update", "sleep", "wake"})


class NodeCommandBody(BaseModel):
    command: str
    reason: str = ""


class NodePatchBody(BaseModel):
    owner_user_id: str | None = None
    studio_id: str | None = None


class NodePluginConfigBody(BaseModel):
    kind: str
    plugin_id: str
    values: dict[str, Any] = {}
    clear: list[str] = []


class NodePluginActionBody(BaseModel):
    kind: str
    plugin_id: str


class NodePluginCallBody(BaseModel):
    capability_id: str
    params: dict[str, Any] = {}


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
    if client != "console":
        scouts = filter_nodes(scouts, sess, studio_id=studio_id)
        seen_studio = {str(row.get("studio_id") or "") for row in scouts if row.get("studio_id")}
        data["studios"] = [s for s in (data.get("studios") or []) if s.get("studio_id") in seen_studio]
    else:
        for row in scouts:
            uid = str(row.get("owner_user_id") or "")
            pub = auth_store.public_user_by_id(uid) if uid else None
            if pub:
                row["owner_name"] = pub.get("name") or pub.get("username") or ""
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
def create_install_token(request: Request, sess: dict = Depends(current_session)):
    tok = issue(user_id=str(sess.get("user_id") or ""))
    base = str(request.base_url).rstrip("/")
    return ok({**tok, "nexus_url": base})


@router.patch("/nodes/{node_id}")
def patch_node(
    node_id: str,
    body: NodePatchBody,
    sess: dict = Depends(current_session),
):
    require_packs_writer(sess)
    nid = sanitize_id(node_id)
    if not nid:
        raise HTTPException(status_code=400, detail="node_id 无效")
    snap = stored_node(nid)
    live = get_registry().get_node(nid)
    if snap is None and live is None:
        raise HTTPException(status_code=404, detail="没有这个节点")
    fields: dict[str, str] = {}
    if body.owner_user_id is not None:
        fields["owner_user_id"] = body.owner_user_id
    if body.studio_id is not None:
        fields["studio_id"] = body.studio_id
    if not fields:
        raise HTTPException(status_code=400, detail="至少提供 owner_user_id 或 studio_id")
    row = upsert(nid, **fields)
    if live is not None:
        if "owner_user_id" in fields:
            live.owner_user_id = sanitize_id(fields["owner_user_id"])
        if "studio_id" in fields:
            live.studio_id = sanitize_id(fields["studio_id"])
        live.persist_snapshot()
    return ok({"node": row})


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


@router.post("/nodes/{node_id}/plugins/config")
async def config_node_plugin(
    node_id: str,
    body: NodePluginConfigBody,
    studio_id: str = Query(default=""),
    sess: dict = Depends(current_session),
):
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


@router.post("/nodes/{node_id}/plugins/install")
async def install_node_plugin(
    node_id: str,
    body: NodePluginActionBody,
    studio_id: str = Query(default=""),
    sess: dict = Depends(current_session),
):
    kind = str(body.kind or "").strip()
    plugin_id = str(body.plugin_id or "").strip()
    if kind not in ("cli", "mcp"):
        raise HTTPException(status_code=400, detail="只有 CLI 和 MCP 需要安装")
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
    cap = str(body.capability_id or "").strip()
    if cap not in _PLUGIN_CALLS:
        raise HTTPException(status_code=400, detail="不支持的插件调用")
    kind = "cli" if cap.startswith("plugin.cli.") else "bot"
    plugin_id = cap.rsplit(".", 1)[-1]
    if cap == "plugin.bot.send":
        plugin_id = str((body.params or {}).get("kind") or "")
    if cap == "plugin.bot.wechat":
        plugin_id = "wechat"
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
    cmd = str(body.command or "").strip().lower()
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
