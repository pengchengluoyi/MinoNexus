"""SQLite revision/job chain. Model calls never run in the writing transaction.

Leases fence results, not payments: expired claims become interrupted and require
an explicit new request. A cancelled model call may still finish remotely.
"""
from __future__ import annotations


from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import threading
import time
import uuid

from sqlalchemy import func, text
from mino_nexus.core.database import session_scope
from mino_nexus.core.execution_user import require_user_id, execution_user
from mino_nexus.models.case import ProjectCase
from mino_nexus.models.case_revision import CaseRevision
from mino_nexus.models.case_compile_job import CaseCompileJob

TERMINAL = {"succeeded", "failed", "interrupted", "cancelled"}
CHAIN_FIELDS = ("source_digest", "source_revision", "source_revision_id", "latest_compile_job_id", "compile_job",
                "effective_artifact", "compile_state")
COMPILED_META = ("step_program_keys", "v5_draft_step_program_keys", "key_compile_warnings",
                 "ui_automation", "compile_provenance", "resource_key", "review_requirements", "review_history")


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _maintainer(db, project, actor):
    from mino_nexus.models.auth import User
    from mino_nexus.models.project_member import ProjectMember
    from mino_nexus.services.auth_store import is_admin_role
    user = db.get(User, actor)
    # Same rule as project_members, evaluated in the held transaction. Its
    # legacy project-store reader may lazily write names in another session.
    return bool(project and user and (is_admin_role(user.role) or project.created_by == actor
        or db.get(ProjectMember, (project.id, actor)) is not None))


def owned(db, project_id, *, write=True):
    from mino_nexus.models.project import Project
    actor = require_user_id()
    project = db.get(Project, project_id)
    if project is None or write and not _maintainer(db, project, actor):
        raise ValueError("project_missing")
    return actor


def input_view(case):
    from mino_nexus.services.v5_publication import source_view
    # Scene/resource_key are compiler OUTPUTS, not author input. Feeding them
    # back invalidates input digests and prevents recompiling rejected artifacts.
    return {**source_view(case), **{key: deepcopy(case.get(key)) for key in
        ("case_id", "project_id", "app_id")}}


def build_identity():
    from pathlib import Path
    build_version = (Path(__file__).resolve().parents[1] / "VERSION").read_text().strip()
    from mino_nexus.services.case_step_key_compiler import _COMPILE_CONTRACT_REVISION
    from mino_nexus.services.case_resource_key_catalog import CATALOG_VERSION
    return {"nexus_version": build_version, "compiler_contract": _COMPILE_CONTRACT_REVISION,
            "resource_catalog_version": CATALOG_VERSION}


def dependency_digest(db, project_id, owner, *, compile_inserts=()):
    from pathlib import Path
    from sqlalchemy import select
    from mino_nexus.models import CatalogEntry, Project, App, NavFsm, NavFsmState, NavFsmEdge, UiControl, ProjectPoolConfig, Settings
    from mino_nexus.models.nav_flow_block_catalog import NavFlowBlockCatalog
    values = []
    for model in (CatalogEntry, Project, App, NavFsm, NavFsmState, NavFsmEdge, UiControl, ProjectPoolConfig, NavFlowBlockCatalog):
        statement = select(model.__table__)
        if model is Project:
            statement = statement.where(Project.id == project_id)
        elif "project_id" in model.__table__.c:
            statement = statement.where(model.__table__.c.project_id == project_id)
        rows = [dict(row) for row in db.execute(statement).mappings()]
        if model is UiControl and compile_inserts:
            # Only this job's exact new rows are compilation outputs. A
            # concurrent edit, delete or unrelated insertion remains visible.
            inserted = {digest(row) for row in compile_inserts}
            present = {digest(row) for row in rows}
            if not inserted <= present:
                values.append(["compile_insert_conflict", sorted(inserted - present)])
            rows = [row for row in rows if digest(row) not in inserted]
        values.append([model.__tablename__, sorted(rows, key=lambda row: json.dumps(row, sort_keys=True, default=str))])
    setting = db.get(Settings, "ai-user:" + owner)
    if setting:
        values.append(["provider_configuration", {column.name: getattr(setting, column.name) for column in Settings.__table__.columns}])
    root = Path(__file__).resolve().parents[1]
    paths = list((root / "prompts").rglob("*.md")) + list((root / "prompts").rglob("*.yaml"))
    paths += [root / "services" / name for name in ("case_compilation.py", "case_step_key_compiler.py",
        "case_step_span_llm.py", "case_resource_claim.py", "account_requirement_compile.py")]
    values.append(["compiler_files", [(str(path.relative_to(root)), hashlib.sha256(path.read_bytes()).hexdigest())
        for path in sorted(paths)]])
    return digest(values)


def job_view(job):
    return {"artifact_digest": digest(job.output) if job.output else None, "provenance": deepcopy((job.output or {}).get("provenance") or {}), **{key: deepcopy(getattr(job, key)) for key in ("job_id", "owner_user_id", "revision_id", "source_revision",
        "status", "parent_job_id", "input_digest", "build", "diagnostics", "activated", "cancel_requested",
        "created_at", "started_at", "finished_at")}}


def _snapshot(db, case, revision, actor, origin):
    from mino_nexus.models.project import App
    app_id = case.get("app_id")
    if app_id and app_id != case["project_id"]:
        app = db.get(App, app_id)
        if app is None or app.project_id != case["project_id"] :
            raise ValueError("compile_app_scope_mismatch")
    from mino_nexus.services.v5_publication import source_view
    frozen = input_view(case)
    row = CaseRevision(revision_id="rev-" + uuid.uuid4().hex, owner_user_id=actor,
        project_id=case["project_id"], case_id=case["case_id"], revision=revision,
        source=source_view(case), source_digest=digest(source_view(case)), compile_input=frozen,
        input_digest=digest(frozen), origin=deepcopy(origin), created_at=now())
    db.add(row)
    db.flush()
    return row


def _queue(db, row, revision, *, request_id, parent_job_id="", initial_status="queued"):
    from mino_nexus.services.control_registry import _assert_runtime_writable
    _assert_runtime_writable()
    actor = owned(db, row.project_id)
    if not request_id or len(request_id) > 160:
        raise ValueError("compile_request_id_required")
    existing = db.query(CaseCompileJob).filter_by(owner_user_id=actor, project_id=row.project_id,
        case_id=row.case_id, request_id=request_id).one_or_none()
    if existing:
        if existing.revision_id != revision.revision_id or existing.parent_job_id != parent_job_id:
            raise ValueError("compile_request_conflict")
        return existing
    job = CaseCompileJob(job_id="compile-" + uuid.uuid4().hex, owner_user_id=actor,
        project_id=row.project_id, case_id=row.case_id, revision_id=revision.revision_id,
        source_revision=revision.revision, request_id=request_id, parent_job_id=parent_job_id,
        status=initial_status, input_digest=revision.input_digest, build={**build_identity(),
            "dependency_digest": dependency_digest(db, row.project_id, actor)}, created_at=now())
    db.add(job)
    db.flush()
    extra = deepcopy(row.extra or {})
    extra.update(source_digest=revision.source_digest, source_revision=revision.revision, source_revision_id=revision.revision_id,
        v5_draft_revision=revision.revision, latest_compile_job_id=job.job_id,
        compile_job=job_view(job), compile_state=initial_status, effective_artifact=None,
        resource_key=deepcopy(revision.compile_input.get("resource_key")),
        case_scene=deepcopy(revision.compile_input.get("case_scene")))
    meta = dict(extra.get("meta") or {})
    for key in COMPILED_META:
        if key not in {"review_history", "resource_key"}:
            meta.pop(key, None)
    meta["resource_key"] = deepcopy(revision.compile_input.get("resource_key"))
    meta["ui_automation"] = {"coverable": False, "blocked_by": []}
    extra["meta"] = meta
    row.extra = extra
    return job


def record_change(db, row, previous=None, *, origin=None, initial_status="queued"):
    """Called in the same transaction as the case/receipt write."""
    from mino_nexus.services.case_store import _public
    actor = owned(db, row.project_id)
    current = _public(row)
    prior_id = (previous or {}).get("source_revision_id")
    prior = db.get(CaseRevision, prior_id) if prior_id else None
    if prior and (prior.project_id != row.project_id or prior.case_id != row.case_id):
        raise ValueError("case_revision_conflict")
    source_generation_changed = bool(origin and origin.get("kind") == "source_sync"
        and (not prior or (prior.origin or {}).get("generation") != origin.get("generation")))
    if prior and not source_generation_changed and digest(input_view(previous)) == digest(input_view(current)):
        # A metadata save cannot forge compile results, status or history.
        extra = deepcopy(row.extra or {})
        for key in CHAIN_FIELDS:
            if key in previous:
                extra[key] = deepcopy(previous[key])
            else:
                extra.pop(key, None)
        extra["v5_draft_revision"] = prior.revision
        extra["meta"] = {**(extra.get("meta") or {}), **{key: deepcopy((previous.get("meta") or {})[key])
            for key in COMPILED_META if key in (previous.get("meta") or {})}}
        row.extra = extra
        return db.get(CaseCompileJob, previous.get("latest_compile_job_id"))
    maximum = db.query(func.max(CaseRevision.revision)).filter_by(project_id=row.project_id, case_id=row.case_id).scalar() or 0
    if previous and not prior:
        number = max(maximum + 1, int(previous.get("v5_draft_revision") or 1))
        legacy = _snapshot(db, previous, number, actor, {"kind": "legacy_snapshot", "compile_origin_unknown": True})
        maximum = number
        if legacy.input_digest == digest(input_view(current)):
            return _queue(db, row, legacy, request_id="source:" + legacy.revision_id, initial_status=initial_status)
    revision = _snapshot(db, current, max(maximum + 1, int(current.get("v5_draft_revision") or 1)), actor,
        origin or {"kind": "save"})
    return _queue(db, row, revision, request_id="source:" + revision.revision_id, initial_status=initial_status)


def history(project_id, case_id):
    with session_scope() as db:
        actor = owned(db, project_id, write=False)
        case = db.query(ProjectCase).filter_by(project_id=project_id, case_id=case_id).one_or_none()
        if case is None:
            raise ValueError("case_missing")
        revisions = db.query(CaseRevision).filter_by(project_id=project_id, case_id=case_id).order_by(CaseRevision.revision.desc()).all()
        jobs = db.query(CaseCompileJob).filter_by(project_id=project_id, case_id=case_id).order_by(CaseCompileJob.created_at.desc()).all()
        return {"expected_revision": int((case.extra or {}).get("v5_draft_revision") or 1), "source_revision": (case.extra or {}).get("source_revision"),
            "source_revision_id": (case.extra or {}).get("source_revision_id"),
            "latest_compile_job_id": (case.extra or {}).get("latest_compile_job_id"),
            "effective_artifact": deepcopy((case.extra or {}).get("effective_artifact")),
            "publication": deepcopy((case.extra or {}).get("v5_publication")),
            "revisions": [{key: deepcopy(getattr(r, key)) for key in ("revision_id", "revision", "source", "source_digest", "input_digest", "origin", "created_at")} for r in revisions],
            "jobs": [job_view(j) for j in jobs]}


def enqueue(project_id, case_id, expected_revision, request_id, *, parent_job_id=""):
    from mino_nexus.services.case_store import _public
    with session_scope() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        actor = owned(db, project_id)
        row = db.query(ProjectCase).filter_by(project_id=project_id, case_id=case_id).one_or_none()
        if row is None:
            raise ValueError("case_missing")
        if int((row.extra or {}).get("v5_draft_revision") or 1) != expected_revision:
            raise ValueError("case_revision_conflict")
        # Resolve request identity before checking the parent. Retrying a lost
        # response must return the original new job, even if it already ran.
        existing = db.query(CaseCompileJob).filter_by(owner_user_id=actor, project_id=project_id,
            case_id=case_id, request_id=request_id).one_or_none()
        if existing:
            if existing.source_revision != expected_revision or existing.parent_job_id != parent_job_id:
                raise ValueError("compile_request_conflict")
            return job_view(existing)
        previous = _public(row)
        if not previous.get("source_revision_id"):
            record_change(db, row, previous, origin={"kind": "adopt_legacy"})
        revision = db.get(CaseRevision, row.extra["source_revision_id"])
        latest = db.get(CaseCompileJob, row.extra.get("latest_compile_job_id"))
        if parent_job_id:
            parent = db.get(CaseCompileJob, parent_job_id)
            if (parent is None or parent.project_id != project_id
                    or parent.case_id != case_id or parent.revision_id != revision.revision_id
                    or parent.status not in {"failed", "interrupted", "cancelled"} or latest.job_id != parent_job_id):
                raise ValueError("compile_recovery_conflict")
        elif latest and latest.status in {"queued", "running"}:
            # Reuse active work, including an adopted legacy job.
            return job_view(latest)
        return job_view(_queue(db, row, revision, request_id=request_id, parent_job_id=parent_job_id))



def _owner_matches(db, job):
    from mino_nexus.models.project import Project
    return _maintainer(db, db.get(Project, job.project_id), job.owner_user_id)


def _source_matches(db, job):
    revision = db.get(CaseRevision, job.revision_id)
    origin = revision.origin if revision else {}
    if origin.get("kind") != "source_sync":
        return True
    from mino_nexus.models.case_source import CaseSource, CaseSourceRow
    source = db.get(CaseSource, origin.get("source_id"))
    binding = db.query(CaseSourceRow).filter_by(source_id=origin.get("source_id"), case_id=job.case_id).one_or_none()
    return bool(source and binding and not binding.detached and source.generation == origin.get("generation"))


def get_job(project_id, case_id, job_id):
    with session_scope() as db:
        actor = owned(db, project_id, write=False)
        job = db.get(CaseCompileJob, job_id)
        if job is None or job.owner_user_id != actor or job.project_id != project_id or job.case_id != case_id:
            raise ValueError("compile_job_missing")
        return {**job_view(job), "output": deepcopy(job.output or {})}


def _provenance(compiled):
    from mino_nexus.models.dispatch import DispatchCall
    actual = []
    with session_scope() as db:
        for ref in compiled.get("compile_provenance") or []:
            record = db.get(DispatchCall, ref.get("dispatch_id")) if ref.get("dispatch_id") else None
            payload = (record.payload_json or {}) if record and record.owner_user_id == require_user_id() else {}
            actual.append({**ref, **{k: payload.get(k) for k in ("model", "provider_id", "prompt_version", "prompt_sha") if payload.get(k)}})
    return {"dispatch_refs": actual,
        "catalog_versions": {n: {kind: {key: plan.get(key) for key in
            ("catalog_version", "compile_contract_revision", "block_dependencies")}
            for kind, plan in bundle.items() if kind in {"do_program_plan", "check_program_plan"}}
            for n, bundle in (compiled.get("step_program_keys") or {}).items()}}

def _project_status(db, job):
    row = db.query(ProjectCase).filter_by(project_id=job.project_id, case_id=job.case_id).one_or_none()
    if row is not None and (row.extra or {}).get("latest_compile_job_id") == job.job_id:
        row.extra = {**row.extra, "compile_job": job_view(job), "compile_state": job.status}


def cancel(project_id, case_id, job_id, expected_revision):
    with session_scope() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        actor = owned(db, project_id)
        job = db.get(CaseCompileJob, job_id)
        case = db.query(ProjectCase).filter_by(project_id=project_id, case_id=case_id).one_or_none()
        if job is None or job.project_id != project_id or job.case_id != case_id or case is None:
            raise ValueError("compile_job_missing")
        if job.source_revision != expected_revision or (case.extra or {}).get("source_revision") != expected_revision:
            raise ValueError("case_revision_conflict")
        if job.status in {"queued", "running", "waiting_review", "waiting_budget", "blocked_config"}:
            job.cancel_requested = True
            job.status = "cancelled"
            job.finished_at = now()
            job.diagnostics = [{"reason_code": "compile_cancelled", "remote_call_may_finish": bool(job.claim_token), "cancelled_by": actor}]
            _project_status(db, job)
        return job_view(job)


def interrupt_expired():
    count = 0
    with session_scope() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        for job in db.query(CaseCompileJob).filter(CaseCompileJob.status == "running", CaseCompileJob.lease_until < time.time()).all():
            job.status, job.finished_at = "interrupted", now()
            job.diagnostics = [{"reason_code": "compile_interrupted", "explicit_recovery_required": True}]
            _project_status(db, job)
            count += 1
    return count


def claim(worker_id, *, lease_seconds=120):
    with session_scope() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        while True:
            job = db.query(CaseCompileJob).filter_by(status="queued").order_by(CaseCompileJob.created_at, CaseCompileJob.job_id).first()
            if job is None:
                return None
            case = db.query(ProjectCase).filter_by(project_id=job.project_id, case_id=job.case_id).one_or_none()
            current = (case.extra or {}) if case is not None else {}
            if current.get("latest_compile_job_id") != job.job_id or current.get("source_revision_id") != job.revision_id:
                job.status, job.finished_at = "cancelled", now()
                job.diagnostics = [{"reason_code": "compile_superseded" if case else "case_deleted"}]
                db.flush()
                continue
            if not _source_matches(db, job):
                job.status, job.finished_at = "cancelled", now()
                job.diagnostics = [{"reason_code": "source_generation_changed"}]
                _project_status(db, job)
                db.flush()
                continue
            if not _owner_matches(db, job):
                job.status, job.finished_at = "failed", now()
                job.diagnostics = [{"reason_code": "compile_owner_scope_changed"}]
                _project_status(db, job)
                db.flush()
                continue
            break
        # Queued work follows the current compiler after an update, not its enqueue-time binary.
        job.build = {**build_identity(), "dependency_digest": dependency_digest(db, job.project_id, job.owner_user_id)}
        job.status, job.claim_token, job.worker_id = "running", uuid.uuid4().hex, worker_id
        job.lease_until, job.started_at = time.time() + lease_seconds, now()
        _project_status(db, job)
        revision = db.get(CaseRevision, job.revision_id)
        from mino_nexus.services.event_migration import job_input
        return {**job_view(job), "automatic_source_build": job.request_id == "source:" + job.revision_id,
            "event_migration": job_input(db, job, case), "claim_token": job.claim_token, "owner_user_id": job.owner_user_id,
            "project_id": job.project_id, "case_id": job.case_id, "input": deepcopy(revision.compile_input)}


def heartbeat(job_id, token, *, lease_seconds=120):
    with session_scope() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        job = db.get(CaseCompileJob, job_id)
        if job is None or job.status != "running" or job.claim_token != token or job.lease_until < time.time():
            return False
        job.lease_until = time.time() + lease_seconds
        return True


def complete(job_id, token, output=None, diagnostics=None):
    """Store late output in history, activate only the exact current claim/input."""
    with session_scope() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        job = db.get(CaseCompileJob, job_id)
        if job is None or job.claim_token != token:
            return False
        # Output is write-once, including after cancellation/interruption.
        if job.output or job.status in {"succeeded", "failed"}:
            return False
        output = deepcopy(output or {})
        if output and not diagnostics:
            from mino_nexus.runtime.session_gate import clamp_case_scene
            try:
                output["case_scene"] = clamp_case_scene(output.get("case_scene"))
            except ValueError as exc:
                diagnostics = [{"reason_code": str(exc), "field": "case_scene"}]
        valid_claim = job.status == "running" and job.lease_until >= time.time() and not job.cancel_requested
        if job.status == "running" and not valid_claim:
            job.status = "interrupted"
        if valid_claim:
            job.status = "failed" if diagnostics else "succeeded"
            job.diagnostics = deepcopy(diagnostics or [])
        job.output, job.finished_at = output, now()
        row = db.query(ProjectCase).filter_by(project_id=job.project_id, case_id=job.case_id).one_or_none()
        from mino_nexus.services.case_store import _public
        matches = (row is not None and (row.extra or {}).get("latest_compile_job_id") == job.job_id
            and (row.extra or {}).get("source_revision_id") == job.revision_id
            and digest(input_view(_public(row))) == job.input_digest
            and _owner_matches(db, job) and _source_matches(db, job)
            and job.build.get("dependency_digest") == dependency_digest(db, job.project_id, job.owner_user_id,
                compile_inserts=output.get("generated_controls") or ()))
        if valid_claim and matches and not diagnostics:
            extra = deepcopy(row.extra or {})
            meta = dict(extra.get("meta") or {})
            meta.update(deepcopy(output.get("meta") or {}))
            if output.get("resource_key"):
                extra["resource_key"] = deepcopy(output["resource_key"])
                meta["resource_key"] = deepcopy(output["resource_key"])
            if output.get("case_scene"):
                extra["case_scene"] = deepcopy(output["case_scene"])
            extra["meta"] = meta
            extra["effective_artifact"] = {"job_id": job.job_id, "revision_id": job.revision_id,
                "source_revision": job.source_revision, "input_digest": job.input_digest, "output_digest": digest(output)}
            row.extra = extra
            job.activated = True
        _project_status(db, job)
        return bool(job.activated)


def readiness(case):
    if not case.get("source_revision_id"):
        return None  # Historical records remain readable; no invented provenance.
    from mino_nexus.services.v5_publication import source_view
    if case.get("source_digest") != digest(source_view(case)):
        return "compile_source_changed"
    job, artifact = case.get("compile_job") or {}, case.get("effective_artifact") or {}
    if (job.get("status") != "succeeded" or job.get("job_id") != artifact.get("job_id")
            or artifact.get("revision_id") != case.get("source_revision_id")
            or job.get("job_id") != case.get("latest_compile_job_id")):
        return "compile_artifact_unavailable" if job.get("status") == "succeeded" else "compile_" + str(job.get("status") or "required")
    current = build_identity()
    if any((job.get("build") or {}).get(key) != current[key]
            for key in ("compiler_contract", "resource_catalog_version")):
        return "compile_contract_changed"
    return None


def ensure_execution_compilation(project_id, case_ids):
    from mino_nexus.services.case_preflight import partition_cases
    from mino_nexus.services.case_store import _public

    repairable = {"compile_required", "compile_source_changed", "compile_contract_changed",
        "compile_artifact_unavailable", "requirements_need_compile", "precondition_coverage_missing",
        "precondition_scene_mismatch", "case_scene_required", "case_scene_recompile_required"}
    with session_scope() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        actor = owned(db, project_id, write=False)
        rows = {row.case_id: row for row in db.query(ProjectCase).filter(
            ProjectCase.project_id == project_id, ProjectCase.case_id.in_(case_ids)).all()}
        if not case_ids or any(case_id not in rows for case_id in case_ids):
            raise ValueError("所选用例为空、已删除或不属于当前项目；本次未创建任务。")
        cases, jobs, build = [], [], None
        for case_id in dict.fromkeys(case_ids):
            row = rows[case_id]
            latest_id = (row.extra or {}).get("latest_compile_job_id")
            latest = db.get(CaseCompileJob, latest_id) if latest_id else None
            if latest and latest.project_id == project_id and latest.case_id == case_id:
                if (row.extra or {}).get("compile_job") != job_view(latest):
                    _project_status(db, latest)
            else:
                latest = None
            case = _public(row)
            _, report = partition_cases([case])
            reasons = {item.get("reason") for blocked in report["blocked"] for item in blocked.get("blocked_by", [])}
            revision = db.get(CaseRevision, case["source_revision_id"]) if case.get("source_revision_id") else None
            same_input = bool(revision and revision.project_id == project_id and revision.case_id == case_id
                and revision.input_digest == digest(input_view(case)))
            active = latest and latest.status in {"queued", "running"} and same_input and latest.revision_id == revision.revision_id
            changed_before_call = latest and latest.status == "failed" and latest.diagnostics and all(
                item.get("reason_code") == "compile_input_or_build_changed" for item in latest.diagnostics)
            needs_repair = bool(reasons & repairable) or bool(changed_before_call)
            if active:
                jobs.append({"case_id": case_id, "job_id": latest.job_id, "status": latest.status})
            elif needs_repair and (not latest or latest.status == "succeeded" or not same_input or changed_before_call):
                owned(db, project_id)
                if not same_input:
                    latest = record_change(db, row, origin={"kind": "execution_preflight"})
                else:
                    if build is None:
                        build = {**build_identity(), "dependency_digest": dependency_digest(db, project_id, actor)}
                    request_id = "execution:" + digest([revision.revision_id, build])
                    existing = db.query(CaseCompileJob).filter_by(owner_user_id=actor, project_id=project_id,
                        case_id=case_id, request_id=request_id).one_or_none()
                    latest = existing or _queue(db, row, revision, request_id=request_id)
                if latest.status in {"queued", "running"}:
                    jobs.append({"case_id": case_id, "job_id": latest.job_id, "status": latest.status})
                case = _public(row)
            cases.append(case)
        return cases, jobs


def run_one(worker_id):
    job = claim(worker_id)
    if job is None:
        return False
    stopped = threading.Event()

    def keep_alive():
        while not stopped.wait(30):
            if not heartbeat(job["job_id"], job["claim_token"]):
                break

    thread = threading.Thread(target=keep_alive, daemon=True)
    thread.start()
    try:
        with execution_user(job["owner_user_id"]):
            with session_scope() as db:
                owned(db, job["project_id"])
                runtime_build = build_identity()
                if (digest(job["input"]) != job["input_digest"]
                        or any(job["build"].get(key) != value for key, value in runtime_build.items())
                        or job["build"].get("dependency_digest") != dependency_digest(db, job["project_id"], job["owner_user_id"])):
                    raise ValueError("compile_input_or_build_changed")
                revision = db.get(CaseRevision, job["revision_id"])
                cached = None
                if job.get("automatic_source_build") and revision and (revision.origin or {}).get("kind") == "source_sync":
                    for prior in db.query(CaseCompileJob).filter_by(owner_user_id=job["owner_user_id"],
                        project_id=job["project_id"], case_id=job["case_id"], input_digest=job["input_digest"],
                        status="succeeded").order_by(CaseCompileJob.finished_at.desc()):
                        if prior.output and prior.build == job["build"] and not prior.cancel_requested:
                            cached = {"job_id": prior.job_id, "output": deepcopy(prior.output)}
                            break
            if cached:
                output = cached["output"]
                output["provenance"] = {**output.get("provenance", {}), "reused_from_job_id": cached["job_id"],
                    "model_calls_this_build": 0}
                complete(job["job_id"], job["claim_token"], output)
                return True
            from mino_nexus.services.case_resource_claim import compile_execution_scene
            from mino_nexus.services.case_step_key_compiler import compile_case_step_program_keys
            if job.get('event_migration'):
                from mino_nexus.services.event_migration import compile_reviewed
                result = compile_reviewed(job)
            else:
                source = compile_execution_scene(deepcopy(job["input"]), project_id=job["project_id"])
                from mino_nexus.services.control_registry import capture_compile_controls
                with capture_compile_controls() as generated_controls:
                    compiled = compile_case_step_program_keys(source)
                result = {"resource_key": source.get("resource_key"), "case_scene": source.get("case_scene"),
                    "meta": compiled, "provenance": _provenance(compiled), "generated_controls": generated_controls}
            complete(job["job_id"], job["claim_token"], result)
    except Exception as exc:
        # Exception strings can contain provider URLs/credentials; retain a safe type/code.
        code = str(exc) if str(exc) in {"compile_input_or_build_changed", "project_missing",
            "compile_provider_unavailable", "compile_prompt_unavailable",
            "compile_model_call_failed", "compile_model_response_incomplete", "migration_contract_changed",
            "migration_source_changed", "migration_plan_incomplete", "migration_control_review_required",
            "business_event_migration_required", "case_scene_required", "case_scene_recompile_required",
            "case_scene_session_conflict", "scene_prep_items_invalid", "scene_prep_text_required",
            "scene_prep_kind_invalid", "scene_prep_phase_invalid", "obsolete_scene_template_id",
            "scene_lease_requirements_invalid", "foreign_foreground_policy_invalid",
            "case_scene_invalid:session_prep", "case_scene_invalid:required_session",
            "case_scene_invalid:device_need", "target_platform_invalid:platform"} else "compile_failed"
        complete(job["job_id"], job["claim_token"], diagnostics=[{"reason_code": code, "error_type": type(exc).__name__}])
    finally:
        stopped.set()
        thread.join(timeout=1)
    return True
