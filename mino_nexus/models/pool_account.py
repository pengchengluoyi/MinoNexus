"""项目号池账号：规范化表 + 按 facet 可索引。"""
from __future__ import annotations

from sqlalchemy import Column, Index, Integer, String, Text
from sqlalchemy.types import JSON

from mino_nexus.core.database import Base


class PoolAccount(Base):
    __tablename__ = "pool_accounts"

    id = Column(String, primary_key=True)
    project_id = Column(String, nullable=False, index=True)
    env = Column(String, default="test", index=True)
    display_name = Column(String, default="")
    phone = Column(String, default="")
    email = Column(String, default="")
    username = Column(String, default="")
    password = Column(String, default="")
    otp = Column(String, default="")
    kind = Column(String, default="mixed")
    profile_id = Column(String, default="")
    note = Column(Text, default="")
    locked = Column(Integer, default=0)
    lease = Column(JSON, default=dict)
    created_at = Column(Integer, default=0)
    updated_at = Column(Integer, default=0)


class PoolAccountFacet(Base):
    __tablename__ = "pool_account_facets"

    project_id = Column(String, primary_key=True)
    account_id = Column(String, primary_key=True)
    facet_key = Column(String, primary_key=True)
    facet_value = Column(String, default="unknown", index=True)


Index(
    "ix_pool_facets_project_key_value",
    PoolAccountFacet.project_id,
    PoolAccountFacet.facet_key,
    PoolAccountFacet.facet_value,
)
