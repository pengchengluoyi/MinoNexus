from __future__ import annotations

from sqlalchemy import JSON, Column, Integer, String

from mino_nexus.core.database import Base


class StudioNav(Base):
    __tablename__ = "studio_nav"

    id = Column(String, primary_key=True)
    allowed = Column(JSON, default=list)
    version = Column(Integer, default=2)
