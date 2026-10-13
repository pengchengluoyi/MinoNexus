from sqlalchemy import JSON, Boolean, Column, Integer, String
from mino_nexus.core.database import Base


class AssistantReceipt(Base):
    __tablename__ = "assistant_operation_receipts"
    key = Column(String, primary_key=True)
    owner_user_id = Column(String, nullable=False, index=True)
    kind = Column(String, nullable=False)
    state = Column(String, nullable=False)
    lease_token = Column(String, default="")
    lease_until = Column(Integer, default=0)
    side_effects = Column(Boolean, default=False)
    data_json = Column(JSON, default=dict)
    updated_at = Column(Integer, default=0)


class AssistantNotification(Base):
    __tablename__ = "assistant_notification_delivery"
    subscription_id = Column(Integer, primary_key=True)
    owner_user_id = Column(String, nullable=False, index=True)
    target_id = Column(String, nullable=False)
    delivered_segments = Column(Integer, default=0)
    attempts = Column(Integer, default=0)
    next_retry_at = Column(Integer, default=0, index=True)
    lease_token = Column(String, default="")
    lease_until = Column(Integer, default=0)
    state = Column(String, default="pending")
    last_error = Column(String, default="")
