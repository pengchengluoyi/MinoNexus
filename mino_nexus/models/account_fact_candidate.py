"""Evidence-bound candidate facts; never used by allocation before confirmation."""
from sqlalchemy import Column, String, JSON, UniqueConstraint
from mino_nexus.core.database import Base

class AccountFactCandidate(Base):
    __tablename__ = 'account_fact_candidates'
    __table_args__ = (UniqueConstraint('owner_user_id', 'session_id', 'account_id', name='uq_account_fact_session'),)
    candidate_id = Column(String, primary_key=True)
    owner_user_id = Column(String, nullable=False, index=True)
    project_id = Column(String, nullable=False, index=True)
    account_id = Column(String, nullable=False, index=True)
    session_id = Column(String, nullable=False)
    run_id = Column(String, nullable=False)
    case_id = Column(String, nullable=False)
    env = Column(String, nullable=False)
    source = Column(String, nullable=False)
    before = Column(JSON, nullable=False)
    proposed = Column(JSON, nullable=False)
    evidence_context = Column(JSON, nullable=False, default=dict)
    status = Column(String, nullable=False, default='candidate')
    created_at = Column(String, nullable=False)
    decision = Column(JSON, nullable=False, default=dict)


class AccountFactRule(Base):
    """Maintainer-reviewed mapping from a frozen business assertion to one fact."""
    __tablename__ = 'account_fact_rules'
    rule_id = Column(String, primary_key=True)
    project_id = Column(String, nullable=False, index=True)
    app_id = Column(String, nullable=False, index=True)
    definition = Column(JSON, nullable=False)
    updated_by = Column(String, nullable=False)
    updated_at = Column(String, nullable=False)
