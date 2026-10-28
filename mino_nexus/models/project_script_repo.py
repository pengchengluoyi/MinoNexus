"""项目用例脚本仓库。库里只留地址，代码在工作目录的检出里。"""
from __future__ import annotations

from sqlalchemy import Column, String, Text

from mino_nexus.core.database import Base


class ProjectScriptRepo(Base):
    __tablename__ = "project_script_repos"

    project_id = Column(String, primary_key=True)
    url = Column(Text, default="")
    branch = Column(String, default="")
    updated_at = Column(String, default="")
