"""Explicit scoped business rules; unconfigured or incomplete evidence stays a candidate."""
from copy import deepcopy
from datetime import datetime
import time
import uuid
from sqlalchemy import text
from mino_nexus.core.database import session_scope
from mino_nexus.models.account_fact_candidate import AccountFactCandidate, AccountFactRule
from mino_nexus.models.pool_account import PoolAccount
from mino_nexus.models.session_event import SessionMeta
from mino_nexus.core.stable_digest import digest, now
from mino_nexus.platform.project_guard import owned


def options(project_id):
    from mino_nexus.models.project import App, Project
    from mino_nexus.models.case import ProjectCase
    from mino_nexus.resources.accounts.pool_templates import merged_pool_field_defs
    from mino_nexus.resources.resource_pool import FACET_LABELS
    with session_scope() as db:
        owned(db, project_id, write=False)
        apps = db.query(App).filter_by(project_id=project_id).all()
        cases = db.query(ProjectCase).filter_by(project_id=project_id).all()
        choices = []
        for case in cases:
            extra = case.extra or {}
            publication = extra.get('v5_publication') or {}
            if extra.get('v5_plan_state') != 'published':
                continue
            points = [p for p in (publication.get('check_program_plan') or {}).get('checkpoints', []) if p.get('required') is True]
            if points:
                choices.append({'case_id': case.case_id, 'name': case.name, 'points': [
                    {'point_id': p['point_id'], 'label': str(p.get('control_id') or p['point_id']) + ' · ' + str((p.get('expected') or {}).get('value', (p.get('expected') or {}).get('predicate', ''))), 'predicate': (p.get('expected') or {}).get('predicate'), 'value': (p.get('expected') or {}).get('value')} for p in points]})
        project = db.get(Project, project_id)
        fields = {key: {'key': key, 'label': key, 'options': [{'value': v, 'label': label} for v, label in values.items()]}
                  for key, values in FACET_LABELS.items()}
        for field in merged_pool_field_defs(project.env or {}):
            fields[field['key']] = field
        return {'apps': [{'id': a.id, 'name': a.name} for a in apps], 'cases': choices,
                'fields': list(fields.values())}


def rules(project_id):
    with session_scope() as db:
        owned(db, project_id, write=False)
        return {'items': [{'rule_id': r.rule_id, **deepcopy(r.definition)} for r in db.query(
            AccountFactRule).filter_by(project_id=project_id).order_by(AccountFactRule.updated_at.desc()).all()]}


def save_rule(project_id, rule_id, body):
    from mino_nexus.models.project import App
    from mino_nexus.models.case import ProjectCase
    from mino_nexus.platform.runtime_mode import assert_writable as _assert_runtime_writable
    _assert_runtime_writable()
    choices = options(project_id)
    field = next((f for f in choices['fields'] if f['key'] == body.facet_key), None)
    if field is None or body.facet_value not in {o['value'] for o in field.get('options') or []}:
        raise ValueError('fact_rule_value_invalid')
    with session_scope() as db:
        db.execute(text('BEGIN IMMEDIATE'))
        actor = owned(db, project_id, write=True)
        app = db.get(App, body.app_id)
        case = db.query(ProjectCase).filter_by(project_id=project_id, case_id=body.case_id).one_or_none()
        if app is None or app.project_id != project_id or case is None:
            raise ValueError('fact_rule_scope_invalid')
        extra = case.extra or {}
        if extra.get('app_id') and extra['app_id'] != app.id:
            raise ValueError('fact_rule_scope_invalid')
        publication = extra.get('v5_publication') or {}
        points = [p for p in (publication.get('check_program_plan') or {}).get('checkpoints', []) if p.get('point_id') == body.point_id and p.get('required') is True]
        build_points = [p for p in (publication.get('check_program_plan') or {}).get('checkpoints', [])
            if p.get('point_id') == body.build_point_id and p.get('required') is True
            and (p.get('expected') or {}).get('predicate') in {'text_equals', 'value_equals'}
            and (p.get('expected') or {}).get('value') == body.build]
        if extra.get('v5_plan_state') != 'published' or len(points) != 1 or len(build_points) != 1:
            raise ValueError('fact_rule_published_checkpoint_required')
        row = db.get(AccountFactRule, rule_id)
        if row and row.project_id != project_id:
            raise ValueError('fact_rule_missing')
        if (row.definition.get('revision', 0) if row else 0) != body.expected_revision:
            raise ValueError('fact_rule_revision_conflict')
        if row is None:
            row = AccountFactRule(rule_id='fact-rule-' + uuid.uuid4().hex, project_id=project_id)
            db.add(row)
        row.app_id = app.id
        row.definition = {**body.model_dump(exclude={'expected_revision'}), 'revision': body.expected_revision + 1,
            'publication_digest': digest(publication)}
        row.updated_by, row.updated_at = actor, now()
        return {'rule_id': row.rule_id, **deepcopy(row.definition)}


def verify_in_transaction(db, row, *, held=False):
    from mino_nexus.resources.pool_account_store import _facets_for_account
    from mino_nexus.records.write_state import protected
    from mino_nexus.records.verification import checkpoint
    if row.status != 'candidate':
        return False
    context = row.evidence_context or {}
    account = db.get(PoolAccount, row.account_id)
    session = db.get(SessionMeta, row.session_id)
    if account is None or session is None or account.env != row.env or session.owner_user_id != row.owner_user_id:
        raise ValueError('fact_evidence_scope_changed')
    if session.app_id != context.get('app_id') or session.run_id != row.run_id or session.case_id != row.case_id:
        raise ValueError('fact_evidence_identity_changed')
    if session.finalization_id != context.get('session_finalization_id') or not all(context.get(k) for k in ('app_id', 'platform', 'sn')):
        raise ValueError('fact_scope_incomplete')
    lease = account.lease or {}
    if not held or str(lease.get('run_id') or '') not in {row.run_id, row.session_id} or lease.get('sn') != context['sn']:
        raise ValueError('fact_active_original_lease_required')
    if context.get('lease_digest') and digest(lease) != context['lease_digest']:
        raise ValueError('fact_lease_changed')
    if protected(row.run_id, db=db, sn=context['sn']):
        raise ValueError('resource_isolated')
    if _facets_for_account(db, row.project_id, row.account_id, apply_observations=False) != row.before:
        raise ValueError('account_facts_changed')
    matching = db.query(AccountFactRule).filter_by(project_id=row.project_id, app_id=session.app_id).all()
    evidence, expiry = [], float('inf')
    for key, value in row.proposed.items():
        candidates = [r for r in matching if r.definition.get('enabled') is True
            and r.definition.get('env') == row.env and r.definition.get('case_id') == row.case_id
            and r.definition.get('publication_digest') == digest((session.runtime_context or {}).get('publication') or {})
            and r.definition.get('platform') == context.get('platform')
            and (not context.get('build') or r.definition.get('build') == context.get('build'))
            and r.definition.get('facet_key') == key and r.definition.get('facet_value') == value]
        if len(candidates) != 1:
            raise ValueError('fact_automatic_rule_missing_or_ambiguous')
        rule = candidates[0]
        build_ref = checkpoint(db, session, rule.definition['build_point_id'], rule.definition['publication_digest'])
        context = {**context, 'build': rule.definition['build'], 'build_evidence': build_ref}
        ref = checkpoint(db, session, rule.definition['point_id'], rule.definition['publication_digest'])
        expires = datetime.fromisoformat(ref['observed_at']).astimezone().timestamp() + rule.definition['valid_for_seconds']
        if expires <= time.time():
            raise ValueError('fact_observation_expired')
        build_expires = datetime.fromisoformat(build_ref['observed_at']).astimezone().timestamp() + rule.definition['valid_for_seconds']
        if build_expires <= time.time():
            raise ValueError('fact_build_observation_expired')
        expiry = min(expiry, expires, build_expires)
        evidence.append({**ref, 'facet_key': key, 'rule_id': rule.rule_id, 'rule_revision': rule.definition['revision']})
    if not evidence:
        return False
    row.evidence_context = context
    row.status = 'confirmed'
    row.decision = {'verification_kind': 'automatic', 'at': now(), 'expires_at': expiry,
        'reason': '已按批准的业务规则核对原始观察', 'evidence': evidence}
    return True


def verify_candidate(project_id, candidate_id):
    from mino_nexus.platform.runtime_mode import assert_writable as _assert_runtime_writable
    _assert_runtime_writable()
    with session_scope() as db:
        db.execute(text('BEGIN IMMEDIATE'))
        actor = owned(db, project_id, write=True)
        row = db.get(AccountFactCandidate, candidate_id)
        if row is None or row.project_id != project_id or row.owner_user_id != actor:
            raise ValueError('fact_candidate_missing')
        verify_in_transaction(db, row, held=True)
        from mino_nexus.resources.account_fact_review import view
        return view(row)


def capture_strict_session(session_id):
    """Called after finalization while the exact input-account lease is still held."""
    from mino_nexus.platform.identity import require_user_id
    from mino_nexus.resources.pool_account_store import _facets_for_account
    from mino_nexus.resources.account_fact_review import view
    actor = require_user_id()
    with session_scope() as db:
        db.execute(text('BEGIN IMMEDIATE'))
        session = db.get(SessionMeta, session_id)
        if session is None or session.owner_user_id != actor or not session.finalization_id:
            return []
        context = session.runtime_context or {}
        project_id = str(context.get('project_id') or '')
        configured = db.query(AccountFactRule).filter_by(project_id=project_id, app_id=session.app_id).all()
        rules_for_session = [r for r in configured if r.definition.get('enabled') is True
            and r.definition.get('case_id') == session.case_id and r.definition.get('platform') == 'web'
            and r.definition.get('env') == context.get('env_profile')
            and r.definition.get('publication_digest') == digest(context.get('publication') or {})]
        if not rules_for_session:
            return []
        result = []
        for account_id, binding in (context.get('account_bindings') or {}).items():
            if db.query(AccountFactCandidate).filter_by(owner_user_id=actor, session_id=session_id, account_id=account_id).first():
                continue
            account = db.get(PoolAccount, account_id)
            if (account is None or account.project_id != project_id or account.env != context.get('env_profile')
                    or digest(account.lease or {}) != binding.get('lease_digest')
                    or (account.lease or {}).get('run_id') != session_id
                    or _facets_for_account(db, project_id, account_id, apply_observations=False) != binding.get('before')):
                continue
            proposed = {r.definition['facet_key']: r.definition['facet_value'] for r in rules_for_session}
            row = AccountFactCandidate(candidate_id='fact-' + uuid.uuid4().hex, owner_user_id=actor,
                project_id=project_id, account_id=account_id, session_id=session_id, run_id=session.run_id,
                case_id=session.case_id, env=account.env, source='deterministic_checkpoint',
                before=deepcopy(binding['before']), proposed=proposed,
                evidence_context={'app_id': session.app_id, 'platform': 'web', 'build': '',
                    'sn': context.get('sn'), 'session_finalization_id': session.finalization_id,
                    'lease_digest': binding['lease_digest']}, status='candidate', created_at=now(), decision={})
            db.add(row)
            db.flush()
            try:
                verify_in_transaction(db, row, held=True)
            except ValueError as exc:
                row.decision = {'automatic_check': str(exc)}
            result.append(view(row))
        return result
