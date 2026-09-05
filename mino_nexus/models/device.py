from __future__ import annotations

from sqlalchemy import JSON, Column, String, Text

from mino_nexus.core.database import Base


class Device(Base):
    """设备身份。连通性权威在 Scout 心跳，channels/status 只是缓存。"""

    __tablename__ = "m_device"

    sn = Column(String, primary_key=True)
    platform = Column(String, default="")
    model = Column(String, default="")
    node_id = Column(String, default="", index=True)
    studio_id = Column(String, default="")
    owner_user_id = Column(String, default="")
    owner_name = Column(String, default="")
    password = Column(Text, default="")
    status = Column(String, default="")
    channels = Column(JSON, default=dict)
    extra = Column(JSON, default=dict)
    updated_at = Column(String, default="")
