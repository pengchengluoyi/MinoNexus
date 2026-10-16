"""CAS-reviewed migration jobs; preserve original source and published history."""
from copy import deepcopy
from sqlalchemy import text
from mino_nexus.services.case_compilation import digest, owned, now, job_view, _queue, readiness
from mino_nexus.services.business_event_contract import migration_candidate, references, VERSION


def preview(case):
    from mino_nexus.services.case_review import _state
    meta = case.get('meta') or {}
    plans = meta.get('v5_draft_step_program_keys') or meta.get('step_program_keys') or {}
    result = migration_candidate(plans)
    result.update(expected_revision=int(case.get('v5_draft_revision') or 1),
        expected_job_id=case.get('latest_compile_job_id') or '', state_digest=_state(case),
        project_id=case['project_id'], case_id=case['case_id'])
    result['candidate_digest'] = digest(result['candidate'])
    if not plans or readiness(case):
        result['status'] = 'blocked'
        result['blockers'].append({'reason_code': 'migration_requires_completed_compilation'})
    return result


def apply(project_id, case_id, *, expected_revision, expected_job_id, state_digest,
          candidate_digest, request_id, reason):
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.case import ProjectCase
    from mino_nexus.models.case_revision import CaseRevision
    from mino_nexus.models.case_compile_job import CaseCompileJob
    from mino_nexus.services.case_store import _public
    from mino_nexus.services.control_registry import _assert_runtime_writable
    _assert_runtime_writable()
    if not reason.strip() or not request_id or len(request_id) > 100:
        raise ValueError('migration_review_required')
    fingerprint = digest([expected_revision, expected_job_id, state_digest, candidate_digest, reason])
    with session_scope() as db:
        db.execute(text('BEGIN IMMEDIATE'))
        actor = owned(db, project_id)
        row = db.query(ProjectCase).filter_by(project_id=project_id, case_id=case_id).one_or_none()
        if row is None:
            raise ValueError('case_missing')
        receipts = deepcopy((row.extra or {}).get('event_migrations') or {})
        receipt = receipts.get(request_id)
        if receipt:
            if receipt['actor'] != actor or receipt['fingerprint'] != fingerprint:
                raise ValueError('migration_request_conflict')
            return job_view(db.get(CaseCompileJob, receipt['job_id']))
        case = _public(row)
        proposal = preview(case)
        if (proposal['expected_revision'] != expected_revision or proposal['expected_job_id'] != expected_job_id
                or proposal['state_digest'] != state_digest or proposal['candidate_digest'] != candidate_digest):
            raise ValueError('case_revision_conflict')
        if proposal['status'] != 'candidate':
            raise ValueError('migration_candidate_not_applicable')
        parent = db.get(CaseCompileJob, expected_job_id)
        revision = db.get(CaseRevision, case.get('source_revision_id'))
        if parent is None or parent.status != 'succeeded' or not parent.output or revision is None:
            raise ValueError('migration_requires_completed_compilation')
        if (parent.project_id != project_id or parent.case_id != case_id or parent.revision_id != revision.revision_id
                or digest(migration_candidate((parent.output.get('meta') or {}).get('step_program_keys') or {})['candidate']) != candidate_digest):
            raise ValueError('migration_source_changed')
        job = _queue(db, row, revision, request_id='event-migration:' + request_id, parent_job_id=parent.job_id)
        receipts[request_id] = {'actor': actor, 'fingerprint': fingerprint, 'job_id': job.job_id,
            'parent_job_id': parent.job_id, 'parent_output_digest': digest(parent.output),
            'candidate': proposal['candidate'], 'candidate_digest': candidate_digest,
            'contract': VERSION, 'reviewed_at': now(), 'reason': reason.strip()}
        extra = deepcopy(row.extra);extra['event_migrations'] = receipts;row.extra = extra
        return job_view(job)


def job_input(db, job, case_row):
    receipts = (case_row.extra or {}).get('event_migrations') or {}
    return deepcopy(next((r for r in receipts.values() if r['job_id'] == job.job_id), None))


def compile_reviewed(job):
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.case_compile_job import CaseCompileJob
    from mino_nexus.services.execution_requirements import compile_steps, case_source_revision
    from mino_nexus.services.case_step_key_compiler import _block_dependencies, _COMPILE_CONTRACT_REVISION
    from mino_nexus.services.control_registry import stamp_compiled_plans
    receipt = job['event_migration']
    if receipt['contract'] != VERSION or digest(receipt['candidate']) != receipt['candidate_digest']:
        raise ValueError('migration_contract_changed')
    with session_scope() as db:
        parent = db.get(CaseCompileJob, receipt['parent_job_id'])
        if (parent is None or parent.project_id != job['project_id'] or parent.case_id != job['case_id']
                or parent.revision_id != job['revision_id'] or digest(parent.output) != receipt['parent_output_digest']):
            raise ValueError('migration_source_changed')
        output = deepcopy(parent.output)
    case = job['input']; app_id = str(case.get('app_id') or case['project_id'])
    plans = deepcopy(receipt['candidate'])
    if references(plans):
        raise ValueError('business_event_migration_required')
    for bundle in plans.values():
        required = set()
        for phase in ('do', 'check'):
            plan = bundle.get(phase + '_program_plan')
            if not isinstance(plan, dict):
                raise ValueError('migration_plan_incomplete')
            plan['binding_steps'] = compile_steps(plan, app_id=app_id)
            plan['binding_source_revision'] = case_source_revision(case)
            plan['binding_event_contract'] = VERSION
            plan['binding_requirement_contract'] = 1
            plan['block_dependencies'] = _block_dependencies(plan, app_id)
            plan['compile_contract_revision'] = _COMPILE_CONTRACT_REVISION
            plan['migration_provenance'] = {'parent_job_id': receipt['parent_job_id'], 'reviewed_at': receipt['reviewed_at']}
            required.update(c for r in plan['binding_steps'] for c in r['event_ids'])
        if stamp_compiled_plans(case, bundle['do_program_plan'], bundle['check_program_plan']):
            raise ValueError('migration_control_review_required')
        bundle['required_capabilities'] = sorted(required)
    output['meta']['step_program_keys'] = plans
    output['meta'].pop('v5_draft_step_program_keys', None)
    output['provenance'] = {'kind': 'reviewed_event_migration', 'parent_job_id': receipt['parent_job_id'],
                            'candidate_digest': receipt['candidate_digest'], 'reviewer': receipt['actor']}
    return output
