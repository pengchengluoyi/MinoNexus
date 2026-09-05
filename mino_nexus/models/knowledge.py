from __future__ import annotations

from sqlalchemy import JSON, Boolean, Column, Integer, String, Text

from mino_nexus.core.database import Base


class KnowledgeEntry(Base):
    """业务知识条目。独立于扩展包 / settings.payload。"""

    __tablename__ = "knowledge_entries"

    id = Column(String, primary_key=True)
    title = Column(String, nullable=False, default="")
    content = Column(Text, default="")
    category = Column(String, default="其他")
    tags_json = Column(JSON, default=list)
    app_ids_json = Column(JSON, default=list)
    enabled = Column(Boolean, default=True)
    source = Column(String, default="manual")
    review_status = Column(String, default="approved")
    account_id = Column(String, default="")
    account_ident = Column(String, default="")
    extra_json = Column(JSON, default=dict)
    updated_at = Column(Integer, default=0)
