from datetime import datetime, timedelta, timezone
import json
import os
from typing import Any

from sqlalchemy import func, or_

from mino_nexus.core.database import session_scope
from mino_nexus.core.execution_user import require_user_id
from mino_nexus.models.case_import_preview import CaseImportPreview


def _now() -> datetime:
    return datetime.now(timezone.utc)


def create(token: str, doc: dict[str, Any]) -> None:
    with session_scope() as database:
        database.query(CaseImportPreview).filter(
            CaseImportPreview.owner_user_id == require_user_id(),
            CaseImportPreview.expires_at <= _now().isoformat(),
            CaseImportPreview.worker_expires_at <= _now().isoformat(),
        ).delete(synchronize_session=False)
        database.add(CaseImportPreview(
            token=token,
            owner_user_id=require_user_id(),
            project_id=doc["project_id"],
            expires_at=(_now() + timedelta(hours=24)).isoformat(),
            payload=doc,
        ))


def get(token: str, project_id: str = "") -> dict[str, Any] | None:
    with session_scope() as database:
        query = database.query(CaseImportPreview).filter(
            CaseImportPreview.token == token,
            CaseImportPreview.owner_user_id == require_user_id(),
            CaseImportPreview.expires_at > _now().isoformat(),
        )
        if project_id:
            query = query.filter(CaseImportPreview.project_id == project_id)
        row = query.first()
        return dict(row.payload) if row else None


def update(token: str, doc: dict[str, Any]) -> bool:
    with session_scope() as database:
        query = database.query(CaseImportPreview).filter(
            CaseImportPreview.token == token,
            CaseImportPreview.owner_user_id == require_user_id(),
        )
        values = {
            CaseImportPreview.payload: doc,
            CaseImportPreview.expires_at: (_now() + timedelta(hours=24)).isoformat(),
        }
        return bool(query.update(values, synchronize_session=False))


def update_row(token: str, row: dict[str, Any], *, worker_id: str = "", restart: bool = False) -> bool:
    with session_scope() as database:
        query = database.query(CaseImportPreview).filter(
            CaseImportPreview.token == token,
            CaseImportPreview.owner_user_id == require_user_id(),
        )
        if worker_id:
            query = query.filter(CaseImportPreview.worker_id == worker_id, CaseImportPreview.payload["cancel"].as_boolean() == False)
        preview = query.first()
        if not preview:
            return False
        index = next((index for index, item in enumerate(preview.payload.get("rows") or []) if item.get("row_index") == row.get("row_index")), None)
        if index is None:
            return False
        payload = func.json_set(CaseImportPreview.payload, f"$.rows[{index}]", func.json(json.dumps(row, ensure_ascii=False)))
        if restart:
            payload = func.json_set(payload, "$.cancel", func.json("false"))
        values = {CaseImportPreview.payload: payload, CaseImportPreview.expires_at: (_now() + timedelta(hours=24)).isoformat()}
        if worker_id:
            values[CaseImportPreview.worker_expires_at] = (_now() + timedelta(minutes=15)).isoformat()
        return bool(query.update(values, synchronize_session=False))


def claim(token: str, worker_id: str) -> bool:
    with session_scope() as database:
        query = database.query(CaseImportPreview).filter(
            CaseImportPreview.token == token,
            CaseImportPreview.owner_user_id == require_user_id(),
            CaseImportPreview.expires_at > _now().isoformat(),
        )
        row = query.first()
        if not row:
            return False
        previous_worker = row.worker_id
        stale = False
        if previous_worker:
            try:
                process_id = int(previous_worker.split(":", 1)[0])
                if process_id > 0:
                    os.kill(process_id, 0)
            except ProcessLookupError:
                stale = True
            except (ValueError, PermissionError):
                pass
        available = or_(CaseImportPreview.worker_id == "", CaseImportPreview.worker_expires_at < _now().isoformat())
        if stale:
            available = or_(available, CaseImportPreview.worker_id == previous_worker)
        return bool(query.filter(available).update({
            CaseImportPreview.worker_id: worker_id,
            CaseImportPreview.worker_expires_at: (_now() + timedelta(minutes=15)).isoformat(),
        }, synchronize_session=False))


def release(token: str, worker_id: str) -> None:
    with session_scope() as database:
        database.query(CaseImportPreview).filter(
            CaseImportPreview.token == token,
            CaseImportPreview.owner_user_id == require_user_id(),
            CaseImportPreview.worker_id == worker_id,
        ).update({CaseImportPreview.worker_id: "", CaseImportPreview.worker_expires_at: ""}, synchronize_session=False)


def remove(token: str) -> None:
    with session_scope() as database:
        database.query(CaseImportPreview).filter(
            CaseImportPreview.token == token,
            CaseImportPreview.owner_user_id == require_user_id(),
        ).delete(synchronize_session=False)
