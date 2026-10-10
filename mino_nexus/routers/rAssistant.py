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

    default_app_id: Optional[str] = None
    default_sn: Optional[str] = None
    persona: Optional[str] = None
    enabled: Optional[bool] = None


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


def _im_nodes() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for node in get_registry().nodes():
        channels = []
        for row in node.plugins or []:
            if row.get("class") not in ("bot", "im"):
                continue
            st = row.get("status")
            if not isinstance(st, dict):
                continue
            channels.append({
                "channel": str(row.get("id") or ""),
                "connected": bool(st.get("connected")),
                "last_message_at": int(st.get("last_message_at") or 0),
            })
        if channels:
            out.append({
                "node_id": node.node_id,
                "name": node.hostname or node.node_id,
                "alive": bool(node.alive),
                "channels": channels,
            })
    return out


@router.get("/status")
def status(request: Request, sess: dict = Depends(current_session)):
    uid = _uid(sess)
    nodes = _im_nodes()
    online = any(n["alive"] and any(c["connected"] for c in n["channels"]) for n in nodes)
    return ok({
        "enabled": store.get_profile(uid)["enabled"],
        "online": online,
        "mcp_url": _mcp_url(request),
        "im_nodes": nodes,
    })


def _profile_view(uid: str) -> dict[str, Any]:
    prof = store.get_profile(uid)
    app_name = ""
    if prof["default_app_id"]:
        try:
            from mino_nexus.services import project_store as ps

            app_name = str((ps.find_app(prof["default_app_id"]) or {}).get("name") or "")
        except Exception:
            app_name = ""
    device_name = ""
    if prof["default_sn"]:
        try:
            from mino_nexus.services.ui_devices import ui_devices

            for d in ui_devices():
                if str(d.get("sn") or "") == prof["default_sn"]:
                    device_name = str(d.get("model") or d.get("sn") or "")
                    break
        except Exception:
            device_name = ""
    return {
        "user_id": uid,
        "persona": prof["persona"],
        "default_app_id": prof["default_app_id"],
        "default_app_name": app_name,
        "default_sn": prof["default_sn"],
        "default_device_name": device_name,
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
def bind_code(sess: dict = Depends(current_session)):
    return ok(store.create_bind_code(_uid(sess)))


@router.get("/identities")
def identities(sess: dict = Depends(current_session)):
    return ok({"items": store.list_identities(_uid(sess))})


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
    sess: dict = Depends(current_session),
):
    return ok(store.list_turns(_uid(sess), limit=limit, offset=offset, surface=surface))


@router.get("/turns/{turn_id}")
def turn_detail(turn_id: str, sess: dict = Depends(current_session)):
    row = store.get_turn(_uid(sess), turn_id)
    if row is None:
        raise HTTPException(status_code=404, detail="轨迹不存在")
    return ok({k: v for k, v in row.items() if not k.startswith("_")})


@router.get("/turns/{turn_id}/messages")
def turn_messages(turn_id: str, sess: dict = Depends(current_session)):
    """正文只在 Scout 本地库。这里经 plugin.im.history 现取，Nexus 不缓存。"""
    row = store.get_turn(_uid(sess), turn_id)
    if row is None:
        raise HTTPException(status_code=404, detail="轨迹不存在")
    channel, chat_id, node_id = row["_channel"], row["_chat_id"], row["_node_id"]
    if row["surface"] != "im" or not channel or not chat_id:
        return ok({"available": False, "reason": "这条记录没有对话正文", "messages": []})
    from mino_nexus.services.node_plugin_call import PluginCallError, call_plugin

    try:
        data = call_plugin(
            kind="bot",
            plugin_id=channel,
            node_id=node_id,
            capability_id="plugin.im.history",
            params={"channel": channel, "chat_id": chat_id, "limit": 40},
            timeout=15,
        )
    except PluginCallError:
        return ok({"available": False, "reason": "节点离线，正文暂不可见", "messages": []})
    msgs = data.get("messages") if isinstance(data.get("messages"), list) else []
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
def subscriptions(sess: dict = Depends(current_session)):
    return ok({"items": store.list_subscriptions(_uid(sess))})


@router.delete("/subscriptions/{sub_id}")
def delete_subscription(sub_id: int, sess: dict = Depends(current_session)):
    if not store.delete_subscription(_uid(sess), sub_id):
        raise HTTPException(status_code=404, detail="订阅不存在")
    return ok({"id": sub_id})


@router.post("/im-hold")
def set_im_hold(body: ImHoldBody, sess: dict = Depends(current_session)):
    """手动指定某个对话渠道由哪台节点持有，其余节点让出。只有管理员能改。"""
    from mino_nexus.services import auth_store
    from mino_nexus.services import im_hold

    if not auth_store.is_admin_role(str(sess.get("role") or "")):
        raise HTTPException(status_code=403, detail="需要管理员才能改持有节点")
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
