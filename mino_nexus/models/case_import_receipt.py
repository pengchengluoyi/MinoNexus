from sqlalchemy import JSON, Column, String, UniqueConstraint

from mino_nexus.core.database import Base


class CaseImportReceipt(Base):
    __tablename__ = "case_import_receipts"
    __table_args__ = (UniqueConstraint("owner_user_id", "project_id", "client_request_id", name="uq_case_import_request"),)

    receipt_id = Column(String, primary_key=True)
    owner_user_id = Column(String, nullable=False, index=True)
    project_id = Column(String, nullable=False, index=True)
    client_request_id = Column(String, nullable=False)
    request_digest = Column(String, nullable=False)
    snapshot = Column(JSON, nullable=False)
    result = Column(JSON, default=dict)
    created_at = Column(String, nullable=False)
    updated_at = Column(String, nullable=False)
