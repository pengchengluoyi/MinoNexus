"""项目维护者：可改该项目用例、环境、账号池与发布配置的账号。项目创建者隐式为维护者。"""
from sqlalchemy import Column, Integer, String

from mino_nexus.core.database import Base


class ProjectMember(Base):
    __tablename__ = "project_members"
    project_id = Column(String, primary_key=True)
    user_id = Column(String, primary_key=True)
    role = Column(String, nullable=False, default="maintainer")
    added_by = Column(String, default="")
    added_at = Column(Integer, default=0)
