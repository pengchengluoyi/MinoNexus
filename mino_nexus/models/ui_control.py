from __future__ import annotations

from sqlalchemy import JSON, Column, Integer, String, UniqueConstraint

from mino_nexus.core.database import Base


class UiControl(Base):
    """用例和采集共用的控件。定位分格存放，不存看图坐标。"""

    __tablename__ = "ui_controls"
    __table_args__ = (UniqueConstraint("project_id", "desc_key", name="uq_ui_control_desc"),)

    pk = Column(Integer, primary_key=True, autoincrement=True)
    control_id = Column(String, nullable=False, index=True)
    project_id = Column(String, nullable=False, index=True)
    desc_key = Column(String, nullable=False, default="")
    element_type = Column(String, default="button")
    scope = Column(String, default="page")
    locator_value = Column(JSON, default=dict)
    pending_merge = Column(Integer, default=0)
    revision = Column(Integer, nullable=False, default=1)
    locator_spec = Column(JSON, nullable=False, default=dict)
    sensitive = Column(Integer, nullable=False, default=0)
    publish_state = Column(String, nullable=False, default="draft")
    published_by = Column(String, nullable=False, default="")
    published_at = Column(String, nullable=False, default="")
    updated_by = Column(String, nullable=False, default="")
    updated_at = Column(String, nullable=False, default="")
