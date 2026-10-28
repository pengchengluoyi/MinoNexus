"""Human-reviewed additions to existing NavFSM states; never a second page catalog."""
from copy import deepcopy
import uuid

from sqlalchemy import func, text
from mino_nexus.core.database import session_scope
from mino_nexus.platform.identity import require_user_id
from mino_nexus.knowledge.page_semantics import analyze_clause, page_key
from mino_nexus.models.nav_case_page_ref import NavCasePageRef
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


def _publish_capture_pages(project_id, app_id):
    """导航图还没有页面时，用采集合成并写入同一份 nav_fsm。"""
    from mino_nexus.navigation.atlas import nav_fsm_store as store

    raw = store.read_raw(app_id)
    if raw and raw.get("states") and str(raw.get("project_id") or "") == str(project_id or ""):
        return
    from mino_nexus.navigation.atlas.nav_candidate_compiler import publish_pages_from_captures

    publish_pages_from_captures(app_id, project_id=project_id, updated_by="application_logic")


def _page_anchors(meta: dict) -> list[str]:
    """这一页采集到的稳定文案。页面身份仍是骨架指纹，这些文字只是该页上的内容。"""
    seen: list[str] = []
    for key in ("chrome_texts", "name_samples", "aliases"):
        for raw in meta.get(key) or []:
            for line in str(raw).splitlines():
                text = " ".join(str(line or "").split()).strip()
                if len(text) < 2 or text in seen or text in {"页面", "EditText"}:
                    continue
                seen.append(text)
                if len(seen) >= 24:
                    return seen
    return seen


def pages(project_id, app_id):
    _publish_capture_pages(project_id, app_id)
    with session_scope() as db:
        actor = scope(db, project_id, app_id)
        ref_counts = {
            state_id: int(count or 0)
            for state_id, count in db.query(NavCasePageRef.page_state_id, func.count(NavCasePageRef.id))
            .filter(NavCasePageRef.app_id == app_id, NavCasePageRef.project_id == project_id)
            .group_by(NavCasePageRef.page_state_id).all()
        }
        label_counts = {
            label: int(count or 0)
            for label, count in db.query(NavCasePageRef.page_label, func.count(NavCasePageRef.id))
            .filter(NavCasePageRef.app_id == app_id, NavCasePageRef.project_id == project_id)
            .group_by(NavCasePageRef.page_label).all()
        }
        result = []
        for fsm in db.query(NavFsm).filter_by(app_id=app_id, project_id=project_id).order_by(NavFsm.version):
            for row in db.query(NavFsmState).filter_by(fsm_id=fsm.id).order_by(NavFsmState.state_id):
                meta = row.meta or {}
                if str(meta.get('semantic_status') or '') == 'rejected':
                    continue
                sem = analyze_clause(str(meta.get('display_name') or ''))
                page_source = str(meta.get('page_source') or '')
                # 旧 case_flow 长句即使还没物理清理，也不再把动作句当页面展示。
                if page_source == 'case_flow' and (not sem.page_label or sem.role in ('assertion', 'flow')):
                    continue
                label = sem.page_label if page_source == 'case_flow' and sem.page_label else (meta.get('display_name') or row.state_id)
                key = str(meta.get('page_key') or '') or page_key(label)
                status = str(meta.get('semantic_status') or '')
                if not status:
                    status = 'provisional' if page_source == 'case_flow' else 'captured'
                # Candidates and private session references are only visible to their author.
                records = [deepcopy(r) for r in meta.get('knowledge_reviews', []) if r.get('owner_user_id') == actor]
                for r in records:
                    r['stale'] = r.get('base_digest') != base_digest(state_view(row))
                result.append({'state_id': row.state_id, 'version': fsm.version,
                    'label': label,
                    'page_key': key,
                    'variant': str(meta.get('variant') or sem.variant or ''),
                    'semantic_status': status,
                    'case_ref_count': label_counts.get(label, ref_counts.get(row.state_id, 0)),
                    'revision': meta.get('knowledge_revision', 0), 'reviews': records,
                    'visit_count': int(meta.get('visit_count') or 0),
                    'anchors': _page_anchors(meta)})
        return {'owner_user_id': actor, 'app_id': app_id, 'project_id': project_id, 'pages': result}


def case_refs(project_id, app_id, state_id):
    """某一页的用例引用：进入/操作/断言，按用例和步骤返回。"""
    with session_scope() as db:
        scope(db, project_id, app_id)
        state = db.query(NavFsmState).filter_by(state_id=str(state_id or '')).one_or_none()
        meta = (state.meta or {}) if state is not None else {}
        label = str(meta.get('display_name') or '')
        query = db.query(NavCasePageRef).filter_by(project_id=project_id, app_id=app_id)
        # 同页面的多个业务状态共用一个页面短名；引用按页面聚合，
        # 不强迫前端为一个页面状态重复请求。
        if label:
            query = query.filter(NavCasePageRef.page_label == label)
        else:
            query = query.filter(NavCasePageRef.page_state_id == str(state_id or ''))
        rows = (
            query
            .order_by(NavCasePageRef.case_id, NavCasePageRef.step_index, NavCasePageRef.relation)
            .limit(500)
            .all()
        )
        return {
            'project_id': project_id,
            'app_id': app_id,
            'state_id': state_id,
            'items': [
                {
                    'case_id': row.case_id,
                    'case_name': row.case_name,
                    'step_index': int(row.step_index or 0),
                    'step_id': row.step_id,
                    'page_state_id': row.page_state_id,
                    'page_label': row.page_label,
                    'control_id': row.control_id,
                    'relation': row.relation,
                    'quote': row.quote,
                    'variant': row.variant,
                    'data': deepcopy(row.data or {}),
                }
                for row in rows
            ],
        }


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
        from mino_nexus.core.ids import prefixed
        record = {**data, 'review_id': prefixed('pagereview'),
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
                result.append({'case_id': cid, 'status': 'failed', 'reason': 'review_removed'})
        except ValueError as exc:
            result.append({'case_id': cid, 'status': 'failed', 'reason': str(exc)})
    return {'request_id': body.request_id, 'items': result, 'business_verified': False}


def trends(project_id, app_id, *, platform=''):
    from mino_nexus.casework.case_store import list_cases
    cases = [c for c in list_cases(project_id) if (not c.get('app_id') or c['app_id'] == app_id)
             and (not platform or c.get('platform') == platform)]
    with session_scope() as db:
        actor = scope(db, project_id, app_id)
    return {'app_id': app_id, 'owner_user_id': actor, 'platform': platform, 'total_cases': len(cases),
            'reviewed_cases': 0, 'ever_verified_resolved_cases': 0, 'reopened_cases': 0,
            'improvement_rate': None, 'days': [], 'note': '用例评审已关闭。'}
