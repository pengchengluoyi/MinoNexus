"""助手的数据层：身份绑定、助手档案、调用轨迹、订阅、MCP token。

不含任何「以某个用户身份执行」的逻辑；执行层单独设计。
"""
from __future__ import annotations

import hashlib
import json
import re
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


def create_bind_code(user_id: str, *, node_id: str, channel: str, instance_id: str) -> dict[str, Any]:
    from mino_nexus.models.assistant import ImBindCode, AssistantProfile

    if _route_args(channel, instance_id, node_id) != user_id:
        raise PermissionError("渠道不属于当前账号")
    now = _now()
    with session_scope() as db:
        profile = db.get(AssistantProfile, user_id)
        if profile is None:
            profile = AssistantProfile(user_id=user_id)
            db.add(profile)
        profile.extra = {**(profile.extra or {}), "bind_code_route": {
            "node_id": node_id, "channel": channel, "instance_id": instance_id}}
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


def _route_args(channel: str, instance_id: str, node_id: str) -> str:
    from mino_nexus.platform.identity import require_user_id
    from mino_nexus.resources.access_scope import node_owner_id
    uid = require_user_id()
    if not channel or not instance_id or not node_id or node_owner_id(node_id) != uid:
        raise PermissionError("渠道来源或归属无效")
    return uid


def consume_bind_code(code: str, *, channel: str, tenant: str, sender_id: str,
                      instance_id: str = "", node_id: str = "") -> Optional[str]:
    """同一事务校验归属、条件删除一次性码并创建映射；不可覆盖其他主人。"""
    from mino_nexus.models.assistant import ImBindCode, ImRouteIdentity, AssistantProfile
    from sqlalchemy import delete
    from sqlalchemy.exc import IntegrityError
    uid = _route_args(channel, instance_id, node_id)
    now = _now()
    try:
        with session_scope() as db:
            profile = db.get(AssistantProfile, uid)
            route = (profile.extra or {}).get("bind_code_route") if profile else None
            if route != {"node_id": node_id, "channel": channel, "instance_id": instance_id}:
                return None
            # DELETE RETURNING 同时校验与抢占。跨账号尝试不消耗该码。
            owner = db.execute(delete(ImBindCode).where(
                ImBindCode.code == str(code or "").strip(), ImBindCode.user_id == uid,
                ImBindCode.expires_at > now).returning(ImBindCode.user_id)).scalar_one_or_none()
            if not owner:
                return None
            existing = db.query(ImRouteIdentity).filter_by(
                channel=channel, instance_id=instance_id, tenant=tenant, sender_id=sender_id).first()
            if existing is not None:
                if existing.user_id != uid or existing.node_id != node_id:
                    raise PermissionError("该身份无法绑定")
            else:
                db.add(ImRouteIdentity(user_id=uid, channel=channel, instance_id=instance_id,
                                       tenant=tenant, node_id=node_id, sender_id=sender_id, bound_at=now, binding_key=uuid.uuid4().hex))
        return uid
    except (IntegrityError, PermissionError):
        return None


def bind_identity(user_id: str, *, channel: str, tenant: str, sender_id: str,
                  instance_id: str = "", node_id: str = "") -> str:
    """用于明确维护操作或已验证的渠道登录本人关联，无法改写既有身份归属。"""
    from mino_nexus.models.assistant import ImRouteIdentity
    if _route_args(channel, instance_id, node_id) != user_id:
        raise PermissionError("该身份无法绑定")
    with session_scope() as db:
        row = db.query(ImRouteIdentity).filter_by(channel=channel, instance_id=instance_id,
                                                tenant=tenant, sender_id=sender_id).first()
        if row is not None:
            if row.user_id != user_id or row.node_id != node_id:
                raise PermissionError("该身份无法绑定")
            return user_id
        db.add(ImRouteIdentity(user_id=user_id, channel=channel, instance_id=instance_id,
                               tenant=tenant, node_id=node_id, sender_id=sender_id, bound_at=_now(), binding_key=uuid.uuid4().hex))
        return user_id


def find_route_identity(channel: str, tenant: str, sender_id: str, *, instance_id: str = "",
                        node_id: str = "") -> Optional[dict[str, Any]]:
    from mino_nexus.models.assistant import ImRouteIdentity
    if not instance_id or not node_id or not sender_id:
        return None
    with session_scope() as db:
        row = db.query(ImRouteIdentity).filter_by(channel=channel, instance_id=instance_id,
            tenant=tenant, sender_id=sender_id, node_id=node_id).first()
        return {"user_id": row.user_id, "binding_key": row.binding_key,
                "bound_at": int(row.bound_at or 0)} if row else None


def find_identity(channel: str, tenant: str, sender_id: str, *, instance_id: str = "",
                  node_id: str = "") -> Optional[str]:
    row = find_route_identity(channel, tenant, sender_id, instance_id=instance_id, node_id=node_id)
    return row["user_id"] if row else None


class ImBindingError(PermissionError):
    """绑定校验的固定原因码；不把身份或渠道凭据写进异常。"""
    def __init__(self, reason: str):
        super().__init__("社交绑定已失效")
        self.reason = reason


def require_im_binding(user_id: str, channel: str, node_id: str, reply_ctx: dict[str, Any]) -> None:
    """每次模型、工具和发送前复核同一绑定；解绑再绑定也不能复活旧请求。"""
    from mino_nexus.platform.identity import require_user_id
    from mino_nexus.resources.node_source import source_validation as _source_validation
    require_user_id(user_id)
    instance = str(reply_ctx.get("instance_id") or "")
    row = find_route_identity(channel, str(reply_ctx.get("tenant") or ""),
        str(reply_ctx.get("sender_id") or ""), instance_id=instance, node_id=node_id)
    if not row:
        raise ImBindingError("binding_missing")
    if row["user_id"] != user_id:
        raise ImBindingError("binding_owner_mismatch")
    if not reply_ctx.get("binding_key") or row["binding_key"] != reply_ctx["binding_key"]:
        raise ImBindingError("binding_changed")
    source_ctx = dict(reply_ctx)
    # 旧版持久请求遗漏了固定渠道的 transport。只在绑定已验证后恢复路由标记；
    # 真实入站仍由 gateway 严格校验，显式错误标记也不能被修正或绕过。
    if channel == "feishu_cli" and source_ctx.get("transport") in (None, ""):
        source_ctx["transport"] = "feishu_cli"
    source_owner, reason = _source_validation({"channel": channel, "node_id": node_id,
        "instance_id": instance, "reply_ctx": source_ctx})
    if source_owner != user_id:
        raise ImBindingError(reason or "source_owner_mismatch")


def legacy_inbound_claimed(key: str) -> bool:
    from mino_nexus.models.assistant import ImInboundClaim
    from mino_nexus.platform.identity import require_user_id
    with session_scope() as db:
        return db.query(ImInboundClaim.key).filter(ImInboundClaim.key == key, ImInboundClaim.owner_user_id == require_user_id()).first() is not None


def claim_inbound(key: str, owner_user_id: str) -> bool:
    from sqlalchemy.dialects.sqlite import insert
    from mino_nexus.models.assistant import ImInboundClaim
    from mino_nexus.platform.identity import require_user_id
    uid = require_user_id(owner_user_id)
    if not key:
        return False
    with session_scope() as db:
        result = db.execute(insert(ImInboundClaim).values(key=key, owner_user_id=uid, created_at=_now())
                            .on_conflict_do_nothing(index_elements=["key"]))
        return result.rowcount == 1


def list_identities(user_id: str, *, merge_accounts: bool = False) -> list[dict[str, Any]]:
    from mino_nexus.models.assistant import ImRouteIdentity

    with session_scope() as db:
        rows = db.query(ImRouteIdentity).filter(ImRouteIdentity.user_id == user_id).order_by(ImRouteIdentity.id).all()
        # 同一账号重新登录可能更换机器人实例；保留路由记录，只合并管理视图。
        accounts = {}
        for row in rows:
            key = (row.channel, row.node_id, row.tenant, row.sender_id,
                   "" if merge_accounts and row.channel == "wechat" else row.instance_id)
            accounts[key] = row
        return [
            {
                "id": r.id,
                "channel": r.channel,
                "node_id": r.node_id,
                "instance_id": r.instance_id,
                "status": "active",
                "sender_id_masked": mask_id(r.sender_id),
                "tenant": r.tenant or "",
                "bound_at": int(r.bound_at or 0),
            }
            for r in accounts.values()
        ]


def delete_identity(user_id: str, identity_id: int) -> bool:
    from mino_nexus.models.assistant import ImRouteIdentity

    with session_scope() as db:
        row = db.get(ImRouteIdentity, identity_id)
        if row is None or row.user_id != user_id:
            return False
        from mino_nexus.models.assistant import AssistantSubscription
        db.query(AssistantSubscription).filter_by(user_id=user_id, channel=row.channel,
            sender_id=row.sender_id, node_id=row.node_id, status="active").update({"status": "cancelled"})
        # 解绑账号时同时撤销该节点旧实例的绑定，避免旧登录恢复后重新生效。
        bindings = db.query(ImRouteIdentity).filter_by(user_id=user_id, channel=row.channel,
            node_id=row.node_id, tenant=row.tenant, sender_id=row.sender_id)
        if row.channel != "wechat":
            bindings = bindings.filter_by(instance_id=row.instance_id)
        bindings.delete(synchronize_session=False)
        db.flush()
        left = db.query(ImRouteIdentity.id).filter(ImRouteIdentity.user_id == user_id).first()
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
                "user_id": user_id, "persona": "",
                "enabled": True, "project_id": "",
            }
        return {
            "user_id": row.user_id,
            "persona": row.persona or "",
            "enabled": True,
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
        row.enabled = True
    return get_profile(user_id)


# ---------------- 轮次轨迹 ----------------


def _trace_value(value: Any) -> Any:
    from mino_nexus.core.secret_redaction import sanitize

    value = sanitize(value)
    if isinstance(value, str):
        value = re.sub(r'(?i)(password|passwd|secret|api[_-]?key|access[_-]?token|token|otp|验证码|密码|密钥)\s*[:=：]\s*[^\s,，;；]+', r'\1：[已隐藏]', value)
        value = re.sub(r'(?i)\bBearer\s+\S+', 'Bearer [已隐藏]', value)
        value = re.sub(r'(/绑定\s*)\d{6}', r'\1[已隐藏]', value)
        value = re.sub(r'(?<!\d)1[3-9]\d{9}(?!\d)', '[手机号已隐藏]', value)
        return value[:8000] + ('…[已截断]' if len(value) > 8000 else '')
    if isinstance(value, dict):
        return {key: _trace_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_trace_value(item) for item in value]
    return value


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

    import hashlib
    steps = []
    for step in turn.get("steps") or []:
        safe = {k: step.get(k) for k in ("tool", "label", "status", "ms")}
        safe["args_sha256"] = hashlib.sha256(str(step.get("args_brief") or "").encode()).hexdigest()
        steps.append(safe)
    card = turn.get("card") or {}
    safe_card = _trace_value(card)
    safe_card["body_redacted"] = not ("user_text" in turn or "assistant_text" in turn)
    detail = dict(turn.get("detail") or {})
    for key in ("user_text", "assistant_text"):
        if key in turn:
            detail[key] = _trace_value(str(turn.get(key) or ""))
    detail["text_truncated"] = any(len(str(turn.get(key) or "")) > 8000 for key in ("user_text", "assistant_text"))
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
            steps_json=steps,
            card_json=safe_card,
            detail_json=detail,
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
        "steps": [{k: v for k, v in step.items() if k in ("tool", "label", "status", "ms", "args_sha256")} for step in (r.steps_json or []) if isinstance(step, dict)],
        "card": _trace_value(r.card_json) if r.card_json else None,
        "user_text": (r.detail_json or {}).get("user_text", ""),
        "assistant_text": (r.detail_json or {}).get("assistant_text", ""),
        "request_id": (r.detail_json or {}).get("request_id", ""),
        "prompt_tokens": int(r.prompt_tokens or 0),
        "completion_tokens": int(r.completion_tokens or 0),
        "elapsed_ms": int(r.elapsed_ms or 0),
        "created_at": int(r.created_at or 0),
        "detail": {k: v for k, v in dict(r.detail_json or {}).items() if not k.startswith("_")},
    }


def record_turn_delivery(turn_id: str, text: str, status: str, *, sent_segments: int = 0, sent_text: str | None = None) -> None:
    from mino_nexus.platform.identity import require_user_id
    from mino_nexus.models.assistant import AssistantTurn

    with session_scope() as db:
        row = db.get(AssistantTurn, turn_id)
        if row is not None and row.user_id == require_user_id():
            confirmed = sent_text if sent_text is not None else text if status == "sent" else ""
            row.detail_json = {**(row.detail_json or {}), "outbound_text": _trace_value(text), "sent_text": _trace_value(confirmed),
                               "delivery_status": status, "sent_segments": sent_segments}


def recent_im_history(user_id: str, inbound: dict[str, Any], binding: dict[str, Any]) -> list[dict[str, Any]]:
    from mino_nexus.models.assistant import AssistantTurn

    with session_scope() as db:
        query = db.query(AssistantTurn).filter_by(user_id=user_id, surface="im",
            channel=str(inbound.get("channel") or ""), chat_id=str(inbound.get("chat_id") or ""),
            node_id=str(inbound.get("node_id") or ""))
        for key in ("instance_id", "tenant", "sender_id", "thread_id"):
            query = query.filter(AssistantTurn.detail_json["_im_route"][key].as_string() == str(inbound.get(key) or ""))
        query = query.filter(AssistantTurn.detail_json["_im_route"]["binding_key"].as_string() == str(binding.get("binding_key") or ""),
                             AssistantTurn.created_at > int(binding.get("bound_at") or 0),
                             AssistantTurn.status.notin_(("processing", "interrupted")))
        rows = query.order_by(AssistantTurn.created_at.desc(), AssistantTurn.turn_id.desc()).limit(4).all()
        history = []
        for row in reversed(rows):
            for item in (row.detail_json or {}).get('_attachments') or []:
                if isinstance(item, dict) and item.get('ref') and item.get('kind') in {'image', 'file'}:
                    history.append({'role': 'user', 'text': json.dumps({'attachment': item}, ensure_ascii=False),
                                    'ts': row.created_at, 'attachment': True})
            for role, key in (("user", "user_text"), ("assistant", "assistant_text")):
                detail = row.detail_json or {}
                if role == "assistant" and detail.get("delivery_status") != "sent":
                    continue
                text = str(detail.get("sent_text" if role == "assistant" else key) or "")
                if text:
                    history.append({"role": role, "text": text[:500], "ts": row.created_at})
        return history


def list_turns(user_id: str, *, limit: int = 30, offset: int = 0, surface: str = "", node_id: str = "") -> dict[str, Any]:
    from mino_nexus.models.assistant import AssistantTurn

    limit = max(1, min(int(limit or 30), 200))
    with session_scope() as db:
        q = db.query(AssistantTurn).filter(AssistantTurn.user_id == user_id)
        if node_id:
            q = q.filter(AssistantTurn.node_id == node_id)
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
        out["_im_route"] = dict((r.detail_json or {}).get("_im_route") or {})
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
                AssistantSubscription.channel == sub.get("channel", ""),
                AssistantSubscription.node_id == sub.get("node_id", ""),
                AssistantSubscription.sender_id == sub.get("sender_id", ""),
                AssistantSubscription.reply_ctx == dict(sub.get("reply_ctx") or {}),
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


def list_subscriptions(user_id: str, *, node_id: str = "") -> list[dict[str, Any]]:
    from mino_nexus.models.assistant import AssistantSubscription

    with session_scope() as db:
        rows = (
            db.query(AssistantSubscription)
            .filter(AssistantSubscription.user_id == user_id)
            .filter(AssistantSubscription.node_id == node_id if node_id else True)
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
        if row is None or row.revoked or row.kind not in ("mcp", "assistant"):
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


def create_probe_token(name: str = "maintenance") -> dict[str, Any]:
    """Local maintenance credential; no user profile or business API access."""
    from mino_nexus.models.assistant import AssistantMcpToken
    token = "mino_probe_" + secrets.token_urlsafe(32)
    with session_scope() as db:
        row = AssistantMcpToken(user_id="system:probe", name=name[:60], kind="probe",
            prefix=token[:15], token_hash=_hash_token(token), created_at=_now())
        db.add(row)
        db.flush()
        return {"id": row.id, "token": token}


def resolve_probe_token(token: str) -> bool:
    """Read-only authentication: no last_used_at update or assistant token."""
    if not str(token or "").startswith("mino_probe_"):
        return False
    from mino_nexus.core.database import SessionLocal
    from mino_nexus.models.assistant import AssistantMcpToken
    with SessionLocal() as db:
        row = db.query(AssistantMcpToken).filter(
            AssistantMcpToken.token_hash == _hash_token(token),
            AssistantMcpToken.kind == "probe", AssistantMcpToken.revoked.is_(False)).first()
        return row is not None


def _resolve_token_session(raw: str):
    """助手 token / MCP token：权限等同其主人，可随时在 Workbench 吊销。"""
    if not is_api_token(raw):
        return None
    from mino_nexus.platform import auth_store

    uid = resolve_api_token(raw)
    sess = auth_store.api_session_for(uid) if uid else None
    if sess is None:
        raise PermissionError("token 无效或已吊销")
    return sess


from mino_nexus.contract.tokens import register_token_resolver  # noqa: E402

register_token_resolver(_resolve_token_session)
