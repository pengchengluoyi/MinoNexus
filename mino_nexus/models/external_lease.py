"""外部 agent 占用设备的软租约：只登记，不阻塞，冲突可见。"""
from sqlalchemy import Column, Float, String
from mino_nexus.core.database import Base


class ExternalLease(Base):
    __tablename__ = "external_leases"
    lease_id = Column(String, primary_key=True)
    run_id = Column(String, nullable=False, index=True)
    owner_user_id = Column(String, nullable=False, index=True)
    sn = Column(String, nullable=False, index=True)
    agent = Column(String, nullable=False, default="")
    starts_at = Column(Float, nullable=False)
    expires_at = Column(Float, nullable=False)
    released_at = Column(Float, nullable=True)
