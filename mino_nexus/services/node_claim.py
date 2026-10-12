"""Workbench administrator claim of a legacy node with no account owner."""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import text
from mino_nexus.core.database import session_scope
from mino_nexus.models.device import Device
from mino_nexus.models.node import Node
from mino_nexus.models.token import NodeCredential
from mino_nexus.services.node_store import is_admin


def claim(node_id: str, session: dict) -> None:
    if not is_admin(session):
        raise HTTPException(status_code=403, detail="只有管理员可以认领历史节点")
    uid = str(session.get("user_id") or "")
    if not uid:
        raise HTTPException(status_code=401, detail="请先登录")
    with session_scope() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        node = db.get(Node, node_id)
        if node is None:
            raise HTTPException(status_code=404, detail="没有这个节点")
        if node.owner_user_id:
            raise HTTPException(status_code=409, detail="节点已有账号归属，不能重新认领")
        credential = db.get(NodeCredential, node_id)
        if credential and credential.user_id and credential.user_id != uid:
            raise HTTPException(status_code=409, detail="节点凭证已有账号归属，不能认领")
        node.owner_user_id = uid
        node.extra = {**dict(node.extra or {}), "owner_user_id": uid}
        if credential:
            credential.user_id = uid
        for device in db.query(Device).filter_by(node_id=node_id):
            if not device.owner_user_id:
                device.owner_user_id = uid
                device.extra = {**dict(device.extra or {}), "owner_user_id": uid}
