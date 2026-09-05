from __future__ import annotations

from sqlalchemy import JSON, Column, Integer, String

from mino_nexus.core.database import Base


class AtlasAlias(Base):
    __tablename__ = "m_atlas_alias"

    id = Column(Integer, primary_key=True, autoincrement=True)
    app_id = Column(String, index=True, default="")
    payload = Column(JSON, default=dict)
