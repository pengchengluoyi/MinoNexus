"""REGISTER 时的 node_id 分配（与 install-token 归属冲突时重发号）。"""
from __future__ import annotations

import secrets

from mino_nexus.services.node_store import get_node, sanitize_id


def resolve_register_node_id(
    requested: str,
    *,
    is_install_token: bool,
    user_id: str,
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
        stored = get_node(nid) or {}
        prev_owner = sanitize_id(str(stored.get("owner_user_id") or ""))
        if prev_owner and prev_owner != owner:
            nid = secrets.token_hex(8)
            warnings.append(f"node_id 已被其他用户占用，已分配 {nid}")
    return nid, warnings
