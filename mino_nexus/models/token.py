from __future__ import annotations

from sqlalchemy import Column, Integer, String

from mino_nexus.core.database import Base


class InstallToken(Base):
    __tablename__ = "install_tokens"

    token = Column(String, primary_key=True)
    user_id = Column(String, default="")
    expires_at = Column(Integer, default=0)
    created_at = Column(Integer, default=0)


class NodeCredential(Base):
    """Scout 首次 REGISTER（安装凭证）后换发的长期 node_token。"""

    __tablename__ = "node_credentials"

    node_id = Column(String, primary_key=True)
    token = Column(String, nullable=False, index=True)
    user_id = Column(String, default="")
    created_at = Column(Integer, default=0)
