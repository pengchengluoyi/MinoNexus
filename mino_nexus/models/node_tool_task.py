"""Nexus projection; Scout receipts remain the installation authority."""
from sqlalchemy import Column, String, JSON, Float, UniqueConstraint
from mino_nexus.core.database import Base


class NodeToolTask(Base):
    __tablename__ = 'node_tool_tasks'
    __table_args__ = (UniqueConstraint('owner_user_id', 'operation_id', name='uq_tool_operation_owner'),)
    task_id = Column(String, primary_key=True)
    owner_user_id = Column(String, nullable=False, index=True)
    operation_id = Column(String, nullable=False)
    node_id = Column(String, nullable=False, index=True)
    tool_id = Column(String, nullable=False)
    action = Column(String, nullable=False)
    active_slot = Column(String, unique=True, nullable=True)
    process_generation = Column(String, nullable=False)
    state = Column(String, nullable=False)
    snapshot = Column(JSON, default=dict)
    created_at = Column(Float, nullable=False)
    updated_at = Column(Float, nullable=False)
