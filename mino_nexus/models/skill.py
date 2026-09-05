from __future__ import annotations

from sqlalchemy import JSON, Boolean, Column, Integer, String, Text

from mino_nexus.core.database import Base


class Skill(Base):
    """可编辑技能：角色 + SOP + prompt + 结果视图。"""

    __tablename__ = "skills"

    id = Column(String, primary_key=True)
    label = Column(String, default="")
    summary = Column(Text, default="")
    category = Column(String, default="flow")
    role_id = Column(String, default="")
    role_label = Column(String, default="")
    engine = Column(String, default="chat")
    system_prompt = Column(Text, default="")
    sop_json = Column(JSON, default=dict)
    input_json = Column(JSON, default=dict)
    view_json = Column(JSON, default=dict)
    triggers_json = Column(JSON, default=list)
    enabled = Column(Boolean, default=True)
    builtin = Column(Boolean, default=False)
    sort_order = Column(Integer, default=0)
