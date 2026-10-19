from sqlalchemy import Column, String
from mino_nexus.core.database import Base


class CollabWorkspaceRead(Base):
    __tablename__ = 'collab_workspace_reads'
    topic_id = Column(String, primary_key=True)
    user_id = Column(String, primary_key=True)
    read_at = Column(String, nullable=False, default='')
