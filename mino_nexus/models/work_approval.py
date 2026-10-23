"""人工审批：计划、工具、重试、接管、未知写裁决。"""
from __future__ import annotations

from sqlalchemy import Column, Index, JSON, String, Text

from mino_nexus.core.database import Base


class WorkApproval(Base):
    __tablename__ = "work_approvals"
    __table_args__ = (
        Index("ix_work_approval_work_status", "work_id", "status"),
        Index("ix_work_approval_step", "step_id"),
    )

    approval_id = Column(String(64), primary_key=True)
    work_id = Column(String(64), nullable=False, index=True)
    step_id = Column(String(64), default="", index=True)

    kind = Column(String(32), nullable=False)
    status = Column(String(32), default="pending", index=True)
    title = Column(String(200), default="")
    prompt = Column(Text, default="")
    options_json = Column(JSON, default=list)
    decision_json = Column(JSON, default=dict)

    requested_by = Column(String(64), default="")
    decided_by = Column(String(64), default="")
    created_at = Column(String, default="")
    expires_at = Column(String, default="")
    decided_at = Column(String, default="")
