"""Human-reviewed additions to existing NavFSM states; never a second page catalog."""
from copy import deepcopy
import uuid

from sqlalchemy import text
from mino_nexus.core.database import session_scope
from mino_nexus.platform.identity import require_user_id
from mino_nexus.models.nav_fsm import NavFsm, NavFsmState
from mino_nexus.models.project import App
from mino_nexus.models.session_event import SessionMeta, SessionEvent
from mino_nexus.core.stable_digest import digest, now
from mino_nexus.platform.project_guard import owned

REVIEW_KEYS = ('knowledge_revision', 'knowledge_reviews')


def scope(db, project_id, app_id, *, write=False):
    actor = owned(db, project_id, write=write)
    app = db.get(App, app_id)
    if app is None or app.project_id != project_id:
        raise ValueError('application_scope_mismatch')
    return actor


def base_digest(state):
    meta = state.get('meta') or {}
    return digest({'identify': state.get('identify') or {}, 'kind': state.get('kind'),
                   'display_name': meta.get('display_name'), 'aliases': meta.get('aliases')})


def state_view(state):
    return {'id': state.state_id, 'kind': state.kind, 'identify': deepcopy(state.identify), 'meta': deepcopy(state.meta or {})}


def _state(db, app_id, version, state_id):
    fsm = db.query(NavFsm).filter_by(app_id=app_id, version=version).one_or_none()
    row = db.query(NavFsmState).filter_by(fsm_id=fsm.id, state_id=state_id).one_or_none() if fsm else None
    if row is None:
        raise ValueError('navigation_page_missing')
    return row


def pages(project_id, app_id):
    with session_scope() as db:
        actor = scope(db, project_id, app_id)
        result = []
        for fsm in db.query(NavFsm).filter_by(app_id=app_id, project_id=project_id).order_by(NavFsm.version):
            for row in db.query(NavFsmState).filter_by(fsm_id=fsm.id).order_by(NavFsmState.state_id):
                meta = row.meta or {}
                # Candidates and private session references are only visible to their author.
                records = [deepcopy(r) for r in meta.get('knowledge_reviews', []) if r.get('owner_user_id') == actor]
                for r in records:
                    r['stale'] = r.get('base_digest') != base_digest(state_view(row))
                result.append({'state_id': row.state_id, 'version': fsm.version,
                    'label': meta.get('display_name') or row.state_id,
                    'revision': meta.get('knowledge_revision', 0), 'reviews': records})
        return {'owner_user_id': actor, 'app_id': app_id, 'project_id': project_id, 'pages': result}


def _evidence(db, app_id, examples, actor):
    if len({(e['session_id'], e['seq']) for e in examples}) != len(examples):
        raise ValueError('distinct_evidence_events_required')
    for example in examples:
        session = db.get(SessionMeta, example['session_id'])
        if session is None or session.owner_user_id != actor or session.app_id != app_id:
            raise ValueError('evidence_scope_mismatch')
        event = db.query(SessionEvent).filter_by(session_id=session.session_id, seq=example['seq']).one_or_none()
        if event is None or event.type not in {'observe/screen', 'observe/hierarchy', 'tool/result', 'inspection/done'}:
            raise ValueError('observation_evidence_required')
        if not session.finished_at:
            raise ValueError('evidence_session_not_finished')


def propose(project_id, app_id, state_id, body):
    from mino_nexus.knowledge.control_registry import _assert_runtime_writable
    _assert_runtime_writable()
    data = body.model_dump()
    request_id = data.pop('request_id')
    expected = data.pop('expected_revision')
    version = data.pop('version')
    if set(data['positive_terms']) & set(data['negative_terms']):
        raise ValueError('contradictory_page_features')
    if not data['reason'].strip() or not data['build'].strip():
        raise ValueError('review_scope_and_reason_required')
    with session_scope() as db:
        db.execute(text('BEGIN IMMEDIATE'))
        actor = scope(db, project_id, app_id, write=True)
        row = _state(db, app_id, version, state_id)
        meta = deepcopy(row.meta or {})
        records = meta.setdefault('knowledge_reviews', [])
        existing = next((r for r in records if r['request_id'] == request_id and r['owner_user_id'] == actor), None)
        if existing:
            if existing['request_digest'] != digest(data):
                raise ValueError('review_request_conflict')
            return deepcopy(existing)
        if meta.get('knowledge_revision', 0) != expected:
            raise ValueError('page_revision_conflict')
        _evidence(db, app_id, data['examples'], actor)
        revision = expected + 1
        record = {**data, 'review_id': 'page-review-' + uuid.uuid4().hex,
            'request_id': request_id, 'request_digest': digest(data), 'owner_user_id': actor,
            'revision': revision, 'base_digest': base_digest(state_view(row)),
            'status': 'candidate', 'created_at': now(), 'decisions': []}
        records.append(record)
        meta['knowledge_revision'] = revision
        row.meta = meta
        return deepcopy(record)


def decide(project_id, app_id, state_id, review_id, body):
    from mino_nexus.knowledge.control_registry import _assert_runtime_writable
    _assert_runtime_writable()
    if not body.reason.strip():
        raise ValueError('review_reason_required')
    with session_scope() as db:
        db.execute(text('BEGIN IMMEDIATE'))
        actor = scope(db, project_id, app_id, write=True)
        row = _state(db, app_id, body.version, state_id)
        meta = deepcopy(row.meta or {})
        record = next((r for r in meta.get('knowledge_reviews', []) if r['review_id'] == review_id and r['owner_user_id'] == actor), None)
        if not record:
            raise ValueError('review_missing')
        target = 'approved' if body.approve else 'rejected'
        if record['status'] == target and record['decisions'][-1]['reason'] == body.reason:
            return deepcopy(record)
        if meta.get('knowledge_revision', 0) != body.expected_revision or record['status'] != 'candidate':
            raise ValueError('page_revision_conflict')
        if body.approve:
            if record['base_digest'] != base_digest(state_view(row)):
                raise ValueError('page_changed_reproposal_required')
            if {e['polarity'] for e in record['examples']} != {'positive', 'negative'}:
                raise ValueError('positive_and_negative_evidence_required')
            if not record['positive_terms'] or not record['negative_terms']:
                raise ValueError('positive_and_negative_features_required')
            _evidence(db, app_id, record['examples'], actor)
        meta['knowledge_revision'] += 1
        record['status'] = target
        record['decisions'].append({'status': target, 'reason': body.reason, 'reviewer_user_id': actor,
                                    'at': now(), 'revision': meta['knowledge_revision']})
        row.meta = meta
        return deepcopy(record)


def apply_reviewed(fsm, *, platform, build):
    """Unknown versions get no overlay; never mutate a published graph or prior run."""
    result = deepcopy(fsm)
    if not result or not platform or not build:
        return result
    actor = require_user_id()
    with session_scope() as db:
        graph = db.query(NavFsm).filter_by(app_id=result.get('app_id'), version=result.get('version') or 'v1').one_or_none()
        trusted = {r.state_id: deepcopy(r.meta or {}) for r in db.query(NavFsmState).filter_by(fsm_id=graph.id)} if graph else {}
    for state in result.get('states') or []:
        meta = state.setdefault('meta', {})
        accepted = [r for r in trusted.get(state.get('id'), {}).get('knowledge_reviews', [])
                    if r.get('owner_user_id') == actor and r.get('status') == 'approved' and r.get('platform') == platform
                    and r.get('build') == build and r.get('base_digest') == base_digest(state)]
        if accepted:
            record = max(accepted, key=lambda r: r['decisions'][-1]['revision'])
            meta['aliases'] = list(dict.fromkeys([*(meta.get('aliases') or []), *record['aliases']]))
            state['reviewed_features'] = {'positive': record['positive_terms'], 'negative': record['negative_terms'],
                                          'review_id': record['review_id']}
    return result


def impact(project_id, app_id, state_id, *, version='v1', review_id=''):
    from mino_nexus.casework.case_store import list_cases
    with session_scope() as db:
        actor = scope(db, project_id, app_id)
        row = _state(db, app_id, version, state_id)
        meta = deepcopy(row.meta or {})
        record = next((r for r in meta.get('knowledge_reviews', []) if r['review_id'] == review_id and r['owner_user_id'] == actor), None)
        if review_id and record is None:
            raise ValueError('review_missing')
        aliases = [meta.get('display_name') or '', *(meta.get('aliases') or []), *((record or {}).get('aliases') or [])]
        reviewed_aliases = [r for r in meta.get('knowledge_reviews', []) if r.get('owner_user_id') == actor
                            and r.get('status') == 'approved' and r.get('base_digest') == base_digest(state_view(row))]
        revision = meta.get('knowledge_revision', 0)
    def references(value):
        if isinstance(value, dict):
            return any(references(v) for v in value.values())
        if isinstance(value, list):
            return any(references(v) for v in value)
        return value == state_id
    rows = []
    for case in list_cases(project_id):
        if case.get('app_id') and case['app_id'] != app_id:
            continue
        if record and case.get('platform') != record['platform']:
            continue
        structural = references({k: case.get(k) for k in ('meta', 'resource_key', 'case_scene', 'v5_publication')})
        source = '\n'.join(str(case.get(k) or '') for k in ('precondition', 'steps_raw', 'expected_raw', 'steps', 'expected'))
        applicable_aliases = list(dict.fromkeys([*aliases, *(a for r in reviewed_aliases
            if r.get('platform') == case.get('platform') for a in r.get('aliases', []))]))
        matched = [a for a in applicable_aliases if a and a in source]
        if structural or matched:
            rows.append({'case_id': case['case_id'], 'name': case.get('name') or case['case_id'],
                'platform': case.get('platform'), 'basis': 'explicit_reference' if structural else 'text_candidate',
                'matched_aliases': matched, 'expected_revision': int(case.get('v5_draft_revision') or 1),
                'expected_job_id': case.get('latest_compile_job_id') or '',
                'revision_id': case.get('source_revision_id') or ''})
    value = {'app_id': app_id, 'project_id': project_id, 'state_id': state_id, 'version': version,
             'review_id': review_id, 'page_revision': revision, 'cases': rows}
    return {**value, 'impact_token': digest({**value, 'cases': [{k: v for k, v in c.items() if k != 'expected_job_id'} for c in rows]}), 'owner_user_id': actor,
            'warning': '文字命中仅为候选；无引用不等于不受影响。目标版本实跑仍需单独核验。'}


def revalidate(project_id, app_id, state_id, body):
    from mino_nexus.casework.case_compilation import enqueue
    from mino_nexus.casework.case_review import recheck
    with session_scope() as db:
        scope(db, project_id, app_id, write=True)
    preview = impact(project_id, app_id, state_id, version=body.version, review_id=body.review_id)
    if preview['impact_token'] != body.impact_token:
        raise ValueError('impact_changed_refresh_required')
    selected = set(body.case_ids)
    allowed = {r['case_id']: r for r in preview['cases']}
    if not selected or not selected <= allowed.keys():
        raise ValueError('impact_selection_invalid')
    result = []
    for cid in sorted(selected):
        case = allowed[cid]
        try:
            if body.action == 'compile':
                receipt = enqueue(project_id, cid, case['expected_revision'],
                                  'knowledge:' + body.request_id + ':' + cid)
                result.append({'case_id': cid, 'status': receipt['status'], 'job_id': receipt['job_id']})
            else:
                receipt = recheck(project_id, cid, case['expected_revision'], case['expected_job_id'],
                                  body.reason, context={}, evidence_session_id='')
                result.append({'case_id': cid, 'status': receipt['resolution_status']})
        except ValueError as exc:
            result.append({'case_id': cid, 'status': 'failed', 'reason': str(exc)})
    return {'request_id': body.request_id, 'items': result, 'business_verified': False}


def trends(project_id, app_id, *, platform=''):
    from mino_nexus.casework.case_store import list_cases
    from mino_nexus.casework.case_review import describe
    from mino_nexus.models.case_review_record import CaseReviewRecord
    cases = [c for c in list_cases(project_id) if (not c.get('app_id') or c['app_id'] == app_id)
             and (not platform or c.get('platform') == platform)]
    ids = {c['case_id'] for c in cases}
    with session_scope() as db:
        actor = scope(db, project_id, app_id)
        records = db.query(CaseReviewRecord).filter_by(project_id=project_id, owner_user_id=actor).order_by(
            CaseReviewRecord.reviewed_at, CaseReviewRecord.record_id).all()
        history = [{'case_id': r.case_id, 'status': r.review_status, 'at': r.reviewed_at,
                    'evidence': deepcopy(r.evidence), 'context': deepcopy(r.context)}
                   for r in records if r.case_id in ids and r.action != 'recheck_snapshot'
                   and (not r.context or r.context.get('app_id') == app_id)]
    daily, resolved, reopened = {}, set(), set()
    def identity(cid, item):
        return digest([cid, item.get('origin'), item.get('reason_code'), item.get('phase'), item.get('control_id'), item.get('source_clause'), item.get('case_step'), item.get('context')])
    last = {}
    for row in history:
        evidence = row['evidence'] or {}
        item = evidence.get('diagnostic') or {}
        if not item:
            continue
        key = identity(row['case_id'], item)
        day = daily.setdefault(row['at'][:10], {'reviewed': set(), 'resolved': set(), 'reopened': set()})
        day['reviewed'].add(row['case_id'])
        verified = row['status'] == 'verified_resolved' and evidence.get('execution_verified') is True
        if verified:
            resolved.add(row['case_id']); day['resolved'].add(row['case_id'])
        elif last.get(key) == 'verified_resolved' and row['status'] not in {'fixed_pending_run', 'verified_resolved'}:
            reopened.add(row['case_id']); day['reopened'].add(row['case_id'])
        last[key] = 'verified_resolved' if verified else row['status']
    for case in cases:
        for item in describe(case)['requirements']:
            if last.get(identity(case['case_id'], item)) == 'verified_resolved':
                reopened.add(case['case_id'])
    return {'app_id': app_id, 'owner_user_id': actor, 'platform': platform, 'total_cases': len(ids),
            'reviewed_cases': len({r['case_id'] for r in history}), 'ever_verified_resolved_cases': len(resolved),
            'reopened_cases': len(reopened), 'improvement_rate': None,
            'days': [{'date': date, **{k+'_cases': len(v) for k, v in counts.items()}} for date, counts in sorted(daily.items())],
            'note': '按用例去重；历史解决与复发可重叠。没有对照基线，不计算改善率；运行失败不直接算用例问题。'}
