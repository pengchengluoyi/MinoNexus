"""可执行步骤：计划、角色回合、工具、等待、审批。"""
from __future__ import annotations

from sqlalchemy import Column, Float, Index, Integer, JSON, String, Text, UniqueConstraint

from mino_nexus.core.database import Base


class WorkStep(Base):
    __tablename__ = "work_steps"
    __table_args__ = (
        UniqueConstraint("work_id", "seq", name="uq_work_step_seq"),
        UniqueConstraint("work_id", "step_key", name="uq_work_step_key"),
        Index("ix_work_step_claim", "status", "next_run_at", "lease_until"),
        Index("ix_work_step_work", "work_id", "seq"),
        Index("ix_work_step_parent", "parent_step_id"),
    )

    step_id = Column(String(64), primary_key=True)
    work_id = Column(String(64), nullable=False, index=True)
    seq = Column(Integer, nullable=False)
    step_key = Column(String(160), nullable=False)

    plan_step_id = Column(String(64), default="")
    kind = Column(String(32), nullable=False)
    agent_id = Column(String(64), default="")
    job_id = Column(String(64), default="")
    role_id = Column(String(64), default="")

    title = Column(String(200), default="")
    intent = Column(Text, default="")
    status = Column(String(32), default="pending", index=True)

    input_json = Column(JSON, default=dict)
    output_json = Column(JSON, default=dict)

    ref_type = Column(String(32), default="")
    ref_id = Column(String(128), default="")

    parent_step_id = Column(String(64), default="")
    attempt = Column(Integer, default=0)
    max_attempts = Column(Integer, default=3)

    lease_owner = Column(String(64), default="")
    lease_until = Column(Float, nullable=True)
    next_run_at = Column(Float, nullable=True)
    last_heartbeat_at = Column(Float, nullable=True)

    error = Column(Text, default="")

    created_at = Column(String, default="")
    updated_at = Column(String, default="")
    started_at = Column(String, nullable=True)
    finished_at = Column(String, nullable=True)
