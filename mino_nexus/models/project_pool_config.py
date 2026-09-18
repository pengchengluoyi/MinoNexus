"""项目号池配置：扩展字段、启用模板、本地模板（与 projects.env 运行时环境分离）。"""
from __future__ import annotations

from sqlalchemy import Column, String
from sqlalchemy.types import JSON

from mino_nexus.core.database import Base


class ProjectPoolConfig(Base):
    __tablename__ = "project_pool_config"

    project_id = Column(String, primary_key=True)
    facet_extensions = Column(JSON, default=list)
    template_ids = Column(JSON, default=list)
    pool_local = Column(JSON, default=dict)
