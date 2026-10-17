"""Durable Scout update admission and receipt mirror (executor is authoritative)."""
from sqlalchemy import Column, Float, JSON, String
from mino_nexus.core.database import Base


class ScoutUpdate(Base):
    __tablename__ = "scout_updates"
    operation_id = Column(String, primary_key=True)
    node_id = Column(String, nullable=False, index=True)
    owner_user_id = Column(String, nullable=False)
    slot = Column(String, nullable=True, unique=True)
    state = Column(String, nullable=False)
    params = Column(JSON, nullable=False)
    result = Column(JSON, nullable=False, default=dict)
    created_at = Column(Float, nullable=False)
    deadline = Column(Float, nullable=False)
    dispatched_at = Column(Float, nullable=True)
