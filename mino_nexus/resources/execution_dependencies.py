"""Bounded dependency facts from authenticated metadata; never acquire or execute.

Only IDs, statuses and fixed reason codes leave this module. A ready fact proves
configuration/admission, not a fresh OTP, a login result or successful model I/O.
"""
from __future__ import annotations

import hashlib
import json

from mino_nexus.contract.binding import MAX_DEPENDENCIES

_FIELDS = frozenset({'username', 'email', 'phone', 'password', 'otp'})
_KEYS = {
    'environment': {'kind'},
    'account_field': {'kind', 'account_id', 'field'},
    'plugin': {'kind', 'class', 'plugin_id', 'node_id'},
    'capability': {'kind', 'capability_id', 'executor'},
    'ai_model': {'kind', 'provider_id'},
}


def declare(specs, operation, spec):
    """Only adapters create specs; compiled refs cannot inject facts or credentials."""
    validate_spec(spec)
    ref = 'dep-' + hashlib.sha256(json.dumps(spec, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    specs[ref] = dict(spec)
    operation.setdefault('dependencies', []).append({'id': ref, 'mode': 'all', 'refs': [ref]})
    return ref


def validate_spec(spec):
    if not isinstance(spec, dict) or spec.get('kind') not in _KEYS:
        raise ValueError('dependency_contract_unsupported')
    if set(spec) - _KEYS[spec['kind']] or any(not isinstance(v, str) or not v or len(v) > 256 for v in spec.values()):
        raise ValueError('dependency_contract_unsupported')
    kind = spec['kind']
    required = {'account_field': {'account_id', 'field'}, 'plugin': {'class', 'plugin_id'},
                'capability': {'capability_id'}}.get(kind, set())
    if not required <= spec.keys() or (kind == 'account_field' and spec['field'] not in _FIELDS):
        raise ValueError('dependency_contract_unsupported')


def fact(status, reason, **refs):
    return {'status': status, 'reason_code': reason, **refs}


def account_fact(account, spec, *, profile, run_id, sn):
    from mino_nexus.resources.resource_pool import lease_blocks_other_holder
    from mino_nexus.records.write_state import protected
    if not account:
        return fact('needs_input', 'account_missing')
    if account.get('env') and account['env'] != profile:
        return fact('needs_input', 'account_environment_mismatch')
    if account.get('locked'):
        return fact('blocked', 'account_locked')
    lease = account.get('lease') or {}
    if protected(str(lease.get('run_id') or ''), sn=str(lease.get('sn') or '')):
        return fact('blocked', 'account_isolated')
    if lease_blocks_other_holder(lease, run_id=run_id, sn=sn):
        return fact('blocked', 'account_busy')
    if not account.get(spec['field']):
        return fact('needs_input', 'account_field_missing')
    return fact('ready', 'account_field_configured')


def account_metadata(project_id, spec):
    # Do not invoke list_test_accounts: its legacy read path can migrate data.
    # Select presence in SQL so secret values never enter this adapter.
    from sqlalchemy import func
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.pool_account import PoolAccount
    with session_scope() as db:
        row = db.query(PoolAccount.env, PoolAccount.locked, PoolAccount.lease,
            (func.length(getattr(PoolAccount, spec['field'])) > 0).label('present')).filter(
                PoolAccount.project_id == project_id, PoolAccount.id == spec['account_id']).one_or_none()
        return ({'env': row.env, 'locked': row.locked, 'lease': row.lease,
                 spec['field']: bool(row.present)} if row is not None else None)


def plugin_fact(nodes, spec, uid):
    """Selection matches plugin routing; offline/configuration/ambiguity stay distinct."""
    owned = [n for n in nodes if n.owner_user_id == uid and
             (not spec.get('node_id') or n.node_id == spec['node_id'])]
    if spec.get('node_id') and not owned:
        return fact('needs_input', 'plugin_node_unavailable')
    configured = []
    for node in owned:
        for row in node.plugins or []:
            same = row.get('class') == spec['class'] or (spec['plugin_id'] == 'wechat' and
                    {row.get('class'), spec['class']} <= {'bot', 'im'})
            if same and row.get('id') == spec['plugin_id'] and row.get('configured') is True:
                configured.append(node)
                break
    live = [n for n in configured if n.alive and n.send is not None]
    if len(live) > 1:
        return fact('needs_input', 'plugin_node_ambiguous')
    if len(live) == 1:
        node = live[0]
        return fact('ready', 'plugin_configured', node_id=node.node_id,
                    node_connection_generation=node.node_connection_generation,
                    scout_process_generation=node.scout_process_generation)
    if configured or (owned and all(not n.alive or n.send is None for n in owned)):
        return fact('blocked', 'plugin_node_offline')
    if any(n.plugins is None for n in owned):
        return fact('blocked', 'plugin_manifest_unreported')
    return fact('needs_input', 'plugin_not_configured')


def collect(requirements, target, snapshot, *, run_id=''):
    """Called after runtime owner/app/target validation, re-read before dispatch."""
    from mino_nexus.platform.identity import require_user_id
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.project import Project
    from mino_nexus.resources.node_registry import get_registry
    uid = require_user_id()
    if requirements.get('owner_scope_ref') != 'user:' + uid:
        raise PermissionError('binding_owner_mismatch')
    pid = str(requirements.get('project_id') or '')
    with session_scope() as db:
        if db.get(Project, pid) is None:
            raise ValueError('binding_project_scope_denied')
    specs = requirements.get('dependency_specs', {})
    operations = requirements.get('operations') or []
    if not isinstance(specs, dict) or len(specs) > MAX_DEPENDENCIES * max(1, len(operations)):
        raise ValueError('dependency_contract_unsupported')
    refs = set()
    for op in operations:
        if not isinstance(op, dict):
            raise ValueError('dependency_contract_unsupported')
        if op.get('applicable') is False:
            continue
        deps = op.get('dependencies', [])
        if not isinstance(deps, list) or len(deps) > MAX_DEPENDENCIES:
            raise ValueError('dependency_contract_unsupported')
        for dep in deps:
            if not isinstance(dep, dict) or set(dep) - {'id', 'mode', 'refs'}:
                raise ValueError('dependency_contract_unsupported')
            members = dep.get('refs')
            if (dep.get('mode', 'all') not in {'all', 'any'} or not isinstance(dep.get('id'), str)
                    or not dep['id'] or not isinstance(members, list) or not members or len(members) > MAX_DEPENDENCIES
                    or any(not isinstance(ref, str) or not ref for ref in members)):
                raise ValueError('dependency_contract_unsupported')
            refs.update(members)
    facts = {}
    for ref in sorted(refs):
        spec = specs.get(ref)
        if spec is None:
            facts[ref] = fact('needs_input', 'dependency_definition_missing')
            continue
        validate_spec(spec)
        kind = spec['kind']
        if kind == 'account_field':
            account = account_metadata(pid, spec)
            facts[ref] = account_fact(account, spec, profile=target['env_profile'], run_id=run_id, sn=target['sn'])
        elif kind == 'environment':
            facts[ref] = fact('ready', 'environment_resolved', target_ref=target['target_ref'])
        elif kind == 'plugin':
            facts[ref] = plugin_fact(get_registry().nodes(), spec, uid)
        elif kind == 'capability':
            available = [row for row in snapshot.get('executors', []) if row.get('available') is True
                         and row.get('id') in target['connected_executors']
                         and (not spec.get('executor') or row['id'] == spec['executor'])
                         and spec['capability_id'] in (row.get('provides') or [])]
            facts[ref] = fact('ready', 'capability_available') if available else fact('blocked', 'capability_unavailable')
        elif kind == 'ai_model':
            facts[ref] = model_fact(uid, spec.get('provider_id', ''))
    return facts


def model_fact(uid, provider_id='', *, explicit_selection=False):
    from mino_nexus.platform.settings_store import list_ai_providers, find_case_execution_provider_id
    config = list_ai_providers(user_id=uid)
    provider_id = provider_id or find_case_execution_provider_id(user_id=uid)
    row = next((r for r in config['providers'] if r['id'] == provider_id), {})
    gate = bool(explicit_selection and provider_id) or bool(row.get('case_execution_use'))
    ready = bool(gate and row.get('configured') and row.get('enabled') and row.get('model'))
    return (fact('ready', 'model_configured', provider_id=provider_id, model=row['model'])
            if ready else fact('needs_input', 'model_configuration_missing'))
