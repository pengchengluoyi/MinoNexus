"""V4 社交助手：身份绑定、助手档案、调用轨迹、订阅、MCP token。

调用轨迹保留限长并按字段脱敏的轮次正文，用于请求复盘；Scout 保留渠道消息真源。
"""
from __future__ import annotations

from sqlalchemy import JSON, Boolean, Column, Integer, String, Text, UniqueConstraint

from mino_nexus.core.database import Base


class ImIdentity(Base):
    """IM 账号 → Nexus 用户。一个用户可绑多个渠道，共用同一个助手。"""

    __tablename__ = "im_identities"
    __table_args__ = (UniqueConstraint("channel", "tenant", "sender_id", name="uq_im_identity"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String, index=True, nullable=False)
    channel = Column(String, nullable=False)
    tenant = Column(String, default="")
    sender_id = Column(String, nullable=False)
    bound_at = Column(Integer, default=0)


class ImBindCode(Base):
    __tablename__ = "im_bind_codes"

    code = Column(String, primary_key=True)
    user_id = Column(String, index=True, nullable=False)
    expires_at = Column(Integer, default=0)


class AssistantProfile(Base):
    """每个用户一个助手。project_id 预留：以后按项目拆助手时不用迁表。"""

    __tablename__ = "assistant_profiles"

    user_id = Column(String, primary_key=True)
    project_id = Column(String, default="")
    persona = Column(Text, default="")
    default_app_id = Column(String, default="")
    default_sn = Column(String, default="")
    enabled = Column(Boolean, default=True)
    extra = Column(JSON, default=dict)


class AssistantTurn(Base):
    __tablename__ = "assistant_turns"

    turn_id = Column(String, primary_key=True)
    user_id = Column(String, index=True, default="")
    surface = Column(String, default="im")
    channel = Column(String, default="")
    chat_id = Column(String, default="")
    node_id = Column(String, default="")
    msg_key = Column(String, index=True, default="")
    status = Column(String, default="ok")
    intent = Column(Text, default="")
    steps_json = Column(JSON, default=list)
    card_json = Column(JSON, default=dict)
    detail_json = Column(JSON, default=dict)
    prompt_tokens = Column(Integer, default=0)
    completion_tokens = Column(Integer, default=0)
    elapsed_ms = Column(Integer, default=0)
    created_at = Column(Integer, index=True, default=0)


class AssistantSubscription(Base):
    __tablename__ = "assistant_subscriptions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String, index=True, nullable=False)
    kind = Column(String, default="run_done")
    target_id = Column(String, index=True, default="")
    channel = Column(String, default="")
    chat_id = Column(String, default="")
    sender_id = Column(String, default="")
    node_id = Column(String, default="")
    reply_ctx = Column(JSON, default=dict)
    status = Column(String, default="active")
    created_at = Column(Integer, default=0)


class AssistantMcpToken(Base):
    __tablename__ = "assistant_mcp_tokens"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String, index=True, nullable=False)
    name = Column(String, default="")
    kind = Column(String, default="mcp")  # mcp：用户给外部 Agent；assistant：IM 绑定时签发，Nexus 自己代用户调路由
    prefix = Column(String, default="")
    token_hash = Column(String, unique=True, index=True, nullable=False)
    created_at = Column(Integer, default=0)
    last_used_at = Column(Integer, default=0)
    revoked = Column(Boolean, default=False)


class ImRouteIdentity(Base):
    """验证了节点/机器人实例的路由；历史 im_identities 不参与在线执行。"""
    __tablename__ = "im_route_identities"
    __table_args__ = (UniqueConstraint("channel", "instance_id", "tenant", "sender_id", name="uq_im_route_identity"),)
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String, index=True, nullable=False)
    node_id = Column(String, nullable=False)
    channel = Column(String, nullable=False)
    instance_id = Column(String, nullable=False)
    tenant = Column(String, default="", nullable=False)
    sender_id = Column(String, nullable=False)
    bound_at = Column(Integer, default=0)
    binding_key = Column(String, nullable=False, default="")


class ImInboundClaim(Base):
    """所有有效回调（包括绑定指令）先原子抢占，避免并发/重放重复执行。"""
    __tablename__ = "im_inbound_claims"
    key = Column(String, primary_key=True)
    owner_user_id = Column(String, index=True, nullable=False)
    created_at = Column(Integer, index=True, nullable=False)
