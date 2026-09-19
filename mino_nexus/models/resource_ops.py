from __future__ import annotations

from sqlalchemy import JSON, Column, Integer, String, Text

from mino_nexus.core.database import Base


class ResourceTransitionRule(Base):
    """配置化资源状态转移：trigger_id + platform → effects。"""

    __tablename__ = "resource_transition_rules"

    rule_id = Column(String, primary_key=True)
    trigger_id = Column(String, nullable=False, index=True)
    platform = Column(String, default="any", index=True)
    label = Column(String, default="")
    effects_json = Column(JSON, default=list)
    enabled = Column(Integer, default=1)
    updated_at = Column(String, default="")


class ResourceTransitionAudit(Base):
    """转移审计（与 session log resource/transition 镜像）。"""

    __tablename__ = "resource_transition_audit"

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(String, default="", index=True)
    case_id = Column(String, default="", index=True)
    session_id = Column(String, default="", index=True)
    sn = Column(String, default="", index=True)
    package_id = Column(String, default="")
    trigger_id = Column(String, default="", index=True)
    source = Column(String, default="")
    platform = Column(String, default="")
    payload_json = Column(JSON, default=dict)
    created_at = Column(String, default="")


class DeviceResourceLease(Base):
    """设备 × App 跑批占用（与账号 lease 平行）。"""

    __tablename__ = "device_resource_leases"

    id = Column(Integer, primary_key=True, autoincrement=True)
    sn = Column(String, nullable=False, index=True)
    package_id = Column(String, nullable=False, index=True)
    run_id = Column(String, nullable=False, index=True)
    case_id = Column(String, default="")
    project_id = Column(String, default="")
    platform = Column(String, default="android")
    leased_at = Column(String, default="")
    expires_at = Column(String, default="")
