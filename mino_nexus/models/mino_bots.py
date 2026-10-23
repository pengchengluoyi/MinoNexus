"""Mino Bots 协作：项目 Bot、议题、消息与工作项。"""
from __future__ import annotations

from sqlalchemy import Column, Integer, String, Text
from sqlalchemy.types import JSON

from mino_nexus.core.database import Base


class ProjectBot(Base):
    __tablename__ = "project_bots"

    bot_id = Column(String, primary_key=True)
    project_id = Column(String, index=True, nullable=False)
    name = Column(String, default="")
    template_id = Column(String, default="collab-default")
    app_ids = Column(JSON, default=list)
    platforms = Column(JSON, default=list)
    policy_override = Column(JSON, default=dict)
    status = Column(String, default="active")
    config_revision = Column(Integer, default=1)
    created_at = Column(String, default="")


class CollabTopic(Base):
    __tablename__ = "collab_topics"

    topic_id = Column(String, primary_key=True)
    project_id = Column(String, index=True, nullable=False)
    app_id = Column(String, default="")
    title = Column(String, default="")
    goal = Column(Text, default="")
    status = Column(String, default="open")
    lead_user_id = Column(String, default="")
    input_version = Column(String, default="v1")
    anchor = Column(JSON, default=dict)
    memory = Column(JSON, default=dict)
    pipeline_state = Column(JSON, default=dict)
    im_route = Column(JSON, default=dict)
    knowledge_graph = Column(JSON, default=dict)
    digest = Column(JSON, default=dict)
    paused = Column(Integer, default=0)  # retired: kept for schema compatibility; no workspace pause path
    workspace_kind = Column(String, default='')
    scope_revision = Column(Integer, default=1)
    scope_key = Column(String, unique=True, nullable=True)
    archived = Column(Integer, default=0)
    workspace_policy = Column(JSON, default=dict)
    requirement_id = Column(String, default="")
    modules = Column(JSON, default=list)
    scope_case_ids = Column(JSON, default=list)
    env_profile = Column(String, default="")
    env_surface = Column(String, default="")
    platform = Column(String, default="")
    expected_build = Column(String, default="")
    scope_snapshot = Column(JSON, default=dict)
    created_at = Column(String, default="")
    updated_at = Column(String, default="")


class CollabTopicMember(Base):
    __tablename__ = "collab_topic_members"

    topic_id = Column(String, primary_key=True)
    member_kind = Column(String, primary_key=True)
    member_id = Column(String, primary_key=True)
    joined_at = Column(String, default="")


class CollabMessage(Base):
    __tablename__ = "collab_messages"

    message_id = Column(String, primary_key=True)
    topic_id = Column(String, index=True, nullable=False)
    author_kind = Column(String, default="user")
    author_id = Column(String, default="")
    message_kind = Column(String, default='text')
    card_kind = Column(String, default='')
    ref_id = Column(String, default='')
    origin = Column(String, default='')
    role = Column(String, default='')
    thread_id = Column(String, default='', index=True)
    agent_run_id = Column(String, default='', index=True)
    plan_id = Column(String, default='')
    acting_bot_id = Column(String, default="")
    body = Column(Text, default="")
    created_at = Column(String, default="")


class CollabWorkItem(Base):
    __tablename__ = "collab_work_items"

    work_item_id = Column(String, primary_key=True)
    topic_id = Column(String, index=True, nullable=False)
    task_id = Column(String, index=True, default="")
    title = Column(String, default="")
    goal = Column(Text, default="")
    acceptance = Column(Text, default="")
    assignee_kind = Column(String, default="bot")
    assignee_id = Column(String, default="")
    holder_user_id = Column(String, default="")
    input_version = Column(String, default="")
    idempotency_key = Column(String, index=True, nullable=False, default="")
    status = Column(String, default="pending")
    action_kind = Column(String, default="none")
    action_payload = Column(JSON, default=dict)
    last_result = Column(JSON, default=dict)
    blocked_reason = Column(Text, default="")
    depends_on = Column(JSON, default=list)
    run_id = Column(String, default="")
    case_ids = Column(JSON, default=list)
    evidence = Column(JSON, default=dict)
    attempt = Column(Integer, default=0)
    next_check_at = Column(String, default="")
    origin = Column(String, default="")
    claimed_by = Column(String, default="")
    claimed_at = Column(String, default="")
    activity = Column(Text, default="")
    stage = Column(String, default="")
    heartbeat_at = Column(String, default="")
    started_at = Column(String, default="")
    created_at = Column(String, default="")
    updated_at = Column(String, default="")


class CollabActivity(Base):
    __tablename__ = "collab_activity"

    activity_id = Column(String, primary_key=True)
    topic_id = Column(String, index=True, nullable=False)
    kind = Column(String, default="")
    body = Column(Text, default="")
    refs = Column(JSON, default=dict)
    created_at = Column(String, default="")
