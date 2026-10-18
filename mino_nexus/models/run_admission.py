from sqlalchemy import Column, String, UniqueConstraint

from mino_nexus.core.database import Base


class RunAdmission(Base):
    __tablename__ = "run_admissions"
    __table_args__ = (UniqueConstraint("owner_user_id", "request_id"),)
    id = Column(String, primary_key=True)
    owner_user_id = Column(String, nullable=False, index=True)
    request_id = Column(String, nullable=False)
    request_digest = Column(String, nullable=False)
    run_id = Column(String, nullable=False, index=True)
