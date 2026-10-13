from sqlalchemy import JSON, Column, Float, Index, Integer, String
from mino_nexus.core.database import Base


class ModelUsage(Base):
    """一条实际 SDK 尝试一行；不存密钥、提示词、截图或模型回复。"""
    __tablename__ = "model_usage"
    id = Column(String, primary_key=True)
    owner_user_id = Column(String, nullable=False, index=True)
    at = Column(String, nullable=False, index=True)
    task_id = Column(String, default="", index=True)
    case_id = Column(String, default="", index=True)
    session_id = Column(String, default="", index=True)
    step_index = Column(Integer, nullable=True)
    turn = Column(Integer, nullable=True)
    provider_id = Column(String, default="")
    model = Column(String, default="")
    kind = Column(String, default="chat")
    job = Column(String, default="")
    status = Column(String, default="")
    prompt_tokens = Column(Integer, nullable=True)
    completion_tokens = Column(Integer, nullable=True)
    total_tokens = Column(Integer, nullable=True)
    estimated_cost_usd = Column(Float, nullable=True)
    payload_json = Column(JSON, default=dict)
    __table_args__ = (Index("ix_model_usage_owner_task_case_step", "owner_user_id", "task_id", "case_id", "step_index"),)
