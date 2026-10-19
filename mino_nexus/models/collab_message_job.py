"""Durable reply receipt; message acceptance does not wait for model execution."""
from sqlalchemy import Column, String, Text
from mino_nexus.core.database import Base


class CollabMessageJob(Base):
    __tablename__ = "collab_message_jobs"
    message_id = Column(String, primary_key=True)
    project_id = Column(String, nullable=False)
    topic_id = Column(String, index=True, nullable=False)
    owner_user_id = Column(String, nullable=False)
    status = Column(String, index=True, nullable=False, default="queued")
    error = Column(Text, default="")
    created_at = Column(String, nullable=False)
    updated_at = Column(String, nullable=False)
