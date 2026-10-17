from sqlalchemy import JSON, Column, Float, Integer, String
from mino_nexus.core.database import Base


class DevelopmentBinding(Base):
    __tablename__ = "development_bindings"
    app_id = Column(String, primary_key=True)
    revision = Column(Integer, nullable=False, default=0)
    owner_user_id = Column(String, nullable=False)
    config = Column(JSON, nullable=False, default=dict)
    checked_revision = Column(Integer, default=0)
    check = Column(JSON, default=dict)


class DevelopmentOperation(Base):
    __tablename__ = "development_operations"
    operation_id = Column(String, primary_key=True)
    app_id = Column(String, nullable=False, index=True)
    owner_user_id = Column(String, nullable=False, index=True)
    node_id = Column(String, nullable=False)
    action = Column(String, nullable=False)
    revision = Column(Integer, nullable=False)
    # Unique live slot prevents duplicate execution across HTTP workers.
    slot = Column(String, unique=True, nullable=True)
    state = Column(String, nullable=False)
    params = Column(JSON, nullable=False)
    result = Column(JSON, default=dict)
    created_at = Column(Float, nullable=False)
    deadline = Column(Float, nullable=False)
