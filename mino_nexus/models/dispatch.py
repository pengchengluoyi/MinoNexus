from __future__ import annotations

from sqlalchemy import JSON, Column, String

from mino_nexus.core.database import Base


class DispatchCall(Base):
    """调度流水：LLM / pipeline job 各一行。"""

    __tablename__ = "dispatch_calls"

    id = Column(String, primary_key=True)
    at = Column(String, default="", index=True)
    kind = Column(String, default="", index=True)
    trigger = Column(String, default="", index=True)
    role = Column(String, default="", index=True)
    app_id = Column(String, default="", index=True)
    pipeline_id = Column(String, default="", index=True)
    payload_json = Column(JSON, default=dict)
