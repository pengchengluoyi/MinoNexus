from __future__ import annotations

from sqlalchemy import JSON, Column, String

from mino_nexus.core.database import Base


class Settings(Base):
    __tablename__ = "settings"

    id = Column(String, primary_key=True, default="main")
    payload = Column(JSON, default=dict)
