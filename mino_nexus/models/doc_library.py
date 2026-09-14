from __future__ import annotations

from sqlalchemy import JSON, Column, Integer, String, Text

from mino_nexus.core.database import Base


class DocSource(Base):
    """上传的原始文档元数据。正文分片见 DocChunk。"""

    __tablename__ = "doc_sources"

    id = Column(String, primary_key=True)
    app_id = Column(String, index=True, default="")
    project_id = Column(String, index=True, default="")
    title = Column(String, default="")
    file_type = Column(String, default="")
    original_filename = Column(String, default="")
    content_hash = Column(String, index=True, default="")
    chunk_count = Column(Integer, default=0)
    status = Column(String, default="ok")
    error_msg = Column(Text, default="")
    file_size = Column(Integer, default=0)
    source_kind = Column(String, default="upload")
    source_url = Column(String, default="")
    feishu_bot_id = Column(String, default="")
    auto_sync = Column(Integer, default=0)
    sync_interval_sec = Column(Integer, default=3600)
    created_at = Column(Integer, default=0)
    updated_at = Column(Integer, default=0)


class DocChunk(Base):
    """文档分片。全文检索经 FTS5 虚拟表 doc_chunks_fts。"""

    __tablename__ = "doc_chunks"

    id = Column(String, primary_key=True)
    source_id = Column(String, index=True)
    chunk_index = Column(Integer, default=0)
    heading = Column(String, default="")
    text = Column(Text, default="")
    char_start = Column(Integer, default=0)
    char_end = Column(Integer, default=0)
    embedding_json = Column(JSON, default=list)
