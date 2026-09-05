from __future__ import annotations

from sqlalchemy import JSON, Boolean, Column, Integer, String, Text, UniqueConstraint

from mino_nexus.core.database import Base


class CatalogEntry(Base):
    """能力目录：prep / do / check / generic / recovery。"""

    __tablename__ = "catalog_entries"
    __table_args__ = (UniqueConstraint("kind", "id", name="uq_catalog_kind_id"),)

    pk = Column(Integer, primary_key=True, autoincrement=True)
    kind = Column(String, nullable=False, index=True)
    id = Column(String, nullable=False, index=True)
    display_name = Column(String, default="")
    description = Column(Text, default="")
    enabled = Column(Boolean, default=True)
    lifecycle = Column(String, default="active")
    provider = Column(String, default="")
    owner = Column(String, default="")
    platforms_json = Column(JSON, default=list)
    visible_to_json = Column(JSON, default=list)
    category = Column(String, default="")
    exec_class = Column(String, default="")
    sort_order = Column(Integer, default=0)
    payload_json = Column(JSON, default=dict)


class CatalogMeta(Base):
    __tablename__ = "catalog_meta"

    key = Column(String, primary_key=True)
    value = Column(String, default="")
