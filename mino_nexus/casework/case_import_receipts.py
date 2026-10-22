from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import re
import time
import uuid

from sqlalchemy import func, text

from mino_nexus.core.database import session_scope
from mino_nexus.platform.identity import require_user_id
from mino_nexus.models.case import ProjectCase
from mino_nexus.models.case_import_receipt import CaseImportReceipt
from mino_nexus.models.case_import_receipt_line import CaseImportReceiptLine


def _now():
    return datetime.now(timezone.utc).isoformat()


def request_identity(request, client_request_id, preview_token):
    identity = client_request_id or ("preview:" + preview_token if preview_token else "")
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", identity):
        raise ValueError("缺少有效的 client_request_id，请使用稳定提交身份")
    encoded = json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return identity, hashlib.sha256(encoded.encode()).hexdigest()


def _owned(database, project_id, *, client_request_id=None, receipt_id=None):
    query = database.query(CaseImportReceipt).filter_by(project_id=project_id, owner_user_id=require_user_id())
    return query.filter_by(client_request_id=client_request_id).one_or_none() if client_request_id else query.filter_by(receipt_id=receipt_id).one_or_none()


def find(project_id, client_request_id, digest=None):
    with session_scope() as database:
        receipt = _owned(database, project_id, client_request_id=client_request_id)
        if receipt is None:
            return None
        if digest is not None and receipt.request_digest != digest:
            raise ValueError("import_request_conflict")
        return receipt.receipt_id


def create(project_id, client_request_id, digest, snapshot):
    with session_scope() as database:
        database.execute(text("BEGIN IMMEDIATE"))
        existing = _owned(database, project_id, client_request_id=client_request_id)
        if existing is not None:
            if existing.request_digest != digest:
                raise ValueError("import_request_conflict")
            return existing.receipt_id
        receipt_id = "import-" + uuid.uuid4().hex
        timestamp = _now()
        database.add(CaseImportReceipt(receipt_id=receipt_id, owner_user_id=require_user_id(),
            project_id=project_id, client_request_id=client_request_id, request_digest=digest,
            snapshot=deepcopy(snapshot), result={}, created_at=timestamp, updated_at=timestamp))
        for ordinal, item in enumerate(snapshot["rows"]):
            database.add(CaseImportReceiptLine(receipt_id=receipt_id, ordinal=ordinal,
                input_row=deepcopy(item), status="pending", result={}, case_snapshot={}))
        return receipt_id


def _line_result(line, status, **extra):
    return {"line_id": str(line.id), "row_index": line.input_row.get("row_index"), "status": status, **extra}


def _finish(database, receipt):
    lines = database.query(CaseImportReceiptLine).filter_by(receipt_id=receipt.receipt_id).order_by(CaseImportReceiptLine.ordinal).all()
    if any(line.status in {"pending", "preparing"} for line in lines) or receipt.result:
        return
    counts = {key: sum(line.status == key for line in lines) for key in ("created", "updated", "skipped", "blocked", "failed")}
    counts["skipped"] += sum(line.status == "excluded" for line in lines)
    total = database.query(func.count(ProjectCase.pk)).filter_by(project_id=receipt.project_id,
        requirement_id=receipt.snapshot["requirement_id"]).scalar()
    receipt.result = {**counts, "total": int(total or 0), "results": [deepcopy(line.result) for line in lines]}
    receipt.updated_at = _now()


def get(project_id, *, receipt_id=None, client_request_id=None):
    with session_scope() as database:
        receipt = _owned(database, project_id, receipt_id=receipt_id, client_request_id=client_request_id)
        if receipt is None:
            return None
        lines = database.query(CaseImportReceiptLine).filter_by(receipt_id=receipt.receipt_id).order_by(CaseImportReceiptLine.ordinal).all()
        counts = {key: sum(line.status == key for line in lines) for key in ("created", "updated", "skipped", "blocked", "failed")}
        counts["skipped"] += sum(line.status == "excluded" for line in lines)
        pending = sum(line.status in {"pending", "preparing"} for line in lines)
        results = [deepcopy(line.result) if line.result else _line_result(line, line.status) for line in lines]
        return {**counts, **deepcopy(receipt.result or {}), "project_id": receipt.project_id,
            "requirement_id": receipt.snapshot["requirement_id"], "receipt_id": receipt.receipt_id,
            "client_request_id": receipt.client_request_id, "receipt_state": "pending" if pending else "completed",
            "rows": deepcopy(receipt.snapshot["rows"]),
            "pending": pending, "results": results if pending else deepcopy(receipt.result.get("results", results)),
            "updated_at": receipt.updated_at, "action": "imported_cases"}


def resume(project_id, receipt_id):
    from mino_nexus.casework.case_import import prepare_receipt_line
    from mino_nexus.casework.case_store import apply_prepared_import

    with session_scope() as database:
        receipt = _owned(database, project_id, receipt_id=receipt_id)
        if receipt is None:
            raise ValueError("import_receipt_missing")
        snapshot = deepcopy(receipt.snapshot)
        line_ids = [line.id for line in database.query(CaseImportReceiptLine).filter_by(receipt_id=receipt_id).order_by(CaseImportReceiptLine.ordinal)]
    for line_id in line_ids:
        token = uuid.uuid4().hex
        with session_scope() as database:
            database.execute(text("BEGIN IMMEDIATE"))
            receipt = _owned(database, project_id, receipt_id=receipt_id)
            line = database.get(CaseImportReceiptLine, line_id)
            if receipt is None or line is None or line.receipt_id != receipt_id:
                raise ValueError("import_receipt_missing")
            if line.status not in {"pending", "preparing"}:
                continue
            if line.status == "preparing":
                if line.prepare_until > time.time():
                    continue
                # Preparation is deterministic; explicit resume can reclaim it.
            line.status = "preparing"
            line.prepare_token = token
            line.prepare_until = time.time() + 300
            item = deepcopy(line.input_row)
        try:
            prepared = prepare_receipt_line(snapshot, item)
        except ValueError as exc:
            prepared = {"status": "blocked" if str(exc) == "import_target_changed" else "failed",
                        "reason": "import_target_changed" if str(exc) == "import_target_changed" else "import_row_invalid"}
        except (TypeError, KeyError):
            prepared = {"status": "failed", "reason": "import_row_invalid"}
        with session_scope() as database:
            database.execute(text("BEGIN IMMEDIATE"))
            receipt = _owned(database, project_id, receipt_id=receipt_id)
            line = database.get(CaseImportReceiptLine, line_id)
            if receipt is None or line is None or line.status != "preparing" or line.prepare_token != token:
                continue
            if "fields" in prepared:
                try:
                    with database.begin_nested():
                        prepared["origin"] = {"kind": "import", "receipt_id": receipt_id,
                            "line_id": str(line.id), "row_index": line.input_row.get("row_index")}
                        status, saved = apply_prepared_import(database, prepared)
                    line.status = status
                    line.case_snapshot = saved
                    line.result = _line_result(line, status, case_id=saved["case_id"],
                        revision=saved.get("source_revision"), revision_id=saved.get("source_revision_id"),
                        compile_job_id=saved.get("latest_compile_job_id"), source_digest=saved.get("source_digest"))
                except ValueError as exc:
                    line.status = "blocked"
                    line.result = _line_result(line, "blocked", reason=str(exc))
            else:
                line.status = prepared["status"]
                line.result = _line_result(line, **prepared)
            line.prepare_token = ""
            line.prepare_until = 0
            receipt.updated_at = _now()
            _finish(database, receipt)
    return get(project_id, receipt_id=receipt_id)
