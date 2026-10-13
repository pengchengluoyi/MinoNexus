from sqlalchemy import JSON, Column, String

from mino_nexus.core.database import Base


class CaseImportPreview(Base):
    __tablename__ = "case_import_previews"

    token = Column(String, primary_key=True)
    owner_user_id = Column(String, nullable=False, index=True)
    project_id = Column(String, nullable=False, index=True)
    expires_at = Column(String, nullable=False, index=True)
    worker_id = Column(String, nullable=False, default="")
    worker_expires_at = Column(String, nullable=False, default="")
    payload = Column(JSON, nullable=False, default=dict)
