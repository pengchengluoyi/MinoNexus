"""工作项与消息、run、session、artifact 的关联。"""
from __future__ import annotations

from sqlalchemy import Column, Index, JSON, String, UniqueConstraint

from mino_nexus.core.database import Base


class WorkLink(Base):
    __tablename__ = "work_links"
    __table_args__ = (
        UniqueConstraint("work_id", "kind", "ref_id", name="uq_work_link_ref"),
        Index("ix_work_link_step", "step_id"),
        Index("ix_work_link_ref", "kind", "ref_id"),
    )

    link_id = Column(String(64), primary_key=True)
    work_id = Column(String(64), nullable=False, index=True)
    step_id = Column(String(64), default="")
    kind = Column(String(32), nullable=False)
    ref_id = Column(String(128), nullable=False)
    meta_json = Column(JSON, default=dict)
    created_at = Column(String, default="")
