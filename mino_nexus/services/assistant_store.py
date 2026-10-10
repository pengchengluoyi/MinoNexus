"""助手的数据层：身份绑定、助手档案、调用轨迹、订阅、MCP token。

不含任何「以某个用户身份执行」的逻辑；执行层单独设计。
"""
from __future__ import annotations

import hashlib
import secrets
import time
import uuid
from typing import Any, Optional

from mino_nexus.core.database import session_scope

BIND_CODE_TTL = 600
MCP_TOKEN_PREFIX = "mino_mcp_"
ASSISTANT_TOKEN_PREFIX = "mino_ast_"
TOKEN_PREFIXES = (MCP_TOKEN_PREFIX, ASSISTANT_TOKEN_PREFIX)


def is_api_token(value: str) -> bool:
    return str(value or "").startswith(TOKEN_PREFIXES)


def _now() -> int:
    return int(time.time())


def mask_id(value: str) -> str:
    v = str(value or "")
    if len(v) <= 4:
        return v[:1] + "***" if v else ""
    return v[:2] + "***" + v[-2:]


# ---------------- 绑定码 / 身份 ----------------


def create_bind_code(user_id: str) -> dict[str, Any]:
    from mino_nexus.models.assistant import ImBindCode

    now = _now()
    with session_scope() as db:
        db.query(ImBindCode).filter(ImBindCode.expires_at < now).delete()
        db.query(ImBindCode).filter(ImBindCode.user_id == user_id).delete()
        for _ in range(20):
            code = f"{secrets.randbelow(1_000_000):06d}"
            if not db.get(ImBindCode, code):
                break
        else:
            raise RuntimeError("绑定码生成失败，请重试")
        db.add(ImBindCode(code=code, user_id=user_id, expires_at=now + BIND_CODE_TTL))
    return {"code": code, "expires_in": BIND_CODE_TTL}


def consume_bind_code(code: str, *, channel: str, tenant: str, sender_id: str) -> Optional[str]:
    """码有效则把这个 IM 账号绑到码的主人，返回 user_id；无效返回 None。码只能用一次。"""
    from mino_nexus.models.assistant import ImBindCode, ImIdentity

    code = str(code or "").strip()
    now = _now()
    with session_scope() as db:
        row = db.get(ImBindCode, code)
        if row is None or int(row.expires_at or 0) < now:
            if row is not None:
                db.delete(row)
            return None
        user_id = row.user_id
        db.delete(row)
        exist = (
            db.query(ImIdentity)
            .filter(ImIdentity.channel == channel, ImIdentity.tenant == tenant, ImIdentity.sender_id == sender_id)
            .first()
        )
        if exist is not None:
            exist.user_id = user_id
            exist.bound_at = now
        else:
            db.add(ImIdentity(user_id=user_id, channel=channel, tenant=tenant, sender_id=sender_id, bound_at=now))
        return user_id


def bind_identity(user_id: str, *, channel: str, tenant: str, sender_id: str) -> str:
    """直接把一个 IM 账号绑到用户（不经绑定码）。已绑给别人则不覆盖，返回现有主人。"""
    from mino_nexus.models.assistant import ImIdentity

    with session_scope() as db:
        row = (
            db.query(ImIdentity)
            .filter(ImIdentity.channel == channel, ImIdentity.tenant == tenant, ImIdentity.sender_id == sender_id)
            .first()
        )
        if row is not None:
            return row.user_id
        db.add(ImIdentity(user_id=user_id, channel=channel, tenant=tenant, sender_id=sender_id, bound_at=_now()))
        return user_id


def find_identity(channel: str, tenant: str, sender_id: str) -> Optional[str]:
    from mino_nexus.models.assistant import ImIdentity

    with session_scope() as db:
        row = (
            db.query(ImIdentity)
            .filter(ImIdentity.channel == channel, ImIdentity.tenant == tenant, ImIdentity.sender_id == sender_id)
            .first()
        )
        return row.user_id if row else None


def list_identities(user_id: str) -> list[dict[str, Any]]:
    from mino_nexus.models.assistant import ImIdentity

    with session_scope() as db:
        rows = db.query(ImIdentity).filter(ImIdentity.user_id == user_id).order_by(ImIdentity.id).all()
        return [
            {
                "id": r.id,
                "channel": r.channel,
                "sender_id_masked": mask_id(r.sender_id),
                "tenant": r.tenant or "",
                "bound_at": int(r.bound_at or 0),
            }
            for r in rows
        ]


def delete_identity(user_id: str, identity_id: int) -> bool:
    from mino_nexus.models.assistant import ImIdentity

    with session_scope() as db:
        row = db.get(ImIdentity, identity_id)
        if row is None or row.user_id != user_id:
            return False
        db.delete(row)
        db.flush()
        left = db.query(ImIdentity.id).filter(ImIdentity.user_id == user_id).first()
    if left is None:
        revoke_assistant_token(user_id)
    return True


# ---------------- 助手档案 ----------------


def get_profile(user_id: str) -> dict[str, Any]:
    from mino_nexus.models.assistant import AssistantProfile

    with session_scope() as db:
        row = db.get(AssistantProfile, user_id)
        if row is None:
            return {
                "user_id": user_id, "persona": "", "default_app_id": "", "default_sn": "",
                "enabled": True, "project_id": "",
            }
        return {
            "user_id": row.user_id,
            "persona": row.persona or "",
            "default_app_id": row.default_app_id or "",
            "default_sn": row.default_sn or "",
            "enabled": row.enabled is not False,
            "project_id": row.project_id or "",
        }


def save_profile(user_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    from mino_nexus.models.assistant import AssistantProfile

    with session_scope() as db:
        row = db.get(AssistantProfile, user_id)
        if row is None:
            row = AssistantProfile(user_id=user_id)
            db.add(row)
        if "persona" in patch and patch["persona"] is not None:
            row.persona = str(patch["persona"])[:2000]
        if "default_app_id" in patch and patch["default_app_id"] is not None:
            row.default_app_id = str(patch["default_app_id"]).strip()
        if "default_sn" in patch and patch["default_sn"] is not None:
            row.default_sn = str(patch["default_sn"]).strip()
        if "enabled" in patch and patch["enabled"] is not None:
            row.enabled = bool(patch["enabled"])
    return get_profile(user_id)


# ---------------- 轨迹（不含正文） ----------------


def new_turn_id() -> str:
    return "t_" + uuid.uuid4().hex[:16]


def msg_seen(msg_key: str) -> bool:
    """IM 会重投消息。同一个 msg_key 已有轨迹就视为重复。"""
    from mino_nexus.models.assistant import AssistantTurn

    if not msg_key:
        return False
    with session_scope() as db:
        return db.query(AssistantTurn.turn_id).filter(AssistantTurn.msg_key == msg_key).first() is not None


def save_turn(turn: dict[str, Any]) -> None:
    from mino_nexus.models.assistant import AssistantTurn

    with session_scope() as db:
        db.merge(AssistantTurn(
            turn_id=turn["turn_id"],
            user_id=turn.get("user_id", ""),
            surface=turn.get("surface", "im"),
            channel=turn.get("channel", ""),
            chat_id=turn.get("chat_id", ""),
            node_id=turn.get("node_id", ""),
            msg_key=turn.get("msg_key", ""),
            status=turn.get("status", "ok"),
            intent=turn.get("intent", ""),
            steps_json=list(turn.get("steps") or []),
            card_json=dict(turn.get("card") or {}),
            detail_json=dict(turn.get("detail") or {}),
            prompt_tokens=int(turn.get("prompt_tokens") or 0),
            completion_tokens=int(turn.get("completion_tokens") or 0),
            elapsed_ms=int(turn.get("elapsed_ms") or 0),
            created_at=int(turn.get("created_at") or _now()),
        ))


def _turn_public(r) -> dict[str, Any]:
    return {
        "turn_id": r.turn_id,
        "surface": r.surface or "im",
        "channel": r.channel or "",
        "chat_id_masked": mask_id(r.chat_id),
        "status": r.status or "ok",
        "intent": r.intent or "",
        "steps": list(r.steps_json or []),
        "card": dict(r.card_json or {}) or None,
        "prompt_tokens": int(r.prompt_tokens or 0),
        "completion_tokens": int(r.completion_tokens or 0),
        "elapsed_ms": int(r.elapsed_ms or 0),
        "created_at": int(r.created_at or 0),
        "detail": dict(r.detail_json or {}),
    }


def list_turns(user_id: str, *, limit: int = 30, offset: int = 0, surface: str = "") -> dict[str, Any]:
    from mino_nexus.models.assistant import AssistantTurn

    limit = max(1, min(int(limit or 30), 200))
    with session_scope() as db:
        q = db.query(AssistantTurn).filter(AssistantTurn.user_id == user_id)
        if surface:
            q = q.filter(AssistantTurn.surface == surface)
        total = q.count()
        rows = q.order_by(AssistantTurn.created_at.desc()).offset(max(0, int(offset or 0))).limit(limit).all()
        return {"items": [_turn_public(r) for r in rows], "total": total}


def get_turn(user_id: str, turn_id: str) -> Optional[dict[str, Any]]:
    from mino_nexus.models.assistant import AssistantTurn

    with session_scope() as db:
        r = db.get(AssistantTurn, turn_id)
        if r is None or r.user_id != user_id:
            return None
        out = _turn_public(r)
        out["_channel"] = r.channel or ""
        out["_chat_id"] = r.chat_id or ""
        out["_node_id"] = r.node_id or ""
        return out


# ---------------- 订阅 ----------------


def add_subscription(sub: dict[str, Any]) -> int:
    from mino_nexus.models.assistant import AssistantSubscription

    with session_scope() as db:
        row = (
            db.query(AssistantSubscription)
            .filter(
                AssistantSubscription.user_id == sub["user_id"],
                AssistantSubscription.kind == sub.get("kind", "run_done"),
                AssistantSubscription.target_id == sub.get("target_id", ""),
                AssistantSubscription.chat_id == sub.get("chat_id", ""),
                AssistantSubscription.status == "active",
            )
            .first()
        )
        if row is not None:
            return row.id
        row = AssistantSubscription(
            user_id=sub["user_id"],
            kind=sub.get("kind", "run_done"),
            target_id=sub.get("target_id", ""),
            channel=sub.get("channel", ""),
            chat_id=sub.get("chat_id", ""),
            sender_id=sub.get("sender_id", ""),
            node_id=sub.get("node_id", ""),
            reply_ctx=dict(sub.get("reply_ctx") or {}),
            status="active",
            created_at=_now(),
        )
        db.add(row)
        db.flush()
        return row.id


def list_subscriptions(user_id: str) -> list[dict[str, Any]]:
    from mino_nexus.models.assistant import AssistantSubscription

    with session_scope() as db:
        rows = (
            db.query(AssistantSubscription)
            .filter(AssistantSubscription.user_id == user_id)
            .order_by(AssistantSubscription.id.desc())
            .limit(200)
            .all()
        )
        return [
            {
                "id": r.id, "kind": r.kind, "target_id": r.target_id or "", "channel": r.channel or "",
                "created_at": int(r.created_at or 0), "status": r.status or "active",
            }
            for r in rows
        ]


def delete_subscription(user_id: str, sub_id: int) -> bool:
    from mino_nexus.models.assistant import AssistantSubscription

    with session_scope() as db:
        row = db.get(AssistantSubscription, sub_id)
        if row is None or row.user_id != user_id:
            return False
        db.delete(row)
        return True


def active_subscriptions_for(kind: str, target_id: str) -> list[dict[str, Any]]:
    from mino_nexus.models.assistant import AssistantSubscription

    with session_scope() as db:
        rows = (
            db.query(AssistantSubscription)
            .filter(
                AssistantSubscription.kind == kind,
                AssistantSubscription.target_id == target_id,
                AssistantSubscription.status == "active",
            )
            .all()
        )
        return [
            {
                "id": r.id, "user_id": r.user_id, "channel": r.channel, "chat_id": r.chat_id,
                "sender_id": r.sender_id, "node_id": r.node_id, "reply_ctx": dict(r.reply_ctx or {}),
            }
            for r in rows
        ]


def finish_subscription(sub_id: int) -> None:
    from mino_nexus.models.assistant import AssistantSubscription

    with session_scope() as db:
        row = db.get(AssistantSubscription, sub_id)
        if row is not None:
            row.status = "done"


# ---------------- MCP token（只存哈希） ----------------


def _hash_token(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def create_mcp_token(user_id: str, name: str) -> dict[str, Any]:
    from mino_nexus.models.assistant import AssistantMcpToken

    token = MCP_TOKEN_PREFIX + secrets.token_urlsafe(32)
    with session_scope() as db:
        row = AssistantMcpToken(
            user_id=user_id,
            name=(str(name or "").strip() or "未命名")[:60],
            kind="mcp",
            prefix=token[:len(MCP_TOKEN_PREFIX) + 4],
            token_hash=_hash_token(token),
            created_at=_now(),
        )
        db.add(row)
        db.flush()
        return {"id": row.id, "name": row.name, "token": token}


def list_mcp_tokens(user_id: str) -> list[dict[str, Any]]:
    from mino_nexus.models.assistant import AssistantMcpToken

    with session_scope() as db:
        rows = db.query(AssistantMcpToken).filter(AssistantMcpToken.user_id == user_id, AssistantMcpToken.kind == "mcp").order_by(AssistantMcpToken.id.desc()).all()
        return [
            {
                "id": r.id, "name": r.name, "prefix": r.prefix,
                "created_at": int(r.created_at or 0), "last_used_at": int(r.last_used_at or 0),
                "revoked": bool(r.revoked),
            }
            for r in rows
        ]


def revoke_mcp_token(user_id: str, token_id: int) -> bool:
    from mino_nexus.models.assistant import AssistantMcpToken

    with session_scope() as db:
        row = db.get(AssistantMcpToken, token_id)
        if row is None or row.user_id != user_id or row.kind != "mcp":
            return False
        row.revoked = True
        return True


def resolve_api_token(token: str) -> Optional[str]:
    """MCP token 或助手 token 有效，返回其主人的 user_id，并记最近使用时间。"""
    from mino_nexus.models.assistant import AssistantMcpToken

    if not is_api_token(token):
        return None
    with session_scope() as db:
        row = db.query(AssistantMcpToken).filter(AssistantMcpToken.token_hash == _hash_token(token)).first()
        if row is None or row.revoked:
            return None
        row.last_used_at = _now()
        return row.user_id


def resolve_mcp_token(token: str) -> Optional[str]:
    if not str(token or "").startswith(MCP_TOKEN_PREFIX):
        return None
    return resolve_api_token(token)


def ensure_assistant_token(user_id: str) -> str:
    """每个绑定过 IM 的用户一枚助手 token。明文只存在 assistant_profiles.extra（Nexus 自己的库），
    表里只留哈希。有效就复用，被吊销或丢了就重新签发。"""
    from mino_nexus.models.assistant import AssistantMcpToken, AssistantProfile

    with session_scope() as db:
        prof = db.get(AssistantProfile, user_id)
        if prof is None:
            prof = AssistantProfile(user_id=user_id)
            db.add(prof)
            db.flush()
        extra = dict(prof.extra or {})
        cur = str(extra.get("assistant_token") or "")
        if cur:
            row = db.query(AssistantMcpToken).filter(AssistantMcpToken.token_hash == _hash_token(cur)).first()
            if row is not None and not row.revoked:
                return cur
        token = ASSISTANT_TOKEN_PREFIX + secrets.token_urlsafe(32)
        db.add(AssistantMcpToken(
            user_id=user_id, name="助手（IM）", kind="assistant",
            prefix=token[:len(ASSISTANT_TOKEN_PREFIX) + 4],
            token_hash=_hash_token(token), created_at=_now(),
        ))
        extra["assistant_token"] = token
        prof.extra = extra
        return token


def revoke_assistant_token(user_id: str) -> None:
    from mino_nexus.models.assistant import AssistantMcpToken, AssistantProfile

    with session_scope() as db:
        db.query(AssistantMcpToken).filter(
            AssistantMcpToken.user_id == user_id, AssistantMcpToken.kind == "assistant"
        ).update({"revoked": True})
        prof = db.get(AssistantProfile, user_id)
        if prof is not None:
            extra = dict(prof.extra or {})
            extra.pop("assistant_token", None)
            prof.extra = extra
