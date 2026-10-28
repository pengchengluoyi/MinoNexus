"""同一条用例在安卓、iOS、Web 上各自的脚本。"""
from __future__ import annotations

from sqlalchemy import Column, String, Text

from mino_nexus.core.database import Base


class CasePlatformScript(Base):
    __tablename__ = "case_platform_scripts"

    project_id = Column(String, primary_key=True)
    case_id = Column(String, primary_key=True, default="")
    platform = Column(String, primary_key=True)
    run_body = Column(Text, default="")
    pretest_body = Column(Text, default="")
    posttest_body = Column(Text, default="")
    digest = Column(String, default="")
    updated_at = Column(String, default="")
