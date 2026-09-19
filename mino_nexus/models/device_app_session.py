from __future__ import annotations

from sqlalchemy import Column, Integer, String, Text

from mino_nexus.core.database import Base


class DeviceAppSession(Base):
    """设备 × App 包：UI 会话与绑定账号（测试资源登记簿）。"""

    __tablename__ = "device_app_sessions"

    pk = Column(Integer, primary_key=True, autoincrement=True)
    sn = Column(String, nullable=False, index=True)
    package_id = Column(String, nullable=False, index=True)
    app_version = Column(String, default="")
    session = Column(String, default="unknown")
    bound_account_id = Column(String, default="")
    identity_hint = Column(Text, default="")
    stale = Column(Integer, default=0)
    stale_reason = Column(String, default="")
    lease_run_id = Column(String, default="")
    observed_at = Column(String, default="")
    updated_at = Column(String, default="")
