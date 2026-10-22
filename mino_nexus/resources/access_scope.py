"""用户节点/设备权限：无归属或外部账号统一视为不存在。"""
from __future__ import annotations

from fastapi import HTTPException
from mino_nexus.platform.identity import current_user_id


def node_owner_id(node_id: str) -> str:
    from mino_nexus.resources.node_registry import get_registry
    from mino_nexus.resources.node_store import get_node
    live = get_registry().get_node(node_id)
    if live is not None:
        return str(live.owner_user_id or "")
    return str((get_node(node_id) or {}).get("owner_user_id") or "")


def require_owned_node(node_id: str) -> str:
    uid = current_user_id()
    if not uid:
        raise HTTPException(status_code=401, detail="请先登录")
    if node_owner_id(node_id) != uid:
        raise HTTPException(status_code=404, detail="没有这个节点")
    return uid


def device_owner_id(sn: str, node_id: str = "") -> str:
    from mino_nexus.resources.node_registry import get_registry
    from mino_nexus.resources.device_store import get_device
    stored = get_device(sn) or {}
    live, _ = get_registry().resolve(sn)
    nid = node_id or (live.node_id if live is not None else str(stored.get("current_scout_id") or ""))
    uid = node_owner_id(nid)
    recorded_owner = str(stored.get("owner_user_id") or "")
    return uid if uid and (not recorded_owner or recorded_owner == uid) else ""


def require_owned_device(sn: str) -> str:
    uid = current_user_id()
    if not uid:
        raise HTTPException(status_code=401, detail="请先登录")
    if device_owner_id(sn) != uid:
        raise HTTPException(status_code=404, detail="没有这个设备")
    return uid
