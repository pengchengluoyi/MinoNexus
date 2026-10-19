"""协作工作项审批。"""
from __future__ import annotations

from sqlalchemy import Column, String, Text

from mino_nexus.core.database import Base


class CollabApproval(Base):
    __tablename__ = "collab_approvals"

    approval_id = Column(String, primary_key=True)
    work_item_id = Column(String, index=True, nullable=False)
    topic_id = Column(String, index=True, nullable=False)
    step = Column(String, default="maintainer")
    approver_user_id = Column(String, default="")
    status = Column(String, default="pending")
    comment = Column(Text, default="")
    created_at = Column(String, default="")
    decided_at = Column(String, default="")
