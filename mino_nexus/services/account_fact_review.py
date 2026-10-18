"""Account observations are proposals. Human confirmation uses an atomic CAS."""
from copy import deepcopy
import uuid
import time
from sqlalchemy import text
from mino_nexus.core.database import session_scope
from mino_nexus.core.execution_user import require_user_id
from mino_nexus.models.account_fact_candidate import AccountFactCandidate
from mino_nexus.models.pool_account import PoolAccount
from mino_nexus.models.session_event import SessionMeta, SessionEvent
from mino_nexus.services.case_compilation import owned, digest, now
from mino_nexus.services.pool_account_store import _facets_for_account, _write_facets


def view(row):
    return {key: deepcopy(getattr(row, key)) for key in ('candidate_id', 'project_id', 'account_id', 'session_id',
        'run_id', 'case_id', 'env', 'source', 'evidence_context', 'before', 'proposed', 'status', 'created_at', 'decision')}


def capture(ctx, proposed, *, source, expected_before):
    lease = ctx.resource_lease
    pid, aid = lease['project_id'], lease['account_id']
    sid = str(getattr(ctx, 'scout_run_id', '') or '')
    actor = require_user_id()
    with session_scope() as db:
        db.execute(text('BEGIN IMMEDIATE'))
        account = db.get(PoolAccount, aid)
        if account is None or account.project_id != pid:
            raise ValueError('account_missing')
        held = account.lease or {}
        if any(str(held.get(k) or '') != str(getattr(ctx, k, '') or '') for k in ('run_id', 'case_id', 'sn')) or not held.get('run_id'):
            raise ValueError('account_lease_changed')
        from mino_nexus.services.run_write_safety import protected
        if protected(str(held['run_id']), db=db):
            raise ValueError('resource_isolated')
        session = db.get(SessionMeta, sid)
        if session is None or session.owner_user_id != actor or session.run_id != ctx.run_id or session.case_id != ctx.case_id or not session.finished_at:
            raise ValueError('finished_execution_evidence_required')
        from mino_nexus.models.project import App
        app = db.get(App, session.app_id)
        if app is None or app.project_id != pid or (getattr(ctx, 'app_id', '') and ctx.app_id != session.app_id):
            raise ValueError('fact_application_scope_mismatch')
        if getattr(ctx, 'env_profile', '') and ctx.env_profile != account.env:
            raise ValueError('fact_environment_scope_mismatch')
        existing = db.query(AccountFactCandidate).filter_by(owner_user_id=actor, session_id=sid, account_id=aid).one_or_none()
        if existing:
            return view(existing)
        before = _facets_for_account(db, pid, aid)
        from mino_nexus.services.resource_pool import account_facet_values_for_match
        from mino_nexus.services.pool_account_store import _account_to_dict
        if account_facet_values_for_match(_account_to_dict(account, before)) != expected_before:
            raise ValueError('account_facts_changed')
        changes = {k: v for k, v in proposed.items() if before.get(k, 'unknown') != v}
        if not changes:
            return None
        row = AccountFactCandidate(candidate_id='fact-' + uuid.uuid4().hex, owner_user_id=actor,
            project_id=pid, account_id=aid, session_id=sid, run_id=ctx.run_id, case_id=ctx.case_id,
            env=account.env, source=source, evidence_context={'app_id': session.app_id,
                'platform': str(getattr(ctx, 'platform', '') or ''),
                'build': str(getattr(ctx, 'app_version', '') or ''), 'sn': str(getattr(ctx, 'sn', '') or ''),
                'session_finalization_id': session.finalization_id or '', 'lease_id': held.get('lease_id', '')}, before=before, proposed=changes, status='candidate', created_at=now(), decision={})
        db.add(row)
        return view(row)


def listing(project_id, *, limit=50, offset=0, status=""):
    with session_scope() as db:
        actor = owned(db, project_id, write=False)
        query = db.query(AccountFactCandidate).filter_by(project_id=project_id, owner_user_id=actor)
        if status:
            query = query.filter_by(status=status)
        total = query.count()
        rows = query.order_by(AccountFactCandidate.created_at.desc(), AccountFactCandidate.candidate_id).offset(offset).limit(limit).all()
        items = []
        for row in rows:
            account = db.get(PoolAccount, row.account_id)
            current = _facets_for_account(db, project_id, row.account_id)
            items.append({**view(row), 'expired': row.status == 'confirmed' and (row.decision or {}).get('expires_at', 0) <= time.time(),
                          'scope_complete': all((row.evidence_context or {}).get(k) for k in ('app_id', 'platform', 'build')),
                          'stale': account is None or account.env != row.env or current != row.before,
                          'busy': bool(account and account.lease), 'expected_digest': digest(current)})
        return {'owner_user_id': actor, 'project_id': project_id, 'items': items, 'total': total, 'limit': limit, 'offset': offset}


def decide(project_id, candidate_id, body):
    from mino_nexus.services.control_registry import _assert_runtime_writable
    _assert_runtime_writable()
    with session_scope() as db:
        db.execute(text('BEGIN IMMEDIATE'))
        actor = owned(db, project_id, write=True)
        row = db.get(AccountFactCandidate, candidate_id)
        if row is None or row.project_id != project_id or row.owner_user_id != actor:
            raise ValueError('fact_candidate_missing')
        target = 'confirmed' if body.approve else 'rejected'
        if row.status == target and row.decision.get('reason') == body.reason:
            return view(row)
        if row.status != 'candidate':
            raise ValueError('fact_decision_conflict')
        if body.approve:
            account = db.get(PoolAccount, row.account_id)
            if account is None or account.project_id != project_id or account.env != row.env:
                raise ValueError('account_scope_changed')
            if account.lease:
                raise ValueError('account_busy')
            current = _facets_for_account(db, project_id, row.account_id)
            if current != row.before or digest(current) != body.expected_digest:
                raise ValueError('account_facts_changed')
            session = db.get(SessionMeta, row.session_id)
            if session is None or session.owner_user_id != actor or not session.finished_at or session.app_id != row.evidence_context.get('app_id'):
                raise ValueError('fact_evidence_missing')
            event = db.query(SessionEvent).filter_by(session_id=row.session_id, seq=body.evidence_seq).one_or_none() if body.evidence_seq is not None else None
            if event is None or event.type not in {'observe/screen', 'observe/hierarchy', 'tool/result', 'inspection/done'}:
                raise ValueError('fact_evidence_event_required')
            if not all((row.evidence_context or {}).get(k) for k in ('app_id', 'platform', 'build')):
                raise ValueError('fact_scope_incomplete')
            if session.finalization_id != row.evidence_context.get('session_finalization_id'):
                raise ValueError('fact_evidence_finalization_changed')
        row.status = target
        row.decision = {'reviewer_user_id': actor, 'reason': body.reason, 'at': now(),
                        'evidence_seq': body.evidence_seq, 'verification_kind': 'human_confirmed',
                        'expires_at': time.time() + body.valid_for_seconds if body.approve else 0}
        return view(row)
