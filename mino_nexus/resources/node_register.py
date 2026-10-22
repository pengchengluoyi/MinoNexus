"""REGISTER 时复用本机身份，归属冲突必须显式拒绝。"""
from __future__ import annotations

import secrets

from mino_nexus.resources.node_store import get_node, sanitize_id


def resolve_register_node_id(
    requested: str,
    *,
    is_install_token: bool,
    user_id: str,
    stored: dict | None = None,
) -> tuple[str, list[str]]:
    warnings: list[str] = []
    nid = sanitize_id(requested)
    if len(nid) < 8:
        prev = nid
        nid = secrets.token_hex(8)
        if prev:
            warnings.append(f"node_id 无效，已分配 {nid}")
        else:
            warnings.append(f"已分配 node_id={nid}")

    owner = sanitize_id(user_id)
    if is_install_token and owner:
        if stored is None:
            stored = get_node(nid) or {}
        prev_owner = sanitize_id(str(stored.get("owner_user_id") or ""))
        if prev_owner and prev_owner != owner:
            raise PermissionError("本机 Scout 已绑定其他账号，请由管理员处理归属后再安装")
        if stored and not prev_owner:
            raise PermissionError("历史节点尚未归属，请先由管理员认领")
    return nid, warnings
