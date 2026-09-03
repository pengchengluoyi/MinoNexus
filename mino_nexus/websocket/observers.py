"""UI 观察者：`WS /ws`。登录页会连，没有 token 也要收下，否则一直重连。"""
from __future__ import annotations

import json
import platform
from typing import Any

from fastapi import Query, WebSocket, WebSocketDisconnect

from mino_nexus import auth_store
from mino_nexus.log import SLog
from mino_nexus.mdns import http_origin, mdns_status, public_urls
from mino_nexus.node_registry import get_registry
from mino_nexus.ui_devices import ui_devices, ui_nodes
from mino_nexus.websocket.node import NEXUS_VERSION

TAG = "Observers"


def _reply(req_id: str, action: str, data: Any = None, *, code: int = 200, msg: str = "") -> dict[str, Any]:
    body: dict[str, Any] = {"req_id": req_id, "action": action, "code": code, "ok": code == 200}
    if msg:
        body["msg"] = msg
    if data is not None:
        body["data"] = data
    return body


def _server_info() -> dict[str, Any]:
    nodes = get_registry().nodes()
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


def _node_status() -> dict[str, Any]:
    nodes = get_registry().nodes()
    alive = sum(1 for n in nodes if n.alive)
    return {
        "role": "nexus",
        "connected": True,
        "is_master": True,
        "candidates": [],
        "sn": "",
        "node_count": len(nodes),
        "nodes_alive": alive,
        "nodes": ui_nodes(),
    }


def handle_action(action: str, payload: dict[str, Any]) -> dict[str, Any]:
    if action == "get_server_info":
        return {"code": 200, "data": _server_info()}
    if action == "get_node_status":
        return {"code": 200, "data": _node_status()}
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
                result = handle_action(action, msg)
                await websocket.send_text(json.dumps(
                    _reply(req_id, action, result.get("data"), code=int(result.get("code") or 200), msg=str(result.get("msg") or "")),
                    ensure_ascii=False,
                ))
        except WebSocketDisconnect:
            SLog.i(TAG, "UI 断开")
        except Exception as exc:
            SLog.e(TAG, f"UI 连接异常: {type(exc).__name__}: {exc}")
