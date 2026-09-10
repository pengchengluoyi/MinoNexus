from __future__ import annotations

from sqlalchemy import JSON, Boolean, Column, Integer, String, Text

from mino_nexus.core.database import Base


class LlmJob(Base):
    """LLM job：块列表 + 槽声明 + 调用参数。prompt 正文唯一真源。"""

    __tablename__ = "llm_jobs"

    id = Column(String, primary_key=True)
    label = Column(String, default="")
    summary = Column(Text, default="")
    engine = Column(String, default="text_chat")
    role_id = Column(String, default="")
    output_schema = Column(String, default="")
    slots_json = Column(JSON, default=list)
    flags_json = Column(JSON, default=list)
    system_blocks_json = Column(JSON, default=list)
    user_blocks_json = Column(JSON, default=list)
    image_json = Column(JSON, default=dict)
    call_json = Column(JSON, default=dict)
    enabled = Column(Boolean, default=True)
    builtin = Column(Boolean, default=False)
    sort_order = Column(Integer, default=0)
    seed_rev = Column(String, default="")
    prompt_version = Column(Integer, default=1)
    overrides_json = Column(JSON, default=dict)
