"""Immutable case definition; independent of mutable draft and compile output."""
from sqlalchemy import JSON, Column, Integer, String, UniqueConstraint, event
from mino_nexus.core.database import Base


class CaseRevision(Base):
    __tablename__ = "case_revisions"
    __table_args__ = (UniqueConstraint("project_id", "case_id", "revision", name="uq_case_source_revision"),)

    revision_id = Column(String, primary_key=True)
    owner_user_id = Column(String, nullable=False, index=True)
    project_id = Column(String, nullable=False, index=True)
    case_id = Column(String, nullable=False, index=True)
    revision = Column(Integer, nullable=False)
    source = Column(JSON, nullable=False)
    source_digest = Column(String, nullable=False)
    compile_input = Column(JSON, nullable=False)
    input_digest = Column(String, nullable=False)
    origin = Column(JSON, nullable=False, default=dict)
    created_at = Column(String, nullable=False)


@event.listens_for(CaseRevision, "before_update")
@event.listens_for(CaseRevision, "before_delete")
def _immutable(*_args):
    raise ValueError("case_revision_immutable")
