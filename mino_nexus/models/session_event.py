from __future__ import annotations

from sqlalchemy import JSON, Column, Integer, String, UniqueConstraint

from mino_nexus.core.database import Base


class SessionMeta(Base):
    """Session Event Log 元数据：一行一会话。"""

    __tablename__ = "session_meta"

    session_id = Column(String(128), primary_key=True)
    run_id = Column(String(64), nullable=False, index=True)
    case_id = Column(String(128), default="", index=True)
    app_id = Column(String(64), default="", index=True)
    status = Column(String(32), default="running", index=True)
    event_count = Column(Integer, default=0)
    started_at = Column(String, nullable=False)
    finished_at = Column(String, nullable=True)
    format_version = Column(Integer, default=1)
    summary = Column(String, default="")


class SessionEvent(Base):
    """Session Event Log：append-only 事件流。"""

    __tablename__ = "session_events"
    __table_args__ = (UniqueConstraint("session_id", "seq", name="uq_session_events_seq"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(String(128), nullable=False, index=True)
    seq = Column(Integer, nullable=False)
    ts = Column(String, nullable=False)
    type = Column(String(64), nullable=False, index=True)
    turn = Column(Integer, default=0)
    phase = Column(String(32), default="")
    payload = Column(JSON, nullable=False, default=dict)
