"""Paired runtime page localization and independent human labels; no model self-scoring."""
from copy import deepcopy
from sqlalchemy import text
from mino_nexus.core.database import session_scope
from mino_nexus.models.nav_fsm import PageRecognitionReview, NavFsm, NavFsmState
from mino_nexus.models.session_event import SessionMeta, SessionEvent
from mino_nexus.services.case_compilation import digest, now
from mino_nexus.services.application_understanding import scope

VERSION = 'nav-localize-paired-1'
EVENT = 'nav/recognition-comparison'


def compact(value):
    return {key: deepcopy(value.get(key)) for key in ('chosen', 'confidence', 'ambiguous', 'band', 'degraded')}


def record_pair(writer, *, states, reviewed, fsm_meta, project_id, app_id, platform, build,
                hierarchy_stale, **observation):
    from mino_nexus.services import nav_localize
    baseline_states = deepcopy(states)
    for state in baseline_states:
        state.pop('reviewed_features', None)
    baseline = nav_localize.localize(states=baseline_states, **observation)
    selected = next((state for state in baseline_states if state.get('id') == baseline.get('chosen')), {})
    baseline = nav_localize.apply_evidence_policy(baseline, state_meta=selected.get('meta') or {}, fsm_meta=fsm_meta)
    writer.append(EVENT, {'contract': VERSION, 'project_id': project_id, 'app_id': app_id,
        'platform': platform, 'build': build, 'graph_digest': digest(states),
        'state_ids': [state['id'] for state in states if state.get('id')],
        'review_ids': sorted({state['reviewed_features']['review_id'] for state in states if state.get('reviewed_features')}),
        'hierarchy_stale': bool(hierarchy_stale), 'hierarchy_weak': bool(observation.get('hierarchy_weak')),
        'baseline': compact(baseline), 'reviewed': compact(reviewed)})


def _training_sessions(db, app_id, actor, review_ids):
    fsms = db.query(NavFsm).filter_by(app_id=app_id).all()
    ids = {f.id for f in fsms}
    sessions = set()
    for state in db.query(NavFsmState).filter(NavFsmState.fsm_id.in_(ids)).all():
        for review in (state.meta or {}).get('knowledge_reviews', []):
            if review.get('owner_user_id') == actor and review.get('review_id') in review_ids:
                sessions.update(example['session_id'] for example in review.get('examples', []))
    return sessions


def _view(db, event, session, actor, training):
    payload = event.payload or {}
    sample_id = digest([actor, session.session_id, event.seq])
    review = db.get(PageRecognitionReview, sample_id)
    reason = ('knowledge_example' if session.session_id in training else
              'hierarchy_incomplete' if payload.get('hierarchy_stale') or payload.get('hierarchy_weak') else
              'scope_incomplete' if not payload.get('build') or not payload.get('platform') else '')
    matches = review is not None and review.evidence_digest == digest(payload)
    return {'sample_id': sample_id, 'session_id': session.session_id, 'case_id': session.case_id,
        'seq': event.seq, 'at': event.ts, 'platform': payload.get('platform'), 'build': payload.get('build'),
        'graph_digest': payload.get('graph_digest'), 'baseline': payload.get('baseline'), 'reviewed': payload.get('reviewed'),
        'expected_state_id': review.expected_state_id if matches else None,
        'revision': review.revision if matches else 0, 'eligible': not reason, 'excluded_reason': reason,
        'state_ids': payload.get('state_ids') or []}


def listing(project_id, app_id, *, platform='', build='', limit=50, offset=0):
    with session_scope() as db:
        actor = scope(db, project_id, app_id)
        query = db.query(SessionEvent, SessionMeta).join(SessionMeta, SessionEvent.session_id == SessionMeta.session_id).filter(
            SessionMeta.owner_user_id == actor, SessionMeta.app_id == app_id,
            SessionMeta.finished_at.isnot(None), SessionEvent.type == EVENT).order_by(SessionMeta.started_at.desc(), SessionEvent.seq.desc())
        # Scope filters are enforced in SQL before paging or statistics.
        if platform:
            query = query.filter(SessionEvent.payload['platform'].as_string() == platform)
        if build:
            query = query.filter(SessionEvent.payload['build'].as_string() == build)
        total = query.count()
        records = query.limit(2000).all()
        review_ids = {rid for event, _ in records for rid in (event.payload or {}).get('review_ids', [])}
        training = _training_sessions(db, app_id, actor, review_ids)
        items = [_view(db, event, session, actor, training) for event, session in records
                 if (event.payload or {}).get('contract') == VERSION and (event.payload or {}).get('project_id') == project_id]
        groups = {}
        for item in items:
            if not item['eligible'] or item['expected_state_id'] is None:
                continue
            key = (item['platform'], item['build'], item['graph_digest'])
            group = groups.setdefault(key, {'platform': key[0], 'build': key[1], 'graph_digest': key[2],
                'samples': 0, 'baseline_errors': 0, 'reviewed_errors': 0, 'case_ids': set()})
            group['samples'] += 1
            group['case_ids'].add(item['case_id'])
            for mode in ('baseline', 'reviewed'):
                prediction = item[mode] or {}
                # Ambiguous and abstaining recognizers cannot be counted as correct labels.
                if prediction.get('ambiguous') or str(prediction.get('chosen') or '') != item['expected_state_id']:
                    group[mode + '_errors'] += 1
        summaries = []
        for group in groups.values():
            summaries.append({**group, 'case_ids': sorted(group['case_ids']), 'cases': len(group['case_ids']),
                'baseline_error_rate': group['baseline_errors'] / group['samples'],
                'reviewed_error_rate': group['reviewed_errors'] / group['samples'],
                'error_rate_delta': (group['baseline_errors'] - group['reviewed_errors']) / group['samples']})
        return {'owner_user_id': actor, 'project_id': project_id, 'app_id': app_id,
            'items': items[offset:offset+limit], 'total': total, 'summaries': summaries,
            'statistics_window': min(total, 2000), 'truncated': total > 2000,
            'unlabeled': sum(i['expected_state_id'] is None for i in items),
            'excluded': sum(not i['eligible'] for i in items), 'recognizer_version': VERSION}


def label(project_id, app_id, session_id, seq, body):
    from mino_nexus.services.control_registry import _assert_runtime_writable
    _assert_runtime_writable()
    with session_scope() as db:
        db.execute(text('BEGIN IMMEDIATE'))
        actor = scope(db, project_id, app_id, write=True)
        session = db.get(SessionMeta, session_id)
        event = db.query(SessionEvent).filter_by(session_id=session_id, seq=seq, type=EVENT).one_or_none()
        if session is None or session.owner_user_id != actor or session.app_id != app_id or not session.finished_at or event is None:
            raise ValueError('comparison_sample_missing')
        payload = event.payload or {}
        if payload.get('contract') != VERSION or payload.get('project_id') != project_id:
            raise ValueError('comparison_contract_unsupported')
        if body.expected_state_id and body.expected_state_id not in payload.get('state_ids', []):
            raise ValueError('comparison_expected_page_missing')
        training = _training_sessions(db, app_id, actor, payload.get('review_ids') or [])
        item = _view(db, event, session, actor, training)
        if not item['eligible']:
            raise ValueError('comparison_sample_excluded:' + item['excluded_reason'])
        row = db.get(PageRecognitionReview, item['sample_id'])
        if row and row.history and row.history[-1].get('request_id') == body.request_id:
            last = row.history[-1]
            if last['expected_state_id'] != body.expected_state_id or last['reason'] != body.reason:
                raise ValueError('comparison_request_conflict')
            return item
        if item['revision'] != body.expected_revision:
            raise ValueError('comparison_revision_conflict')
        if row is None:
            row = PageRecognitionReview(sample_id=item['sample_id'], project_id=project_id, app_id=app_id,
                owner_user_id=actor, session_id=session_id, event_seq=seq, revision=0, history=[])
            db.add(row)
        row.revision += 1
        row.evidence_digest = digest(payload)
        row.expected_state_id = body.expected_state_id
        row.history = [*(row.history or []), {'request_id': body.request_id, 'reviewer_user_id': actor,
            'expected_state_id': body.expected_state_id, 'reason': body.reason, 'at': now(), 'revision': row.revision}]
        db.flush()
        return _view(db, event, session, actor, training)
