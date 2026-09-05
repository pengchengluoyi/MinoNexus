from __future__ import annotations

from sqlalchemy import JSON, Column, Integer, String, Text, UniqueConstraint

from mino_nexus.core.database import Base


class AppCase(Base):
    __tablename__ = "app_cases"
    __table_args__ = (UniqueConstraint("app_id", "case_id", name="uq_app_case"),)

    pk = Column(Integer, primary_key=True, autoincrement=True)
    case_id = Column(String, nullable=False, index=True)
    app_id = Column(String, nullable=False, index=True)
    requirement_id = Column(String, default="", index=True)
    name = Column(String, default="")
    module = Column(String, default="")
    platform = Column(String, default="")
    aspect = Column(String, default="")
    precondition = Column(Text, default="")
    steps = Column(JSON, default=list)
    expected = Column(JSON, default=list)
    steps_raw = Column(Text, default="")
    expected_raw = Column(Text, default="")
    point_ids = Column(JSON, default=list)
    source = Column(String, default="")
    extra = Column(JSON, default=dict)
    sort_index = Column(Integer, default=0)
    updated_at = Column(String, default="")
