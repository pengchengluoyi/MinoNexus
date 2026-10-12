"""用户实时通道：认证连接、归属过滤、吊销后不再发送。"""
from __future__ import annotations

import asyncio
import json
import platform
from typing import Any, Optional

from fastapi import Query, WebSocket, WebSocketDisconnect

from mino_nexus.services import auth_store
from mino_nexus.core.log import SLog
from mino_nexus.core.mdns import http_origin, mdns_status, public_urls
from mino_nexus.services.node_registry import get_registry
from mino_nexus.services.ui_devices import ui_devices, ui_nodes
from mino_nexus.websocket.node import NEXUS_VERSION

TAG = "Observers"

_CLIENTS: dict[WebSocket, tuple[str, str]] = {}
_LOOP: Optional[asyncio.AbstractEventLoop] = None


def set_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _LOOP
    _LOOP = loop


def broadcast_sync(payload: dict[str, Any]) -> None:
    """从执行线程把 agent_step / testing_task 推给 Studio。"""
    from mino_nexus.core.execution_user import current_user_id
    payload = dict(payload)
    payload["_execution_owner"] = current_user_id()
    loop = _LOOP
    if loop is None or not loop.is_running():
        return
    try:
        asyncio.run_coroutine_threadsafe(_broadcast(payload), loop)
    except Exception as exc:
        SLog.d(TAG, f"broadcast_sync failed: {exc}")


def _event_owner(payload: dict[str, Any]) -> str:
    from mino_nexus.services import run_store
    from mino_nexus.services.access_scope import node_owner_id, device_owner_id
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    rid = str(data.get("task_id") or data.get("run_id") or "").split("::", 1)[0]
    if rid:
        doc = run_store.get(rid)
        return str((doc or {}).get("owner_user_id") or "")
    if data.get("node_id"):
        return node_owner_id(str(data["node_id"]))
    if data.get("sn"):
        return device_owner_id(str(data["sn"]))
    return str(payload.get("_execution_owner") or "")


async def _broadcast(payload: dict[str, Any]) -> None:
    owner = _event_owner(payload)
    if not owner:
        return  # 无法确定归属的执行事件不能广播。
    public = {k: v for k, v in payload.items() if k != "_execution_owner"}
    text = json.dumps(public, ensure_ascii=False, default=str)
    for ws, (token, uid) in list(_CLIENTS.items()):
        try:
            sess = auth_store.session_of(token)
            if not sess or sess.get("user_id") != uid:
                _CLIENTS.pop(ws, None)
                await ws.close(code=1008, reason="登录已失效")
                continue
            if owner == uid:
                await ws.send_text(text)
        except Exception:
            _CLIENTS.pop(ws, None)


def _reply(req_id: str, action: str, data: Any = None, *, code: int = 200, msg: str = "") -> dict[str, Any]:
    body: dict[str, Any] = {"req_id": req_id, "action": action, "code": code, "ok": code == 200}
    if msg:
        body["msg"] = msg
    if data is not None:
        body["data"] = data
    return body


def _server_info() -> dict[str, Any]:
    from mino_nexus.core.execution_user import current_user_id
    nodes = [n for n in get_registry().nodes() if n.owner_user_id == current_user_id()]
    urls = public_urls()
    return {
        "hostname": platform.node(),
        "service": "MinoNexus",
        "version": NEXUS_VERSION,
        "role": "nexus",
        "lan_host": urls["lan_host"],
        "http_url": urls["http_url"],
        "node_ws_url": urls["node_ws_url"],
        "candidate_urls": [http_origin()],
        "token": "",
        "mdns": mdns_status(),
        "node_count": len(nodes),
        "nodes_alive": sum(1 for n in nodes if n.alive),
    }


def _node_status(sess: dict[str, Any]) -> dict[str, Any]:
    from mino_nexus.services.node_store import filter_nodes
    nodes = filter_nodes(ui_nodes(), sess)
    return {"role": "nexus", "connected": True, "is_master": True, "candidates": [], "sn": "",
            "node_count": len(nodes), "nodes_alive": sum(bool(n.get("online")) for n in nodes), "nodes": nodes}


def handle_action(action: str, payload: dict[str, Any], sess: dict[str, Any]) -> dict[str, Any]:
    if action == "get_server_info":
        return {"code": 200, "data": _server_info()}
    if action == "get_node_status":
        return {"code": 200, "data": _node_status(sess)}
    if action == "get_device_list":
        return {"code": 200, "data": ui_devices()}
    if action in ("join_cluster", "leave_cluster", "update_server_config"):
        return {"code": 400, "msg": "本版本不支持集群配网。Nexus 地址在构建时写死。"}
    return {"code": 404, "msg": f"未实现的动作：{action}"}


def register_routes(app: Any) -> None:
    @app.websocket("/ws")
    async def ui_endpoint(websocket: WebSocket, token: str = Query(default="")) -> None:
        await websocket.accept()
        sess = auth_store.session_of(token)
        if not sess or not sess.get("user_id"):
            await websocket.close(code=1008, reason="请先登录")
            return
        uid = str(sess["user_id"])
        _CLIENTS[websocket] = (token, uid)
        SLog.i(TAG, f"UI 连入 logged_in={bool(sess)}")
        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    await websocket.send_text(json.dumps({"code": 400, "msg": "无效 JSON"}, ensure_ascii=False))
                    continue
                if not isinstance(msg, dict):
                    continue
                req_id = str(msg.get("req_id") or "")
                action = str(msg.get("action") or msg.get("type") or "")
                sess = auth_store.session_of(token)
                if not sess or str(sess.get("user_id") or "") != uid:
                    await websocket.close(code=1008, reason="登录已失效")
                    break
                from mino_nexus.core.execution_user import execution_user
                with execution_user(uid):
                    result = handle_action(action, msg, sess)
                await websocket.send_text(json.dumps(
                    _reply(req_id, action, result.get("data"), code=int(result.get("code") or 200), msg=str(result.get("msg") or "")),
                    ensure_ascii=False,
                ))
        except WebSocketDisconnect:
            SLog.i(TAG, "UI 断开")
        except Exception as exc:
            SLog.e(TAG, f"UI 连接异常: {type(exc).__name__}: {exc}")
        finally:
            _CLIENTS.pop(websocket, None)
