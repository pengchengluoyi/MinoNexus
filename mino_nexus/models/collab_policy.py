"""项目级 Mino Bots 自动化策略（§3.1）。"""
from __future__ import annotations

from sqlalchemy import Column, Integer, String
from sqlalchemy.types import JSON

from mino_nexus.core.database import Base


class CollabPolicy(Base):
    __tablename__ = "collab_policies"

    policy_id = Column(String, primary_key=True)
    project_id = Column(String, index=True, nullable=False)
    app_id = Column(String, default="")
    auto_apply_drafts = Column(Integer, default=1)
    auto_compile = Column(Integer, default=1)
    auto_execute = Column(Integer, default=1)
    execute_env_profiles = Column(JSON, default=list)
    execute_surfaces = Column(JSON, default=list)
    auto_publish = Column(Integer, default=0)
    auto_retry_toolchain = Column(Integer, default=1)
    max_parallel_runs = Column(Integer, default=1)
    daily_llm_budget_tokens = Column(Integer, default=0)
    notify = Column(JSON, default=list)
    watch_manual_runs = Column(Integer, default=0)
    updated_at = Column(String, default="")
