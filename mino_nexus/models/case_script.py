"""用例脚本与项目级前置/后置。case_id 为空串时表示项目级脚本。"""
from __future__ import annotations

from sqlalchemy import Column, String, Text

from mino_nexus.core.database import Base


class CaseScript(Base):
    __tablename__ = "case_scripts"

    project_id = Column(String, primary_key=True)
    case_id = Column(String, primary_key=True, default="")
    run_body = Column(Text, default="")
    pretest_body = Column(Text, default="")
    posttest_body = Column(Text, default="")
    digest = Column(String, default="")
    updated_at = Column(String, default="")
