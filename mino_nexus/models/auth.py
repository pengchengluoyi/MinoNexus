from __future__ import annotations

from sqlalchemy import JSON, Column, Integer, String, Text

from mino_nexus.core.database import Base


class User(Base):
    __tablename__ = "users"

    user_id = Column(String, primary_key=True)
    username = Column(String, default="")
    name = Column(String, default="")
    role = Column(String, default="user")
    password_hash = Column(Text, default="")
    extra = Column(JSON, default=dict)


class AuthSession(Base):
    __tablename__ = "sessions"

    token = Column(String, primary_key=True)
    user_id = Column(String, index=True, default="")
    extra = Column(JSON, default=dict)
    expires_at = Column(Integer, default=0)


class AuthState(Base):
    """codes / agent_sessions 等尚未拆开的账号附属状态。"""

    __tablename__ = "auth_state"

    id = Column(String, primary_key=True, default="main")
    payload = Column(JSON, default=dict)
