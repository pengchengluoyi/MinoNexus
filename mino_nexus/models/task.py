from __future__ import annotations

from sqlalchemy import Column, String

from mino_nexus.core.database import Base


class Task(Base):
    __tablename__ = "tasks"

    id = Column(String, primary_key=True)
    app_id = Column(String, index=True, default="")
    name = Column(String, default="")
    task_type = Column(String, default="")
    status = Column(String, default="pending")
    created_at = Column(String, default="")
