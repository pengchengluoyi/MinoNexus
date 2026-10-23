"""工作/项目/角色级记忆。"""
from __future__ import annotations

from sqlalchemy import Column, Index, Integer, String, Text, UniqueConstraint

from mino_nexus.core.database import Base


class WorkAgentMemory(Base):
    __tablename__ = "work_agent_memory"
    __table_args__ = (
        UniqueConstraint("scope", "scope_id", "agent_id", "content_hash", name="uq_work_memory"),
        Index("ix_work_memory_lookup", "scope", "scope_id", "agent_id"),
    )

    memory_id = Column(String(64), primary_key=True)
    scope = Column(String(16), nullable=False)
    scope_id = Column(String(64), nullable=False)
    agent_id = Column(String(64), default="")
    kind = Column(String(32), default="fact")
    content = Column(Text, default="")
    content_hash = Column(String(64), default="")
    source_ref = Column(String(160), default="")
    confidence = Column(Integer, default=0)
    expires_at = Column(String(32), default="")
    created_at = Column(String, default="")
