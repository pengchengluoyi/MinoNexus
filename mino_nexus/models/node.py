from __future__ import annotations

from sqlalchemy import JSON, Column, String

from mino_nexus.core.database import Base


class Node(Base):
    __tablename__ = "nodes"

    node_id = Column(String, primary_key=True)
    studio_id = Column(String, default="")
    owner_user_id = Column(String, default="")
    extra = Column(JSON, default=dict)
    first_seen = Column(String, default="")
    updated_at = Column(String, default="")


class Studio(Base):
    __tablename__ = "studios"

    studio_id = Column(String, primary_key=True)
    owner_user_id = Column(String, default="")
    extra = Column(JSON, default=dict)
    updated_at = Column(String, default="")
