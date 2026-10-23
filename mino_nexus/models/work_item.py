"""工作项主表：目标、计划、验收、预算、状态。"""
from __future__ import annotations

from sqlalchemy import Column, Index, Integer, JSON, String, Text, UniqueConstraint

from mino_nexus.core.database import Base


class WorkItem(Base):
    __tablename__ = "work_items"
    __table_args__ = (
        UniqueConstraint("owner_user_id", "create_key", name="uq_work_owner_create_key"),
        Index("ix_work_project_status", "project_id", "status", "updated_at"),
        Index("ix_work_owner_created", "owner_user_id", "created_at"),
    )

    work_id = Column(String(64), primary_key=True)
    project_id = Column(String(64), nullable=False, index=True)
    topic_id = Column(String(64), default="", index=True)
    owner_user_id = Column(String(64), nullable=False, index=True)

    create_key = Column(String(160), nullable=False, default="")
    source_message_id = Column(String(64), default="")

    title = Column(String(200), default="")
    goal = Column(Text, default="")
    status = Column(String(32), default="planning", index=True)

    plan_json = Column(JSON, default=dict)
    plan_revision = Column(Integer, default=0)
    plan_index = Column(Integer, default=0)
    acceptance_json = Column(JSON, default=list)
    review_json = Column(JSON, default=dict)
    instructions_json = Column(JSON, default=list)

    budget_json = Column(JSON, default=dict)
    spent_json = Column(JSON, default=dict)
    policy_json = Column(JSON, default=dict)

    blocked_reason = Column(Text, default="")
    last_event_seq = Column(Integer, default=0)
    stop_requested = Column(Integer, default=0)

    created_at = Column(String, default="")
    updated_at = Column(String, default="")
    finished_at = Column(String, nullable=True)
