"""Authenticated adapters for the pure binding resolver and existing session store.

This module does not operate devices, obtain leases, or decide action ordering.
"""
from copy import deepcopy
import hashlib
import json

from mino_nexus.core.execution_user import require_user_id
from mino_nexus.services.execution_binding import (
    CONTRACT, BindingResolution, dispatch_projection, resolve_execution_binding,
)

_FRAMEWORK_PHASES = {'probe': ['prep'], 'open_web_v5': ['prep'],
                     'screenshot': ['check'], 'close_web_v5': ['cleanup']}
_STRICT_EVENTS = {*_FRAMEWORK_PHASES, 'tap_target', 'input_target', 'read_web_target_facts'}
_EPOCHS = ('node_connection_generation', 'scout_process_generation', 'capability_revision')


class BindingAdmissionError(ValueError):
    def __init__(self, reason_code, diagnostics=()):
        self.reason_code = reason_code
        self.diagnostics = deepcopy(list(diagnostics))
        super().__init__(reason_code)


def _unavailable(status, reason, responsibility):
    return BindingResolution(status, diagnostics=[{'reason_code': reason,
        'responsibility': responsibility, 'missing_fields': [],
        'recovery_entry': 'workbench.execution_review', 'recheckable': True}])


def _digest(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def strict_requirements(case, app_id):
    """Project immutable reviewed semantics; no target lookup or binding resolution."""
    frozen = case.get('v5_publication') or {}
    revision = frozen.get('frozen_revision')
    if type(revision) is not int or revision < 1:
        raise BindingAdmissionError('plan_invalid')
    operations, dependency_specs = [], {}
    from mino_nexus.services.execution_dependencies import declare

    def add(ref, event_id, phase, **refs):
        operation = {'operation_ref': ref, 'event_id': event_id, 'phase': phase,
            'applicable': True, 'case_ref': case['case_id'], 'dependencies': [], **refs}
        declare(dependency_specs, operation, {'kind': 'environment'})
        declare(dependency_specs, operation, {'kind': 'capability', 'capability_id': event_id, 'executor': 'playwright'})
        operations.append(operation)
        return operation

    add('system:probe', 'probe', 'prep')
    add('system:open', 'open_web_v5', 'prep')
    add('system:baseline', 'read_web_target_facts', 'prep')
    for action in frozen.get('actions') or []:
        ref = str(action.get('operation_id') or '')
        if not ref or action.get('capability_id') not in {'tap_target', 'input_target'}:
            raise BindingAdmissionError('plan_invalid')
        operation = add('action:' + ref, action['capability_id'], 'do', step_ref=str(action.get('case_step') or ''))
        resource = (action.get('payload') or {}).get('resource_ref')
        if resource is not None:
            if not isinstance(resource, dict) or set(resource) != {'account_id', 'field'}:
                raise BindingAdmissionError('dependency_contract_unsupported')
            declare(dependency_specs, operation, {'kind': 'account_field', **resource})
        if action['capability_id'] == 'input_target':
            add('classify:' + ref, 'read_web_target_facts', 'do')
    for check in (frozen.get('check_program_plan') or {}).get('checkpoints') or []:
        ref = str(check.get('point_id') or '')
        if not ref:
            raise BindingAdmissionError('plan_invalid')
        add('check:' + ref, 'read_web_target_facts', 'check', clause_ref=ref)
    add('system:screenshot', 'screenshot', 'check')
    add('system:close', 'close_web_v5', 'cleanup')
    identity = f"{case['project_id']}:{case['case_id']}:publication:{revision}"
    return {'applicable': True, 'plan_ref': identity, 'project_id': case['project_id'],
        'app_id': app_id, 'owner_scope_ref': 'user:' + require_user_id(),
        'requirements_ref': 'requirements-' + _digest({'plan_ref': identity, 'operations': operations}),
        'source_revision': str(revision), 'created_at': frozen.get('published_at', ''),
        'timeout_profile_ref': 'web.atomic.v1', 'evidence_policy_ref': 'web.atomic.v1',
        'execution_mode': 'v5', 'action_scheme': 'dom_preferred', 'operations': operations,
        'dependency_specs': dependency_specs}


def _implementation(value, strict):
    result = deepcopy(value)
    # Versioned bridge for the already implemented strict primitive contract only.
    if strict and result.get('id') == 'web.strict.v1' and result.get('executor') == 'playwright':
        for key, default in {'runs_on': 'scout', 'params_contract': {'version': 1},
                             'result_contract': {'version': 1},
                             'device_hint_template': {'execution_mode': 'v5'}}.items():
            result.setdefault(key, default)
    return result


def control_probe_requirements(project_id, control):
    """A configuration probe has its own project-scoped session, never a case verdict."""
    identity = f"{project_id}:control:{control['control_id']}:revision:{control['revision']}"
    operations = [{'operation_ref': ref, 'event_id': cid, 'phase': phase,
                   'applicable': True, 'dependencies': []} for ref, cid, phase in (
        ('system:open', 'open_web_v5', 'prep'),
        ('control:read', 'read_web_target_facts', 'check'),
        ('system:close', 'close_web_v5', 'cleanup'))]
    from mino_nexus.services.execution_dependencies import declare
    dependency_specs = {}
    for operation in operations:
        declare(dependency_specs, operation, {'kind': 'environment'})
        declare(dependency_specs, operation, {'kind': 'capability',
            'capability_id': operation['event_id'], 'executor': 'playwright'})
    return {'applicable': True, 'plan_ref': identity, 'project_id': project_id,
        'app_id': 'control-probe:' + project_id, 'owner_scope_ref': 'user:' + require_user_id(),
        'requirements_ref': 'requirements-' + _digest({'plan_ref': identity, 'operations': operations}),
        'source_revision': str(control['revision']), 'created_at': control.get('updated_at') or identity,
        'timeout_profile_ref': 'web.atomic.v1', 'evidence_policy_ref': 'web.atomic.v1',
        'execution_mode': 'v5', 'purpose': 'configuration_probe', 'action_scheme': 'dom_preferred',
        'operations': operations, 'dependency_specs': dependency_specs}


def _events(requirements, device=None):
    from mino_nexus.catalog import registry
    strict = requirements.get('execution_mode') == 'v5'
    from mino_nexus.services.execution_requirements import adapt_event, PHASES
    device = device or {}
    events = {}
    for operation in requirements.get('operations') or []:
        cid = operation.get('event_id')
        if cid in events:
            continue
        if strict and cid in _FRAMEWORK_PHASES:
            catalog_entry = registry.get_capability(cid)
            events[cid] = {'id': cid,
                'enabled': catalog_entry.enabled if catalog_entry is not None else True,
                'lifecycle': catalog_entry.lifecycle if catalog_entry is not None else 'active', 'platforms': ['web'],
                'caller': 'program', 'phases': _FRAMEWORK_PHASES[cid], 'implementations': [{
                    'id': 'web.strict.v1', 'executor': 'playwright', 'runs_on': 'scout', 'low_level': {},
                    'params_contract': {'version': 1}, 'result_contract': {'version': 1},
                    'device_hint_template': {'execution_mode': 'v5'}}]}
            continue
        cap = registry.get_capability(cid)
        if not strict and device.get('platform') in {'desktop', 'mac', 'win'}:
            from mino_nexus.services.desktop_catalog import CAPABILITY_IDS, capabilities
            if cid in CAPABILITY_IDS:
                cap = next((row for row in capabilities(device.get('capabilities') or [], device.get('platform') if device.get('platform') in {'mac', 'win'} else device.get('os', ''), platform=device.get('platform')) if row.id == cid), None)
        if not strict and cid in {'screenshot', 'hierarchy'}:
            from mino_nexus.loop.router_proxy import executors_for_platform
            events[cid] = adapt_event({'id': cid, 'enabled': cap.enabled if cap else True,
                'lifecycle': cap.lifecycle if cap else 'active', 'platforms': [device.get('platform')],
                'caller': 'system', 'phases': list(PHASES), 'implementations': [
                    {'id': 'observe.v1:' + ex, 'executor': ex, 'low_level': {}}
                    for ex in executors_for_platform(device.get('platform', ''))]}, platform=device.get('platform'))
            continue
        if not strict and cid == 'close_app' and device.get('platform') == 'web':
            events[cid] = adapt_event({'id': cid, 'enabled': cap.enabled if cap else True,
                'lifecycle': cap.lifecycle if cap else 'active', 'platforms': ['web'],
                'caller': 'system', 'phases': ['prep', 'cleanup'], 'implementations': [
                    {'id': 'web.cleanup.v1', 'executor': 'playwright', 'low_level': {}}]}, platform='web')
            continue
        if not strict and cid.startswith('recover_'):
            rule_id = cid[len('recover_'):]
            rule = next((row for row in registry.list_recovery_rules(enabled_only=True) if row.id == rule_id), None)
            frozen = (requirements.get('recovery_refs') or {}).get(rule_id) or {}
            if rule and rule.lifecycle == 'active' and _digest(rule.model_dump()) == frozen.get('revision'):
                events[cid] = adapt_event({'id': cid, 'enabled': True, 'lifecycle': 'active',
                    'platforms': rule.platforms, 'caller': operation.get('caller', 'case'),
                    'phases': ['prep', 'do'], 'implementations': []}, platform=device.get('platform'))
            continue
        if cap is not None:
            value = cap.model_dump()
            value['implementations'] = [_implementation(item, strict) for item in value.get('implementations', [])
                if not strict or (item.get('id') == 'web.strict.v1' and item.get('executor') == 'playwright'
                                  and not item.get('needs_vlm'))]
            if not strict:
                value = adapt_event(value, platform=device.get('platform'))
                if cid == 'probe_device_state' and not value.get('phases'):
                    value['phases'] = list(PHASES)
            events[cid] = value
    return events


def resolve_live(requirements, target_context):
    """Preflight and attempt use the same fresh authenticated snapshots."""
    from mino_nexus.services.access_scope import require_owned_device
    from mino_nexus.services.device_targets import target_channel
    from mino_nexus.services.node_registry import get_registry, NodeSession
    from mino_nexus.core import protocol as P
    from mino_nexus.loop.router_proxy import executors_for_device

    uid = require_user_id()
    sn = str(target_context.get('sn') or '')
    require_owned_device(sn)
    if requirements.get('owner_scope_ref') != 'user:' + uid:
        raise BindingAdmissionError('binding_owner_mismatch')
    configuration_probe = requirements.get('purpose') == 'configuration_probe'
    if configuration_probe:
        pid = str(requirements.get('project_id') or '')
        _, env_doc = binding_target_config(pid)
        if requirements.get('app_id') != 'control-probe:' + pid:
            raise BindingAdmissionError('binding_project_mismatch')
        from urllib.parse import urlsplit
        url = str(target_context.get('probe_url') or '')
        parsed = urlsplit(url)
        if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password:
            return _unavailable('needs_input', 'invalid_probe_url', 'input')
        app = {'id': requirements['app_id'], 'project_id': pid}
    else:
        app, env_doc = binding_target_config(str(requirements.get('project_id') or ''), str(requirements.get('app_id') or ''))
    if app.get('project_id') != requirements.get('project_id'):
        raise BindingAdmissionError('binding_project_mismatch')
    snapshot = get_registry().execution_snapshot(sn)
    if not snapshot or snapshot.get('owner_user_id') != uid:
        return _unavailable('blocked', 'binding_target_unavailable', 'runtime')
    device = snapshot['device']
    node, _ = get_registry().resolve(sn)
    view = NodeSession(node_id=snapshot['node_id'], executors={row['id']: P.ExecutorManifest(**row)
        for row in snapshot['executors']}, devices={sn: P.DeviceManifest(**device)})
    if configuration_probe:
        if device.get('platform') != 'web':
            raise BindingAdmissionError('event_platform_unsupported')
        surface, profile, identifier = 'web', 'configuration_probe', url
    else:
        try:
            channel = target_channel(app, device, surface=target_context.get('env_surface', ''),
                                     profile=target_context.get('env_profile', ''), env_doc=env_doc)
        except (ValueError, KeyError):
            return _unavailable('needs_input', 'environment_configuration_invalid', 'input')
        surface, profile = str(channel.get('id') or ''), str(channel.get('env_profile') or '')
        from mino_nexus.services.app_automation import package_for_app
        identifier = package_for_app(app, env_profile=profile,
            platform=channel.get('platform') or device.get('platform'), surface=surface, env_doc=env_doc)
        if not identifier:
            return _unavailable('needs_input', 'binding_target_missing', 'input')
    target = {'node_id': snapshot['node_id'], 'sn': sn, 'platform': device.get('platform'),
        'env_profile': profile, 'env_surface': surface,
        'target_ref': 'target-' + _digest({'app_id': app['id'], 'surface': surface, 'profile': profile,
            'identifier': identifier}),
        'online': bool(snapshot['online'] and node is not None),
        'connected_executors': executors_for_device(sn, view),
        **{key: snapshot[key] for key in _EPOCHS}}
    # Host plugins are explicit same-node channels, independent of the device UI
    # family. A manifest and configuration dependency are both still required.
    if any(row.get('event_id') == 'plugin.gmail.fetch_otp' for row in requirements['operations']):
        target['connected_executors'] += [row['id'] for row in snapshot['executors']
            if row['id'] == 'host_plugin' and row.get('available') is True]
    events = _events(requirements, device)
    facts, resource_state = _dependency_facts(requirements, target_context, target, identifier, env_doc,
                                             node=node, events=events, executors=snapshot['executors'])
    target['resource_state'] = resource_state
    # A selected dynamic source requires its child, not merely an optional route.
    if facts.get('resource:otp_source', {}).get('reason_code') == 'plugin_configured':
        requirements = deepcopy(requirements)
        parents = {row['phase']: row for row in requirements['operations'] if row['event_id'] == 'get_otp'}
        for row in requirements['operations']:
            if row['event_id'] == 'plugin.gmail.fetch_otp' and row['phase'] in parents:
                row['optional'] = parents[row['phase']].get('optional', False)
    capabilities = {**snapshot, 'events': events, 'dependency_facts': facts}
    if requirements.get('execution_mode') == 'v5' and device.get('platform') == 'web':
        # The v1 strict runtime advertises its fixed primitives through its versioned
        # adapter; the epoch remains the hash of the unmodified registered manifest.
        for row in capabilities['executors']:
            if row['id'] == 'playwright':
                row['provides'] = sorted(set(row.get('provides') or []) | _STRICT_EVENTS)
    from mino_nexus.services.execution_dependencies import collect
    try:
        capabilities['dependency_facts'] = {**collect(requirements, target, capabilities,
            run_id=str(target_context.get('attempt_id') or '')), **facts}
    except ValueError as exc:
        raise BindingAdmissionError('dependency_contract_unsupported') from exc
    caller = 'program' if requirements.get('execution_mode') == 'v5' else 'case'
    policy = {'policy_ref': 'runtime-binding-policy.v1', 'caller': caller,
        'allowed_phases': ['prep', 'do', 'check', 'cleanup'],
        'target_scope': {'owner_scope_ref': 'user:' + uid, 'project_id': app['project_id'], 'app_id': app['id']}}
    if requirements.get('execution_mode') != 'v5':
        policy['operation_callers'] = {row['operation_ref']: row.get('caller', 'case') for row in requirements['operations']}
        local = {}
        for cap in capabilities['events'].values():
            for impl in cap.get('implementations') or []:
                if impl.get('runs_on') == 'nexus' and str(impl.get('id', '')).startswith('nexus.local.v1:'):
                    row = local.setdefault(impl['executor'], {'available': True, 'provides': []})
                    row['provides'].append(cap['id'])
        policy['local_executors'] = local
    return resolve_execution_binding(requirements, target, capabilities, policy, CONTRACT)


def require_ready(result):
    if result.status != 'ready' or result.binding is None:
        reason = next((row['reason_code'] for row in result.diagnostics), 'binding_not_ready')
        raise BindingAdmissionError(reason, result.diagnostics)
    return result.binding


def bind_attempt(session_id, requirements, target_context, *, deadline=None):
    from mino_nexus.services.execution_binding_store import persist_binding
    from mino_nexus.services.session_store import get_meta
    try:
        binding = require_ready(resolve_live(requirements, target_context))
        meta = get_meta(session_id)
        if meta is None or meta.get('app_id') != requirements.get('app_id'):
            raise BindingAdmissionError('binding_session_mismatch')
        persist_binding(session_id, binding, deadline=deadline)
    except BindingAdmissionError:
        raise
    except (ValueError, PermissionError) as exc:
        raise BindingAdmissionError(str(exc)) from exc
    except Exception as exc:
        raise BindingAdmissionError('binding_store_unavailable') from exc
    return binding


def committed_projection(session_id, binding_id, requirements, target_context, *, event_id, operation_ref, phase,
                         dynamic_target=None, input_combination=None):
    from mino_nexus.services.session_store import get_meta
    meta = get_meta(session_id)
    context = (meta or {}).get('runtime_context') or {}
    binding = context.get('execution_binding')
    ref = (context.get('runtime_state') or {}).get('execution_bound_ref')
    if (not isinstance(binding, dict) or binding.get('binding_id') != binding_id
            or not isinstance(ref, dict) or ref.get('session_id') != session_id or type(ref.get('seq')) is not int):
        raise BindingAdmissionError('binding_not_committed')
    if (meta or {}).get('finalization_id') or (meta or {}).get('finished_at'):
        raise BindingAdmissionError('binding_attempt_finalized')
    live = require_ready(resolve_live(requirements, {**target_context, "_attempt_admitted": True}))
    for key in _EPOCHS:
        if live[key] != binding[key]:
            raise BindingAdmissionError(key + '_changed')
    if live['binding_id'] != binding_id:
        raise BindingAdmissionError('binding_policy_changed')
    if event_id in {'open_web_v5', 'open_url', 'launch_app', 'open_app'} and dynamic_target:
        target_ref = 'target-' + _digest({'app_id': binding['app_id'], 'surface': binding['target']['env_surface'],
            'profile': binding['target']['env_profile'], 'identifier': dynamic_target})
        if target_ref != binding['target']['target_ref']:
            raise BindingAdmissionError('binding_target_changed')
    operations = [row for row in binding['operations'] if row['event_id'] == event_id
                  and row['phase'] == phase
                  and (not operation_ref or row['operation_ref'] == operation_ref)
                  and (input_combination is None or row.get('input_combination') == input_combination)]
    if not phase or not operations or any(row['phase'] != phase for row in operations):
        raise BindingAdmissionError('binding_phase_mismatch')
    if len(operations) != 1:
        raise BindingAdmissionError('binding_operation_ambiguous')
    return {**dispatch_projection(binding, operations[0]['operation_ref']),
            'input_combination': deepcopy(operations[0].get('input_combination') or {})}


def menu_projection(binding, phase):
    """Only bound events in the current phase can appear in a bound menu."""
    from mino_nexus.services.execution_binding import validate_binding
    value = validate_binding(binding)
    return {operation['event_id'] for operation in value['operations'] if operation['phase'] == phase}


def _dependency_facts(requirements, target_context, target, identifier, env_doc, *, node=None, events=None, executors=()):
    """Read-only facts. No account import, lease acquisition or provider secrets in output."""
    from mino_nexus.services.device_resource_lease import get_active_lease
    from mino_nexus.services.device_admission import busy_for
    from mino_nexus.services.run_write_safety import protected
    rid = str(target_context.get('task_id') or '')
    lease = get_active_lease(target['sn'], identifier)
    holder = str((lease or {}).get('run_id') or '')
    busy = busy_for({'sn': target['sn'], 'node_id': target['node_id'], 'platform': target['platform']},
                    headless=target_context.get('headless', True))
    if rid and busy and busy != rid:
        from mino_nexus.core.database import session_scope
        from mino_nexus.models.device_reservation import DeviceReservation
        from mino_nexus.services.device_admission import resource_group
        group = resource_group({'sn': target['sn'], 'node_id': target['node_id'], 'platform': target['platform']},
                               headless=target_context.get('headless', True))
        with session_scope() as db:
            reservations = db.query(DeviceReservation).filter(DeviceReservation.run_id == rid).all()
            if any(row.resource_key == group or row.resource_key.startswith(group + ':') for row in reservations):
                busy = rid
    state = 'isolated' if (holder and protected(holder, sn=target['sn'])) or (rid and protected(rid, sn=target['sn'])) else (
        'contended' if (holder and holder != rid) or (busy and busy != rid) else 'ready')
    facts = {}
    if requirements.get('execution_mode') == 'v5':
        return facts, state
    from mino_nexus.services.settings import should_use_ai_planning
    model = should_use_ai_planning('case_execution', provider_id=requirements.get('provider_id') or None)
    facts['model:case_execution'] = {'status': 'ready' if model.get('enabled') else 'unknown'}
    from mino_nexus.services.resource_preflight import run_start_resource_blockers
    blockers = [] if target_context.get('_attempt_admitted') else run_start_resource_blockers(requirements.get('resource_claim'),
        scene=requirements.get('resource_scene'), precondition=requirements.get('resource_precondition', ''),
        sn=target['sn'], package_id=identifier)
    facts['resource:preflight'] = {'status': 'blocked' if blockers else 'ready'}
    if any(row.get('event_id') == 'get_otp' for row in requirements.get('operations', [])):
        from mino_nexus.services.otp_resolve import resolve_effective_secrets
        otp = resolve_effective_secrets(env_doc, env_profile=target['env_profile'],
                                       env_surface=target['env_surface']).get('otp') or {}
        from mino_nexus.services.execution_otp_contract import otp_facts
        facts.update(otp_facts(otp, node=node, uid=require_user_id(), events=events or {}, executors=executors))
    from mino_nexus.services.nav_flow_block_catalog import load_block_row, resolve_effective_steps
    for block_id, expected in (requirements.get('block_revisions') or {}).items():
        row = load_block_row(app_id=requirements['app_id'], block_id=block_id) or {}
        content = {'version': row.get('version'), 'enabled': row.get('enabled'),
                   'steps': resolve_effective_steps(app_id=requirements['app_id'], block_id=block_id)}
        actual = hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()[:16]
        facts['block:' + block_id] = {'status': 'ready' if actual == expected else 'blocked'}
    if requirements.get('lease_requirements') and target_context.get('_attempt_admitted'):
        facts['resource:account'] = {'status': 'ready'}  # Already admitted; own lease state is checked above.
    elif requirements.get('lease_requirements'):
        from mino_nexus.services.pool_account_store import list_accounts
        from mino_nexus.services.resource_pool import match_requirements, lease_blocks_other_holder, account_facet_values_for_match
        account = (requirements.get('resource_claim') or {}).get('account') or {}
        req = account.get('requirements') or {}
        # env_doc=None is intentional: preview never imports accounts into SQLite.
        rows = list_accounts(requirements['project_id'])
        matched = [row for row in rows if not row.get('locked')
                   and (not row.get('env') or row['env'] == target['env_profile'])
                   and match_requirements(account_facet_values_for_match(row), req)[0]]
        usable = [row for row in matched if not lease_blocks_other_holder(row.get('lease') or {}, run_id=rid, sn=target['sn'])
                  and not protected(str((row.get('lease') or {}).get('run_id') or ''), sn=str((row.get('lease') or {}).get('sn') or ''))]
        facts['resource:account'] = {'status': 'ready' if usable else 'blocked' if matched else 'unknown'}
    return facts, state


def binding_target_config(project_id, app_id=''):
    """Authenticated, strictly read-only view; no legacy lazy ownership/pool migration."""
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.project import Project, App
    from mino_nexus.services.project_env import env_from_project_row
    from mino_nexus.services.project_pool_config_store import load_pool_config, merge_pool_config_into_doc
    from mino_nexus.models.project_pool_config import ProjectPoolConfig
    uid = require_user_id()
    with session_scope() as db:
        project = db.get(Project, project_id)
        # Project reads are shared for authenticated users, as in project/case APIs.
        # Device, model configuration and the resulting binding remain user scoped.
        if project is None:
            raise BindingAdmissionError('binding_project_scope_denied')
        app = db.get(App, app_id) if app_id else None
        if app_id and (app is None or app.project_id != project_id):
            raise BindingAdmissionError('binding_project_mismatch')
        value = {'id': app.id, 'project_id': app.project_id, 'env': deepcopy(app.env or {})} if app else {}
        env = env_from_project_row({'env': deepcopy(project.env or {})}, [value] if value else [])
        if db.get(ProjectPoolConfig, project_id) is not None:
            env = merge_pool_config_into_doc(env, load_pool_config(project_id))
    return value, env
