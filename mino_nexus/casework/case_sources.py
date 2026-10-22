from __future__ import annotations

from copy import deepcopy
import time
import uuid

from sqlalchemy import text

from mino_nexus.core.database import session_scope
from mino_nexus.models.case import ProjectCase
from mino_nexus.models.case_source import CaseSource, CaseSourceRead, CaseSourceRow
from mino_nexus.casework.case_compilation import record_change
from mino_nexus.core.stable_digest import digest, now
from mino_nexus.platform.project_guard import owned

FIELDS = {"name", "precondition", "steps_raw", "expected_raw", "platform", "module"}


def view(row):
    return {key: deepcopy(getattr(row, key)) for key in (
        "source_id", "project_id", "generation", "kind", "configuration",
        "active_read_id", "failures", "last_receipt")}


def _get(db, project_id, source_id, *, write=True):
    actor = owned(db, project_id, write=write)
    row = db.get(CaseSource, source_id)
    if row is None or row.project_id != project_id or row.owner_user_id != actor:
        raise ValueError("source_missing")
    return row


def validate_configuration(kind, config):
    config = deepcopy(config)
    if kind == "case_api":
        from mino_nexus.casework.case_source_api import configuration
        return configuration(config)
    if kind not in {"file", "sheets", "upload"}:
        raise ValueError("source_kind_invalid")
    if not isinstance(config.get("columns"), dict) or not {"external_case_no", "steps_raw", "expected_raw"} <= set(config["columns"]):
        raise ValueError("source_column_mapping_required")
    if set(config["columns"]) - FIELDS - {"external_case_no"}:
        raise ValueError("source_column_mapping_invalid")
    if any(type(value) is not int or not 0 <= value < 256 for value in config["columns"].values()):
        raise ValueError("source_column_mapping_invalid")
    if type(config.get("header_rows", 1)) is not int or not 0 <= config.get("header_rows", 1) <= 100:
        raise ValueError("source_header_rows_invalid")
    if kind != "upload" and not str(config.get("node_id") or "").strip():
        raise ValueError("source_node_required")
    if kind == "file" and not str(config.get("path") or "").strip():
        raise ValueError("source_file_path_required")
    if kind == "sheets" and any(not config.get(key) for key in ("spreadsheet_token", "sheet_id", "range")):
        raise ValueError("source_sheets_range_required")
    return config


def save(project_id, body, source_id=""):
    configuration = validate_configuration(body.kind, body.configuration)
    with session_scope() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        actor = owned(db, project_id)
        if configuration.get("app_id"):
            from mino_nexus.models.project import App
            app = db.get(App, configuration["app_id"])
            if app is None or app.project_id != project_id:
                raise ValueError("source_app_scope_mismatch")
        if body.kind != "upload":
            from mino_nexus.resources.node_registry import get_registry
            node = get_registry().get_node(configuration["node_id"])
            if node is None or node.owner_user_id != actor:
                raise ValueError("source_node_scope_mismatch")
        if source_id:
            row = _get(db, project_id, source_id)
            if row.generation != body.expected_generation:
                raise ValueError("source_generation_conflict")
            rebinding = row.kind != body.kind or row.configuration != configuration
            if rebinding and not body.confirm_rebind:
                raise ValueError("source_rebind_confirmation_required")
            if rebinding:
                row.generation += 1
                row.active_read_id, row.read_lease_until = "", 0
        else:
            row = CaseSource(source_id="source-" + uuid.uuid4().hex, project_id=project_id,
                owner_user_id=actor, generation=1)
            db.add(row)
        row.kind, row.configuration = body.kind, configuration
        db.flush()
        return view(row)


def listing(project_id):
    with session_scope() as db:
        actor = owned(db, project_id, write=False)
        rows = db.query(CaseSource).filter_by(project_id=project_id, owner_user_id=actor).all()
        return {"items": [view(row) for row in rows]}


def discover(project_id, config, operation):
    from mino_nexus.casework.case_source_api import configuration
    from mino_nexus.casework.case_source_transport import read_source
    if operation not in {"projects", "sessions"}:
        raise ValueError("source_api_operation_invalid")
    config = configuration(config, operation=operation)
    with session_scope() as db:
        actor = owned(db, project_id)
    result = read_source({"configuration": config, "kind": "case_api", "owner_user_id": actor,
        "source_id": "discovery-" + uuid.uuid4().hex, "read_id": uuid.uuid4().hex, "generation": 1}, operation=operation)
    if result.get("complete") is not True or not isinstance(result.get("options"), list) or len(result["options"]) > 10000:
        raise ValueError("source_api_payload_invalid")
    from mino_nexus.casework.case_source_api import identifier, origin
    options = []
    for option in result["options"]:
        if not isinstance(option, dict) or not isinstance(option.get("label"), str):
            raise ValueError("source_api_payload_invalid")
        options.append({"id": identifier(option.get("id")), "label": option["label"][:500]})
    service_url = origin(result.get("service_url"))
    if config.get("service_url") and config["service_url"] != service_url:
        raise ValueError("source_api_service_mismatch")
    return {"items": options, "service_url": service_url}


def history(project_id, source_id, *, limit=50, offset=0):
    with session_scope() as db:
        row = _get(db, project_id, source_id, write=False)
        query = db.query(CaseSourceRead).filter_by(source_id=source_id)
        reads = query.order_by(CaseSourceRead.created_at.desc(), CaseSourceRead.read_id).offset(offset).limit(limit).all()
        mappings = db.query(CaseSourceRow).filter_by(source_id=source_id).order_by(CaseSourceRow.id).offset(offset).limit(limit).all()
        return {"source": view(row), "total": query.count(), "limit": limit, "offset": offset,
            "reads": [{key: deepcopy(getattr(read, key)) for key in ("read_id", "generation", "status", "created_at", "finished_at", "receipt")} for read in reads],
            "rows": [{key: deepcopy(getattr(item, key)) for key in ("external_case_no", "case_id", "source_position", "retired_candidate", "detached")} for item in mappings]}


def claim(project_id, source_id, expected_generation):
    with session_scope() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        row = _get(db, project_id, source_id)
        if row.generation != expected_generation:
            raise ValueError("source_generation_conflict")
        if row.active_read_id and row.read_lease_until > time.time():
            raise ValueError("source_read_in_progress")
        if row.active_read_id:
            previous = db.get(CaseSourceRead, row.active_read_id)
            if previous and previous.status == "reading":
                previous.status, previous.finished_at = "interrupted", now()
                previous.receipt = {"reason_code": "source_read_lease_expired"}
        read_id = "read-" + uuid.uuid4().hex
        db.add(CaseSourceRead(read_id=read_id, source_id=source_id, generation=row.generation,
            status="reading", created_at=now()))
        row.active_read_id, row.read_lease_until = read_id, time.time() + 180
        db.flush()
        return {**view(row), "read_id": read_id, "owner_user_id": row.owner_user_id}


def normalize_table(table, configuration):
    if not isinstance(table, list) or len(table) > 10000:
        raise ValueError("source_table_size_invalid")
    rows, seen = [], set()
    columns = configuration["columns"]
    for position, values in enumerate(table[configuration.get("header_rows", 1):], configuration.get("header_rows", 1) + 1):
        if not isinstance(values, list) or len(values) > 256:
            raise ValueError("source_row_invalid")
        if not any(value is not None and str(value).strip() for value in values):
            continue
        number_index = columns["external_case_no"]
        number = values[number_index] if number_index < len(values) else None
        if not isinstance(number, str) or not number.strip() or number != number.strip():
            raise ValueError("source_stable_text_case_number_required")
        if number in seen:
            raise ValueError("source_duplicate_case_number")
        seen.add(number)
        fields = {}
        for key, index in columns.items():
            if key == "external_case_no":
                continue
            value = values[index] if index < len(values) else ""
            if value is not None and not isinstance(value, (str, int, float, bool)):
                raise ValueError("source_cell_invalid")
            fields[key] = str(value) if value is not None else ""
            if len(fields[key]) > 60000:
                raise ValueError("source_cell_too_large")
        fields.setdefault("platform", configuration.get("default_platform", ""))
        if fields["platform"] not in {"", "web", "android", "ios", "mac", "win", "linux"}:
            raise ValueError("source_platform_invalid")
        fields.setdefault("name", number)
        fields.update(app_id=configuration.get("app_id", ""))
        import re
        match = re.match(r"[A-Z]+([0-9]+):", configuration.get("range") or "")
        row_offset = int(match[1]) - 1 if match else 0
        rows.append({"number": number, "fields": fields, "position": {"row": position + row_offset,
            "sheet_id": configuration.get("sheet_id") or configuration.get("worksheet", "")}})
    return rows


def finish(snapshot, result=None, *, error_code=""):
    rows = None
    if not error_code:
        try:
            if not result or result.get("complete") is not True:
                raise ValueError("source_read_incomplete")
            if snapshot["kind"] == "case_api":
                from mino_nexus.casework.case_source_api import normalize
                rows = normalize(result.get("records"), snapshot["configuration"])
            else:
                rows = normalize_table(result.get("table"), snapshot["configuration"])
        except (ValueError, TypeError) as exc:
            error_code = str(exc) if isinstance(exc, ValueError) else "source_result_invalid"
    with session_scope() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        source = db.get(CaseSource, snapshot["source_id"])
        read = db.get(CaseSourceRead, snapshot["read_id"])
        if read is None:
            raise ValueError("source_read_missing")
        if read.status != "reading":
            return deepcopy(read.receipt)
        current = bool(source and source.active_read_id == read.read_id and source.generation == snapshot["generation"]
            and source.read_lease_until > time.time() and source.owner_user_id == snapshot["owner_user_id"])
        if not current:
            read.status, read.finished_at, read.receipt = "superseded", now(), {"reason_code": "source_generation_changed"}
            return deepcopy(read.receipt)
        owned(db, source.project_id)
        source.active_read_id, source.read_lease_until = "", 0
        read.finished_at = now()
        if error_code:
            source.failures += 1
            read.status, read.receipt = "failed", {"read_id": read.read_id, "reason_code": error_code}
        else:
            from mino_nexus.casework.case_store import _public
            existing = {item.external_case_no: item for item in db.query(CaseSourceRow).filter_by(source_id=source.source_id)}
            changes, jobs, seen = [], [], set()
            for item in rows:
                number, fields = item["number"], item["fields"]
                seen.add(number)
                mapping = existing.get(number)
                if mapping and mapping.detached:
                    continue
                content = digest(fields)
                if mapping:
                    mapping.retired_candidate = 0
                    mapping.source_position = item["position"]
                    case = db.query(ProjectCase).filter_by(project_id=source.project_id, case_id=mapping.case_id).one_or_none()
                    if mapping.content_digest == content and case and ((case.extra or {}).get("source_binding") or {}).get("generation") == source.generation:
                        case.extra = {**(case.extra or {}), "source_binding": {
                            **case.extra["source_binding"], "position": item["position"], "read_id": read.read_id}}
                        continue
                    if case is None:
                        raise ValueError("source_bound_case_missing")
                    previous = _public(case)
                else:
                    case = ProjectCase(case_id="case-" + uuid.uuid4().hex[:16], project_id=source.project_id,
                        sort_index=0, extra={}, steps=[], expected=[])
                    db.add(case)
                    db.flush()
                    previous = None
                    mapping = CaseSourceRow(source_id=source.source_id, external_case_no=number, case_id=case.case_id,
                        content_digest=content, source_position=item["position"])
                    db.add(mapping)
                for key, value in fields.items():
                    if key != "app_id":
                        setattr(case, key, value)
                case.steps, case.expected = [], []
                case.source, case.updated_at = "bound_source", now()
                case.extra = {**(case.extra or {}), "app_id": fields["app_id"], "source_binding": {
                    "source_id": source.source_id, "generation": source.generation,
                    "external_case_no": number, "read_id": read.read_id, "position": item["position"]}}
                job = record_change(db, case, previous, origin={"kind": "source_sync", "source_id": source.source_id,
                    "generation": source.generation, "read_id": read.read_id}, initial_status="waiting_review")
                if job:
                    jobs.append({"case_id": case.case_id, "job_id": job.job_id, "status": job.status})
                mapping.content_digest = content
                changes.append(case.case_id)
            retired = []
            for number, mapping in existing.items():
                if number not in seen and not mapping.detached:
                    mapping.retired_candidate = 1
                    retired.append(mapping.case_id)
            source.failures = 0
            source.content_digest = digest(sorted((item["number"], digest(item["fields"])) for item in rows))
            read.status, read.receipt = "succeeded", {"read_id": read.read_id, "generation": source.generation,
                "rows": len(rows), "changed_case_ids": changes, "retired_candidates": retired,
                "jobs": jobs, "complete": True, "empty_snapshot": not rows}
        source.last_receipt = deepcopy(read.receipt)
        return deepcopy(read.receipt)


def detach(project_id, source_id, case_id, expected_generation):
    with session_scope() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        source = _get(db, project_id, source_id)
        if source.generation != expected_generation:
            raise ValueError("source_generation_conflict")
        mapping = db.query(CaseSourceRow).filter_by(source_id=source_id, case_id=case_id).one_or_none()
        case = db.query(ProjectCase).filter_by(project_id=project_id, case_id=case_id).one_or_none()
        if mapping is None or case is None:
            raise ValueError("source_bound_case_missing")
        if mapping.detached:
            return {"generation": source.generation, "case_id": case_id, "detached": True}
        source.generation += 1
        mapping.detached = 1
        extra = deepcopy(case.extra or {})
        extra.pop("source_binding", None)
        job_id = extra.get("latest_compile_job_id")
        from mino_nexus.models.case_compile_job import CaseCompileJob
        job = db.get(CaseCompileJob, job_id) if job_id else None
        if job and job.status not in {"succeeded", "failed", "cancelled", "interrupted"}:
            job.status, job.cancel_requested, job.finished_at = "cancelled", True, now()
            from mino_nexus.casework.case_compilation import job_view
            extra.update(compile_state=job.status, compile_job=job_view(job))
        case.extra = extra
        case.source = "platform"
        return {"generation": source.generation, "case_id": case_id, "detached": True}


def refresh(project_id, source_id, expected_generation, table=None):
    snapshot = claim(project_id, source_id, expected_generation)
    if snapshot["kind"] == "upload":
        return finish(snapshot, {"complete": True, "table": table})
    try:
        from mino_nexus.casework.case_source_transport import read_source
        result = read_source(snapshot)
    except Exception as exc:
        code = str(exc)
        return finish(snapshot, error_code=code if isinstance(exc, ValueError) and code.startswith("source_") else "source_node_read_failed")
    return finish(snapshot, result)
