"""Read-only verification of persisted evidence. Never upgrades ordinary outcomes."""
from datetime import datetime, timezone


def _time(value):
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError):
        raise ValueError('review_evidence_time_invalid') from None


def validate(db, session, *, case, actor, context, boundaries):
    from mino_nexus.models.session_event import SessionEvent
    from mino_nexus.models.run import AppRegressionRun
    from mino_nexus.services.case_compilation import digest
    publication = case.get('v5_publication') or {}
    runtime = session.runtime_context or {} if session else {}
    verdict = session.verdict or {} if session else {}
    if (session is None or session.owner_user_id != actor or session.case_id != case['case_id']
            or runtime.get('project_id') != case['project_id'] or session.status != 'pass'
            or not session.finalization_id or not session.finished_at
            or runtime.get('executor_stopped') is not True
            or runtime.get('execution_mode') != 'v5' or verdict.get('verification_status') != 'verified_pass'
            or not publication.get('source_revision_id')
            or publication.get('source_revision_id') != case.get('source_revision_id')
            or publication.get('compile_job_id') != case.get('latest_compile_job_id')
            or digest(runtime.get('publication') or {}) != digest(publication)
            or runtime.get('frozen_revision') != publication.get('frozen_revision')):
        raise ValueError('review_evidence_mismatch')
    if context:
        observed = {**runtime, 'app_id': session.app_id}
        for key, value in context.items():
            if value and key not in {'action_scheme', 'provider_id'} and observed.get(key) != value:
                raise ValueError('review_evidence_context_mismatch')
        for key in ('action_scheme', 'provider_id'):
            if context.get(key) and context[key] != runtime.get(key):
                raise ValueError('review_evidence_context_mismatch')
    start, finish = _time(session.started_at), _time(session.finished_at)
    if finish < start or finish > datetime.now(timezone.utc):
        raise ValueError('review_evidence_time_invalid')
    if any(value and start < _time(value) for value in boundaries):
        raise ValueError('review_evidence_stale')
    task = db.get(AppRegressionRun, session.run_id)
    if task and (task.status == 'cancelled' or (task.payload or {}).get('status') == 'cancelled'):
        raise ValueError('review_evidence_cancelled')
    refs = verdict.get('event_refs') or []
    records = []
    for ref in refs:
        if ref.get('session_id') != session.session_id or type(ref.get('seq')) is not int:
            raise ValueError('review_evidence_reference_invalid')
        row = db.query(SessionEvent).filter_by(session_id=session.session_id, seq=ref['seq']).one_or_none()
        if row is None:
            raise ValueError('review_evidence_reference_missing')
        records.append(row)
    ends = [r for r in records if r.type == 'verification/verdict']
    bundles = [r for r in records if r.type == 'verification/evidence']
    if len(ends) != 1 or len(bundles) != 1 or ends[0].payload.get('verification_status') != 'verified_pass':
        raise ValueError('review_evidence_incomplete')
    required = {r['point_id'] for r in (publication.get('check_program_plan') or {}).get('checkpoints', []) if r.get('required')}
    checks = bundles[0].payload.get('checks') or []
    matched = [r for r in checks if r.get('required')]
    if (not required or len(matched) != len(required) or {r.get('point_id') for r in matched} != required
            or any(r.get('truth') != 'true' or not any(x.get('fresh') is True and (x.get('evaluation') or {}).get('truth') == 'true' for x in r.get('reads', [])) for r in matched)
            or set(verdict.get('consumed_required') or []) != required
            or set(ends[0].payload.get('consumed_required') or []) != required):
        raise ValueError('review_evidence_checks_incomplete')
    for check in matched:
        for read in check.get('reads') or []:
            ref = read.get('event_ref') or {}
            if ref.get('session_id') != session.session_id or type(ref.get('seq')) is not int:
                raise ValueError('review_evidence_reference_invalid')
            event = db.query(SessionEvent).filter_by(session_id=session.session_id, seq=ref['seq']).one_or_none()
            if event is None or event.type != 'device/observation' or event.seq >= bundles[0].seq:
                raise ValueError('review_evidence_reference_missing')
            raw = event.payload.get('raw_response') or {}
            if (read.get('fresh') is True and (event.payload.get('status') != 'pass'
                    or event.payload.get('capability_id') != 'read_web_target_facts'
                    or raw.get('observation') != read.get('observation') or raw.get('target_facts') != read.get('facts'))):
                raise ValueError('review_evidence_observation_mismatch')
    return {'session_id': session.session_id, 'finalization_id': session.finalization_id,
            'event_refs': refs, 'publication_digest': digest(publication), 'verification_status': 'verified_pass'}
