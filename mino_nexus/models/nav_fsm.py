"""NavFSM 配置表。设计稿 docs/NAVIGATION_ATLAS.md §0.2.1。

三张表按 `app_id` 隔离、冗余 `project_id` 便于按项目查询。**被测 App 的文案、包名、
resource-id 全部在这里**，代码里一律不硬编码（设计稿 §0）。

校准证据（hierarchy 片段、walkthrough 记录）不进库，落 `data_dir()`，见
`services/nav_calibration_store.py`。
"""
from __future__ import annotations

from sqlalchemy import JSON, Column, Integer, String, UniqueConstraint

from mino_nexus.core.database import Base


class NavFsm(Base):
    __tablename__ = "nav_fsm"
    __table_args__ = (UniqueConstraint("app_id", "version", name="uq_nav_fsm_app_version"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    app_id = Column(String, index=True, nullable=False, default="")
    # 冗余自 apps.project_id。load 时与 apps 校验一致，不一致即拒绝。
    project_id = Column(String, index=True, nullable=False, default="")
    version = Column(String, nullable=False, default="v1")
    # hierarchy_calibration、guard_catalog 等
    meta = Column(JSON, nullable=False, default=dict)
    # lease_tags、anchor 字段名等，与 Console 号池对齐
    test_data = Column(JSON, nullable=False, default=dict)
    updated_by = Column(String, default="")
    updated_at = Column(Integer, default=0)


class NavFsmState(Base):
    __tablename__ = "nav_fsm_states"
    __table_args__ = (UniqueConstraint("fsm_id", "state_id", name="uq_nav_fsm_state"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    fsm_id = Column(Integer, index=True, nullable=False)
    state_id = Column(String, nullable=False, default="")
    kind = Column(String, nullable=False, default="page")  # page / dialog / state
    identify = Column(JSON, nullable=False, default=dict)
    guards = Column(JSON, nullable=False, default=dict)
    wiki_ref = Column(String, default="")
    # Tab 入口等：entry=1 表示应用级并列入口（底栏 Tab）
    entry = Column(Integer, nullable=False, default=0)
    role = Column(String, default="")


class NavFsmEdge(Base):
    __tablename__ = "nav_fsm_edges"
    __table_args__ = (UniqueConstraint("fsm_id", "edge_id", name="uq_nav_fsm_edge"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    fsm_id = Column(Integer, index=True, nullable=False)
    edge_id = Column(String, nullable=False, default="")
    kind = Column(String, nullable=False, default="nav")  # nav / recover
    from_state = Column(String, nullable=False, default="")
    to_state = Column(String, nullable=False, default="")
    guard = Column(JSON, default=dict)
    execute = Column(JSON, default=dict)
    effect_assert = Column(JSON, default=dict)
    on_fail = Column(JSON, default=dict)
    scroll_into_view = Column(JSON, default=dict)
