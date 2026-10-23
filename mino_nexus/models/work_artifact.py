"""工作证据索引：run/session/报告/文件/人工决定。"""
from __future__ import annotations

from sqlalchemy import Column, Index, JSON, String, UniqueConstraint

from mino_nexus.core.database import Base


class WorkArtifact(Base):
    __tablename__ = "work_artifacts"
    __table_args__ = (
        UniqueConstraint("work_id", "step_id", "kind", "ref_id", name="uq_work_artifact_ref"),
        Index("ix_work_artifact_work", "work_id", "created_at"),
        Index("ix_work_artifact_ref", "ref_type", "ref_id"),
    )

    artifact_id = Column(String(64), primary_key=True)
    work_id = Column(String(64), nullable=False, index=True)
    step_id = Column(String(64), default="", index=True)

    kind = Column(String(32), nullable=False)
    title = Column(String(200), default="")
    ref_type = Column(String(32), default="")
    ref_id = Column(String(128), default="")
    uri = Column(String(1024), default="")
    sha256 = Column(String(64), default="")
    meta_json = Column(JSON, default=dict)
    created_at = Column(String, default="")
