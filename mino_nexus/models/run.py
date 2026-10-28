from __future__ import annotations

from sqlalchemy import JSON, Column, Float, Index, String, Text

from mino_nexus.core.database import Base


class AppRegressionRun(Base):
    __tablename__ = "app_regression_runs"
    __table_args__ = (Index("ix_app_regression_runs_app_started", "app_id", "started_at", "run_id"),)

    run_id = Column(String(64), primary_key=True)
    owner_user_id = Column(String, index=True, default="")
    app_id = Column(String, index=True, default="")
    run_type = Column(String(32), default="manual", index=True)
    sn = Column(String(128), default="")
    platform = Column(String(32), default="android")
    status = Column(String(32), default="running", index=True)
    total = Column(Float, default=0)
    passed = Column(Float, default=0)
    failed = Column(Float, default=0)
    error = Column(Text, default="")
    payload = Column(JSON, default=dict)
    started_at = Column(String, default="")
    finished_at = Column(String, nullable=True)
