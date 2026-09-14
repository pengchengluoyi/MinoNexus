from __future__ import annotations

from sqlalchemy import JSON, Column, Integer, String, Text

from mino_nexus.core.database import Base


class AppIntelLink(Base):
    """跨渠道关联边（补充 wiki_ref，不替代 guards 内权威挂接）。"""

    __tablename__ = "app_intel_links"

    id = Column(Integer, primary_key=True, autoincrement=True)
    app_id = Column(String, nullable=False, index=True)
    from_ref = Column(String, nullable=False, default="")
    to_ref = Column(String, nullable=False, default="")
    rel = Column(String, nullable=False, default="")
    meta_json = Column(JSON, default=dict)
    created_at = Column(Integer, default=0)


class AppIntelProposal(Base):
    """待审队列：nav_patch / knowledge_draft / doc_learn_batch 等。"""

    __tablename__ = "app_intel_proposals"

    id = Column(String, primary_key=True)
    app_id = Column(String, nullable=False, index=True)
    kind = Column(String, nullable=False, default="")
    status = Column(String, nullable=False, default="pending")
    payload_json = Column(JSON, default=dict)
    created_at = Column(Integer, default=0)
    updated_at = Column(Integer, default=0)
    note = Column(Text, default="")
