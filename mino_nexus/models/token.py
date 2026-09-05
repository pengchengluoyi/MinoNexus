from __future__ import annotations

from sqlalchemy import Column, Integer, String

from mino_nexus.core.database import Base


class InstallToken(Base):
    __tablename__ = "install_tokens"

    token = Column(String, primary_key=True)
    user_id = Column(String, default="")
    expires_at = Column(Integer, default=0)
    created_at = Column(Integer, default=0)
