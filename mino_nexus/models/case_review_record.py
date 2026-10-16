"""Append-only, revision-scoped observations and human review decisions."""
from sqlalchemy import JSON, Column, Integer, String
from mino_nexus.core.database import Base


class CaseReviewRecord(Base):
    __tablename__ = "case_review_records"
    record_id = Column(String, primary_key=True)
    owner_user_id = Column(String, nullable=False, index=True)
    project_id = Column(String, nullable=False, index=True)
    case_id = Column(String, nullable=False, index=True)
    source_revision = Column(Integer, nullable=False)
    revision_id = Column(String, default="")
    item_id = Column(String, nullable=False)
    action = Column(String, nullable=False)
    review_status = Column(String, nullable=False)
    reason = Column(String, nullable=False)
    evidence = Column(JSON, nullable=False, default=dict)
    context = Column(JSON, nullable=False, default=dict)
    reviewed_at = Column(String, nullable=False)
