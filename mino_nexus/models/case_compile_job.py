from sqlalchemy import JSON, Boolean, Column, Float, Integer, String, UniqueConstraint
from mino_nexus.core.database import Base


class CaseCompileJob(Base):
    __tablename__ = "case_compile_jobs"
    __table_args__ = (UniqueConstraint("owner_user_id", "project_id", "case_id", "request_id", name="uq_case_compile_request"),)

    job_id = Column(String, primary_key=True)
    owner_user_id = Column(String, nullable=False, index=True)
    project_id = Column(String, nullable=False, index=True)
    case_id = Column(String, nullable=False, index=True)
    revision_id = Column(String, nullable=False, index=True)
    source_revision = Column(Integer, nullable=False)
    request_id = Column(String, nullable=False)
    parent_job_id = Column(String, default="")
    status = Column(String, nullable=False, index=True)
    claim_token = Column(String, default="")
    worker_id = Column(String, default="")
    lease_until = Column(Float, default=0)
    cancel_requested = Column(Boolean, default=False)
    input_digest = Column(String, nullable=False)
    build = Column(JSON, nullable=False)
    output = Column(JSON, nullable=False, default=dict)
    diagnostics = Column(JSON, nullable=False, default=list)
    activated = Column(Boolean, default=False)
    created_at = Column(String, nullable=False)
    started_at = Column(String, default="")
    finished_at = Column(String, default="")
