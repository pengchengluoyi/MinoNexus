"""用例步骤对页面/控件的引用。

页面详情里的“进入这个页面的用例引用”来自这里；它不是控件，也不是入口边。
一条引用只回答：哪条用例、第几步、以什么关系（进入/操作/断言）指向哪一页/哪个元素。
"""
from __future__ import annotations

from sqlalchemy import JSON, Column, Index, Integer, String, Text, UniqueConstraint

from mino_nexus.core.database import Base


class NavCasePageRef(Base):
    __tablename__ = "nav_case_page_refs"
    __table_args__ = (
        UniqueConstraint("app_id", "ref_id", name="uq_nav_case_page_ref"),
        Index("ix_nav_case_page_refs_page", "app_id", "page_state_id", "relation"),
        Index("ix_nav_case_page_refs_case", "app_id", "case_id", "step_index"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    ref_id = Column(String, nullable=False, default="")
    app_id = Column(String, index=True, nullable=False, default="")
    project_id = Column(String, index=True, nullable=False, default="")
    case_id = Column(String, nullable=False, default="")
    case_name = Column(String, nullable=False, default="")
    step_index = Column(Integer, nullable=False, default=0)
    step_id = Column(String, nullable=False, default="")
    page_state_id = Column(String, nullable=False, default="")
    page_label = Column(String, nullable=False, default="")
    control_id = Column(String, nullable=False, default="")
    relation = Column(String, nullable=False, default="enters")  # enters / operates / asserts
    quote = Column(Text, nullable=False, default="")
    variant = Column(String, nullable=False, default="")
    data = Column(JSON, nullable=False, default=dict)
    created_at = Column(Integer, nullable=False, default=0)
    updated_at = Column(Integer, nullable=False, default=0)
