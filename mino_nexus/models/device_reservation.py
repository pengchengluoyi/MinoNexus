"""Atomic admission for interactive desktops and existing device/Web slots."""
from sqlalchemy import Column, String
from mino_nexus.core.database import Base


class DeviceReservation(Base):
    __tablename__ = "device_reservations"
    resource_key = Column(String, primary_key=True)
    run_id = Column(String, nullable=False, index=True)
