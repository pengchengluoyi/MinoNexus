from __future__ import annotations

from sqlalchemy import JSON, Column, DateTime, Float, Integer, String, Text

from mino_nexus.core.database import Base


class AppRegressionRun(Base):
    __tablename__ = "app_regression_runs"

    run_id = Column(String(64), primary_key=True)
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


class MCaseRunTrace(Base):
    __tablename__ = "m_case_run_trace"

    run_id = Column(String(64), primary_key=True)
    case_id = Column(String(128), index=True, default="")
    app_id = Column(String(64), index=True, default="")
    batch_id = Column(String(32), index=True, default="")
    sn = Column(String(128), default="")
    platform = Column(String(32), default="android")
    overall_status = Column(String(16), default="unknown", index=True)
    payload = Column(JSON, default=dict)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)


class MCaseBaseline(Base):
    __tablename__ = "m_case_baseline"

    id = Column(Integer, primary_key=True, autoincrement=True)
    case_id = Column(String(128), index=True, nullable=False)
    device_signature = Column(String(256), default="")
    run_id = Column(String(64), default="")
    payload = Column(JSON, default=dict)
