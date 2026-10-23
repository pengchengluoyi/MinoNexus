"""工作事件流：追加写，UI/审计/恢复共用。"""
from __future__ import annotations

from sqlalchemy import Column, Index, Integer, JSON, String, UniqueConstraint

from mino_nexus.core.database import Base


class WorkEvent(Base):
    __tablename__ = "work_events"
    __table_args__ = (
        UniqueConstraint("work_id", "seq", name="uq_work_event_seq"),
        Index("ix_work_event_work_seq", "work_id", "seq"),
        Index("ix_work_event_type", "type", "created_at"),
    )

    event_id = Column(String(64), primary_key=True)
    work_id = Column(String(64), nullable=False, index=True)
    step_id = Column(String(64), default="", index=True)
    seq = Column(Integer, nullable=False)
    type = Column(String(64), nullable=False)
    actor = Column(String(64), default="system")
    payload = Column(JSON, default=dict)
    created_at = Column(String, default="")
