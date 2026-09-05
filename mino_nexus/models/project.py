from __future__ import annotations

from sqlalchemy import JSON, Column, Integer, String, Text

from mino_nexus.core.database import Base


class Project(Base):
    __tablename__ = "projects"

    id = Column(String, primary_key=True)
    uid = Column(String, default="")
    name = Column(String, nullable=False)
    description = Column(Text, default="")
    env = Column(JSON, default=dict)
    created_by = Column(String, default="")
    created_by_name = Column(String, default="")
    created_at = Column(Integer, default=0)
    updated_at = Column(Integer, default=0)


class App(Base):
    __tablename__ = "apps"

    id = Column(String, primary_key=True)
    uid = Column(String, default="")
    name = Column(String, nullable=False)
    description = Column(Text, default="")
    platforms = Column(String, default="")
    env = Column(JSON, default=dict)
    project_id = Column(String, index=True, default="")
    created_by = Column(String, default="")
    created_by_name = Column(String, default="")
    created_at = Column(Integer, default=0)
    updated_at = Column(Integer, default=0)
