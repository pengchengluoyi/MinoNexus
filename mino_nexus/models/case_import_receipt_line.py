from sqlalchemy import JSON, Column, Float, Integer, String, UniqueConstraint

from mino_nexus.core.database import Base


class CaseImportReceiptLine(Base):
    __tablename__ = "case_import_receipt_lines"
    __table_args__ = (UniqueConstraint("receipt_id", "ordinal", name="uq_case_import_receipt_line"),)

    id = Column(Integer, primary_key=True)
    receipt_id = Column(String, nullable=False, index=True)
    ordinal = Column(Integer, nullable=False)
    input_row = Column(JSON, nullable=False)
    status = Column(String, nullable=False, default="pending")
    result = Column(JSON, default=dict)
    case_snapshot = Column(JSON, default=dict)
    prepare_token = Column(String, default="")
    prepare_until = Column(Float, default=0)
