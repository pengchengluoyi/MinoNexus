"""Owner-scoped measurements from immutable revisions and durable compile jobs."""
from collections import Counter
from datetime import datetime, timedelta, timezone
import math
from sqlalchemy import func
from mino_nexus.core.database import session_scope
from mino_nexus.models.case_compile_job import CaseCompileJob
from mino_nexus.models.case_revision import CaseRevision


def _time(value):
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.timestamp() if parsed.tzinfo else None
    except (TypeError, ValueError, OverflowError):
        return None


def _elapsed(start, finish):
    a, b = _time(start), _time(finish)
    return b - a if a is not None and b is not None and b >= a else None


def _distribution(values):
    known = sorted(value for value in values if value is not None)
    return {"samples": len(known), "missing_samples": len(values) - len(known),
            "mean_seconds": round(sum(known) / len(known), 3) if known else None,
            "p95_seconds": round(known[math.ceil(len(known) * .95) - 1], 3) if known else None}


def summary(project_id, app_id, *, platform='', days=30):
    from mino_nexus.services.application_understanding import scope
    until = datetime.now(timezone.utc)
    since = until - timedelta(days=days)
    with session_scope() as db:
        actor = scope(db, project_id, app_id)
        query = db.query(CaseCompileJob, CaseRevision).join(CaseRevision,
            CaseCompileJob.revision_id == CaseRevision.revision_id).filter(
                CaseCompileJob.owner_user_id == actor, CaseRevision.owner_user_id == actor,
                CaseCompileJob.project_id == project_id, CaseRevision.project_id == project_id,
                CaseCompileJob.case_id == CaseRevision.case_id,
                CaseRevision.compile_input['app_id'].as_string() == app_id,
                func.julianday(CaseCompileJob.created_at) >= func.julianday(since.isoformat()),
                func.julianday(CaseCompileJob.created_at) <= func.julianday(until.isoformat()))
        if platform:
            query = query.filter(CaseRevision.compile_input['platform'].as_string() == platform)
        rows = query.all()
        statuses = Counter(job.status for job, _ in rows)
        completed = [job for job, _ in rows if job.status in {'succeeded', 'failed'}]
        success = [(job, revision) for job, revision in rows if job.status == 'succeeded']
        reused = 0
        unknown_reuse = 0
        for job, revision in success:
            provenance = (job.output or {}).get('provenance') or {}
            ref = provenance.get('reused_from_job_id')
            if not ref:
                # Missing provenance is not proof that a model was called.
                unknown_reuse += not bool(provenance.get('dispatch_refs'))
                continue
            prior = db.get(CaseCompileJob, ref)
            old_revision = db.get(CaseRevision, prior.revision_id) if prior else None
            valid = (prior is not None and prior.job_id != job.job_id and prior.status == 'succeeded'
                and not prior.cancel_requested and prior.owner_user_id == actor and prior.project_id == project_id
                and prior.case_id == job.case_id and prior.input_digest == job.input_digest and prior.build == job.build
                and old_revision is not None and old_revision.owner_user_id == actor
                and old_revision.project_id == project_id and old_revision.case_id == job.case_id
                and old_revision.compile_input.get('app_id') == app_id
                and old_revision.compile_input.get('platform') == revision.compile_input.get('platform')
                and type(provenance.get('model_calls_this_build')) is int and provenance['model_calls_this_build'] == 0
                and _elapsed(prior.finished_at, job.created_at) is not None)
            reused += bool(valid)
            unknown_reuse += not valid
        known_reuse = len(success) - unknown_reuse
        return {"project_id": project_id, "app_id": app_id, "owner_user_id": actor, "platform": platform,
            "window": {"days": days, "from": since.isoformat(), "to": until.isoformat(), "basis": "job_created_at"},
            "jobs": len(rows), "cases": len({job.case_id for job, _ in rows}), "statuses": dict(statuses),
            "failure": {"numerator": statuses['failed'], "denominator": len(completed),
                        "rate": statuses['failed'] / len(completed) if completed else None},
            "reuse": {"numerator": reused, "denominator": known_reuse, "unknown": unknown_reuse,
                      "rate": reused / known_reuse if known_reuse else None},
            "queue": _distribution([_elapsed(job.created_at, job.started_at) for job, _ in rows if job.started_at or job.status in {'succeeded', 'failed'}]),
            "execution": _distribution([_elapsed(job.started_at, job.finished_at) for job in completed]),
            "revision_to_success": _distribution([_elapsed(revision.created_at, job.finished_at) for job, revision in success]),
            "improvement_rate": None,
            "note": "按任务创建时间统计近指定天数；应用与平台来自冻结原文。失败率仅含明确成功/失败任务；中断、取消和等待单列。复用率仅含证据可判定的成功任务。原文入库至成功不是外部编辑至同步时延，也不代表业务通过；暂无有无提示对照证据。"}
