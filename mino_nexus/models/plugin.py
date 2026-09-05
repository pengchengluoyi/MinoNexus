from __future__ import annotations

from sqlalchemy import JSON, Boolean, Column, String

from mino_nexus.core.database import Base


class PluginPolicy(Base):
    """集成插件是否对客户端可见 / 启用。不含密钥。"""

    __tablename__ = "plugin_policies"

    plugin_id = Column(String, primary_key=True)
    visible = Column(Boolean, default=True)
    enabled = Column(Boolean, default=True)


class UserPluginSecret(Base):
    """每用户集成配置（飞书/禅道/Figma key、机器人）。"""

    __tablename__ = "user_plugin_secrets"

    user_id = Column(String, primary_key=True)
    config = Column(JSON, default=dict)
    updated_at = Column(String, default="")
