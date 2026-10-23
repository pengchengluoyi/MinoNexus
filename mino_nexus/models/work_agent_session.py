"""工作项内各角色的会话摘要。"""
from __future__ import annotations

from sqlalchemy import Column, Index, Integer, String, Text, UniqueConstraint

from mino_nexus.core.database import Base


class WorkAgentSession(Base):
    __tablename__ = "work_agent_sessions"
    __table_args__ = (
        UniqueConstraint("work_id", "agent_id", name="uq_work_agent_session"),
        Index("ix_work_agent_session_work", "work_id"),
    )

    session_id = Column(String(64), primary_key=True)
    work_id = Column(String(64), nullable=False, index=True)
    agent_id = Column(String(64), nullable=False)
    job_id = Column(String(64), default="")
    role_id = Column(String(64), default="")
    status = Column(String(32), default="active")
    summary = Column(Text, default="")
    last_event_seq = Column(Integer, default=0)
    created_at = Column(String, default="")
    updated_at = Column(String, default="")
