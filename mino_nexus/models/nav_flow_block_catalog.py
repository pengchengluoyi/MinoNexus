from __future__ import annotations

from sqlalchemy import JSON, Column, Integer, String, Text, UniqueConstraint

from mino_nexus.core.database import Base

GLOBAL_APP_ID = "__global__"


class NavFlowBlockCatalog(Base):
    """通用 / 应用逻辑块宏（与页面 flow_block 分段分离）。"""

    __tablename__ = "nav_flow_block_catalog"
    __table_args__ = (UniqueConstraint("app_id", "block_id", name="uq_flow_block_catalog"),)

    pk = Column(Integer, primary_key=True, autoincrement=True)
    app_id = Column(String, nullable=False, index=True)
    block_id = Column(String, nullable=False, index=True)
    block_origin = Column(String, default="global_catalog")
    display_name = Column(String, default="")
    description = Column(Text, default="")
    steps_json = Column(JSON, nullable=False, default=list)
    version = Column(String, default="v1")
    enabled = Column(Integer, default=1)
    key_ref = Column(String, default="")
