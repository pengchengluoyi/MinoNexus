"""页面入口档案：从运行观察/用例/常识得到的「怎么进入某一页」。

设计稿：docs/架构/V8/页面入口图谱与可达性调度-20261026.md

和 `nav_fsm_edges` 的分工：
- `nav_fsm_edges` 是已发布的路线图合同，运行时规划最短路；
- `nav_page_entries` 是入口证据/工作集，允许来自未跑过的用例、只成功过一次的
  跳转、通用常识。它不是第二份页面目录，`target_state_id` 仍指向
  `nav_fsm_states.state_id`。

本版按产品要求**不做人工审核**：抽取后直接 `active` 可用；失败只降置信度/
标 stale，不需要审批。也没有导入导出接口。
"""
from __future__ import annotations

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)

from mino_nexus.core.database import Base


class NavPageEntry(Base):
    __tablename__ = "nav_page_entries"
    __table_args__ = (
        UniqueConstraint("app_id", "entry_id", name="uq_nav_page_entry"),
        Index(
            "uq_nav_page_entry_hash",
            "app_id",
            "entry_hash",
            unique=True,
            sqlite_where=text("entry_hash <> ''"),
        ),
        Index("ix_nav_page_entries_target", "app_id", "target_state_id", "status"),
        Index("ix_nav_page_entries_from", "app_id", "from_state_id", "status"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    entry_id = Column(String, nullable=False, default="")
    app_id = Column(String, index=True, nullable=False, default="")
    project_id = Column(String, index=True, nullable=False, default="")

    # 空 from 表示任意页（deeplink/冷启动/常识入口）；空 target 表示只记了目标名。
    from_state_id = Column(String, nullable=False, default="")
    target_state_id = Column(String, nullable=False, default="")
    target_label = Column(String, nullable=False, default="")

    platform = Column(String, nullable=False, default="")
    build_scope = Column(String, nullable=False, default="")
    source_kind = Column(String, nullable=False, default="case")
    # active / stale / rejected。没有 candidate/published 的人工闸门。
    status = Column(String, nullable=False, default="active")
    confidence = Column(Float, nullable=False, default=0.5)

    trigger = Column(JSON, nullable=False, default=dict)
    preconditions = Column(JSON, nullable=False, default=dict)
    expect = Column(JSON, nullable=False, default=dict)
    # 目标页的 DOM 结构指纹（v2）。用例推导可能为空，采集/到达后自动补。
    fingerprint = Column(JSON, nullable=False, default=dict)

    success_count = Column(Integer, nullable=False, default=0)
    fail_count = Column(Integer, nullable=False, default=0)
    fail_streak = Column(Integer, nullable=False, default=0)
    last_success_at = Column(Integer, nullable=False, default=0)
    last_fail_at = Column(Integer, nullable=False, default=0)
    conflict = Column(Boolean, nullable=False, default=False)
    entry_hash = Column(String, nullable=False, default="")
    created_at = Column(Integer, nullable=False, default=0)
    updated_at = Column(Integer, nullable=False, default=0)
    updated_by = Column(String, nullable=False, default="")


class NavPageEntryEvidence(Base):
    __tablename__ = "nav_page_entry_evidence"
    __table_args__ = (
        UniqueConstraint(
            "entry_id",
            "source_kind",
            "source_ref",
            "relation",
            name="uq_nav_page_entry_evidence",
        ),
        Index("ix_nav_page_entry_evidence_entry", "entry_id", "created_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    entry_id = Column(String, nullable=False, default="")
    app_id = Column(String, index=True, nullable=False, default="")
    source_kind = Column(String, nullable=False, default="")
    source_ref = Column(String, nullable=False, default="")
    relation = Column(String, nullable=False, default="leads_to")
    quote = Column(Text, nullable=False, default="")
    weight = Column(Float, nullable=False, default=0.5)
    data = Column(JSON, nullable=False, default=dict)
    created_at = Column(Integer, nullable=False, default=0)


class NavPageGap(Base):
    __tablename__ = "nav_page_gaps"
    __table_args__ = (
        UniqueConstraint("app_id", "gap_id", name="uq_nav_page_gap"),
        Index("ix_nav_page_gaps_status", "app_id", "status", "last_seen_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    gap_id = Column(String, nullable=False, default="")
    app_id = Column(String, index=True, nullable=False, default="")
    project_id = Column(String, index=True, nullable=False, default="")
    target_label = Column(String, nullable=False, default="")
    target_state_id = Column(String, nullable=False, default="")
    from_state_id = Column(String, nullable=False, default="")
    platform = Column(String, nullable=False, default="")
    # target_only / no_entry / trigger_not_visible / arrival_mismatch / fingerprint_drift
    kind = Column(String, nullable=False, default="no_entry")
    status = Column(String, nullable=False, default="open")
    detail = Column(Text, nullable=False, default="")
    occurrence_count = Column(Integer, nullable=False, default=1)
    first_seen_at = Column(Integer, nullable=False, default=0)
    last_seen_at = Column(Integer, nullable=False, default=0)
    sample_refs = Column(JSON, nullable=False, default=list)
