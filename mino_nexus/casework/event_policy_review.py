"""Admin-reviewed recovery/guard migration with CAS and immutable review receipts."""
from copy import deepcopy
import json
from sqlalchemy import text
from mino_nexus.core.stable_digest import digest, now
from mino_nexus.contract.business_event_contract import references, migration_candidate, tier_for, RETIRED


def _snapshot(row):
    return {'kind': row.kind, 'id': row.id, 'enabled': row.enabled, 'lifecycle': row.lifecycle,
            'platforms': row.platforms_json, 'payload': row.payload_json}


def inventory():
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.catalog import CatalogEntry
    result = []
    with session_scope() as db:
        for row in db.query(CatalogEntry).order_by(CatalogEntry.pk):
            raw = deepcopy(row.payload_json or {})
            issues = references(raw)
            if row.lifecycle == 'deprecated' and not row.enabled:
                continue
            if not issues and row.id not in RETIRED and not (row.id.startswith('guard.') and raw.get('tier') != 'policy'):
                continue
            proposal = migration_candidate(raw, context='recovery')
            result.append({'kind': row.kind, 'id': row.id, 'title': row.display_name,
                'source_digest': digest(_snapshot(row)), 'tier': tier_for(row.id, recovery_rule=row.kind=='recovery'),
                'blockers': proposal['blockers'], 'candidate': proposal['candidate'],
                'retired_event': row.id in RETIRED})
    return result


def apply(*, kind, entry_id, source_digest, request_id, reason, action, replacement, actor):
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.catalog import CatalogEntry, CatalogMeta
    from mino_nexus.models.auth import User
    from mino_nexus.platform.auth_store import is_admin_role
    from mino_nexus.knowledge.control_registry import _assert_runtime_writable
    from mino_nexus.catalog.models import RecoveryRule
    from mino_nexus.catalog.loader import force_reload
    _assert_runtime_writable()
    if not reason.strip() or action not in {'replace', 'retire'} or not request_id or len(request_id)>100:
        raise ValueError('policy_review_required')
    identity = 'event-policy-review:' + digest([actor, request_id])
    fingerprint = digest([kind, entry_id, source_digest, reason, action, replacement])
    with session_scope() as db:
        db.execute(text('BEGIN IMMEDIATE'))
        user = db.get(User, actor)
        if not user or not is_admin_role(user.role):
            raise PermissionError('policy_admin_required')
        prior = db.get(CatalogMeta, identity)
        if prior:
            receipt = json.loads(prior.value)
            if receipt['fingerprint'] != fingerprint:
                raise ValueError('policy_request_conflict')
            return {'request_id': request_id, 'status': receipt['status']}
        row = db.query(CatalogEntry).filter_by(kind=kind, id=entry_id).one_or_none()
        if row is None:
            raise ValueError('policy_entry_missing')
        original = _snapshot(row)
        if digest(original) != source_digest:
            raise ValueError('policy_revision_conflict')
        if action == 'replace':
            if entry_id in RETIRED or (kind != 'recovery' and not entry_id.startswith('guard.')) or not isinstance(replacement, dict) or references(replacement):
                raise ValueError('policy_replacement_invalid')
            payload = deepcopy(replacement)
            payload['tier'] = 'policy'
            RecoveryRule(id=entry_id, **{k:v for k,v in payload.items() if k!='id'})
            if not payload.get('match') or not payload.get('verify') or not payload.get('actions'):
                raise ValueError('policy_match_actions_verify_required')
            from mino_nexus.catalog.registry import get_capability
            for step in payload['actions']:
                cap = get_capability(step['capability'])
                if cap is None or not cap.enabled or cap.lifecycle != 'active' or step['capability'].startswith('guard.'):
                    raise ValueError('policy_action_unavailable')
            if row.kind != 'recovery':
                conflict = db.query(CatalogEntry).filter_by(kind='recovery', id=entry_id).one_or_none()
                if conflict is not None:
                    raise ValueError('policy_kind_conflict')
                row.kind = 'recovery'
            if entry_id.startswith('guard.'):
                payload['platforms'] = []
                row.platforms_json = []
            row.payload_json = payload
            status = 'replaced'
        else:
            row.enabled = False
            row.lifecycle = 'deprecated'
            status = 'retired'
        db.add(CatalogMeta(key=identity, value=json.dumps({'fingerprint':fingerprint, 'status':status,
            'actor':actor, 'reason':reason, 'reviewed_at':now(), 'before':original,
            'after':_snapshot(row)}, ensure_ascii=False)))
    force_reload()
    return {'request_id':request_id, 'status':status}
