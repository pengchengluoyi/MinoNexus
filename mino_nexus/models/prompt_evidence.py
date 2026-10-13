from sqlalchemy import JSON, Column, Integer, String
from mino_nexus.core.database import Base


class PromptEvidence(Base):
    """不可编辑模板证据；运行时依然只加载仓库 prompts/。"""
    __tablename__ = "prompt_evidence"
    sha = Column(String, primary_key=True)
    job_id = Column(String, nullable=False)
    version = Column(Integer, nullable=False)
    template_json = Column(JSON, nullable=False)
