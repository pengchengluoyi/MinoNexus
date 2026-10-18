from sqlalchemy import JSON, Column, Float, Integer, String, UniqueConstraint

from mino_nexus.core.database import Base


class CaseSource(Base):
    __tablename__ = "case_sources"
    source_id = Column(String, primary_key=True)
    project_id = Column(String, nullable=False, index=True)
    owner_user_id = Column(String, nullable=False, index=True)
    generation = Column(Integer, nullable=False, default=1)
    kind = Column(String, nullable=False)
    configuration = Column(JSON, nullable=False, default=dict)
    enabled = Column(Integer, nullable=False, default=0)
    auto_compile = Column(Integer, nullable=False, default=0)
    interval_seconds = Column(Integer, nullable=False, default=900)
    daily_build_limit = Column(Integer, nullable=False, default=0)
    budget_day = Column(String, nullable=False, default="")
    budget_used = Column(Integer, nullable=False, default=0)
    next_read_at = Column(Float, nullable=False, default=0)
    active_read_id = Column(String, nullable=False, default="")
    read_lease_until = Column(Float, nullable=False, default=0)
    failures = Column(Integer, nullable=False, default=0)
    content_digest = Column(String, nullable=False, default="")
    last_receipt = Column(JSON, nullable=False, default=dict)


class CaseSourceRead(Base):
    __tablename__ = "case_source_reads"
    read_id = Column(String, primary_key=True)
    source_id = Column(String, nullable=False, index=True)
    generation = Column(Integer, nullable=False)
    status = Column(String, nullable=False)
    created_at = Column(String, nullable=False)
    finished_at = Column(String, nullable=False, default="")
    receipt = Column(JSON, nullable=False, default=dict)


class CaseSourceRow(Base):
    __tablename__ = "case_source_rows"
    __table_args__ = (UniqueConstraint("source_id", "external_case_no"),)
    id = Column(Integer, primary_key=True, autoincrement=True)
    source_id = Column(String, nullable=False, index=True)
    external_case_no = Column(String, nullable=False)
    case_id = Column(String, nullable=False, index=True)
    content_digest = Column(String, nullable=False)
    source_position = Column(JSON, nullable=False, default=dict)
    retired_candidate = Column(Integer, nullable=False, default=0)
    detached = Column(Integer, nullable=False, default=0)
