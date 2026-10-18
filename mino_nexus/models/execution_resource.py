"""One fenced lease set per attempt; domain configuration is explicit."""
from sqlalchemy import Column, String, Float, JSON, Integer
from mino_nexus.core.database import Base


class ExecutionResource(Base):
    __tablename__ = "execution_resources"
    resource_key = Column(String, primary_key=True)
    project_id = Column(String, nullable=False, index=True)
    owner_user_id = Column(String, nullable=False)
    run_id = Column(String, nullable=False, index=True)
    execution_unit_id = Column(String, nullable=False)
    attempt_id = Column(String, nullable=False)
    sn = Column(String, nullable=False)
    epoch = Column(Integer, nullable=False, default=1)
    expires_at = Column(Float, nullable=False)
    state = Column(String, nullable=False, default="held")
    detail = Column(JSON, nullable=False, default=dict)


class ResourceDomain(Base):
    __tablename__ = "resource_domains"
    domain_id = Column(String, primary_key=True)
    project_id = Column(String, nullable=False, index=True)
    name = Column(String, nullable=False)
    env = Column(String, nullable=False)
    app_id = Column(String, nullable=False, default="")
    account_ids = Column(JSON, nullable=False, default=list)
    enabled = Column(Integer, nullable=False, default=1)
