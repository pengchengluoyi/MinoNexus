"""Compile-time semantic requirements and versioned ordinary execution adapters.

No resolver, device access or model calls. Nested blocks are flattened only by
compile_steps; preview/attempt consume that persisted result verbatim.
"""
from copy import deepcopy
from mino_nexus.contract.executor_sets import CHILDREN, LOCAL_EXECUTORS  # noqa: F401
from types import SimpleNamespace
import hashlib
import json

PHASES = ('prep', 'do', 'check', 'cleanup')
# Navigation policy names and compile-time markers, not catalog_entries ids.
NON_CATALOG_EVENT_IDS = frozenset({'nav_target'})
POLICY_NAV_CAPS = ('tap_element', 'press_key')
# 旧 Scout 兼容：老版本上报的是抽象通道名（ui_native_input/system_shell…）。
# 新 Scout 直接上报具体事件 id，绑定走 cid in provides 这条主路径，不依赖本表。
ABSTRACT = {
    'tap_element': 'ui_native_input', 'multi_tap': 'ui_native_input',
    'long_press_element': 'ui_native_input', 'swipe_direction': 'ui_native_input',
    'swipe_element_to_element': 'ui_native_input', 'input_text': 'ui_input_text',
    'screenshot': 'ui_screenshot', 'hierarchy': 'ui_native_input',
    'probe_device_state': 'read_system_data', 'read_device_data': 'read_system_data',
    'get_app_version': 'read_system_data', 'get_foreground_app': 'read_system_data',
    'launch_app': 'app_launch_native', 'open_app': 'app_launch_native',
    # pm clear 通过 shell 执行；Scout 的 adb 执行器提供 system_shell。
    'clear_app_cache': 'system_shell', 'kill_app': 'app_force_stop',
    'close_app': 'app_force_stop', 'press_key': 'key_event', 'set_clipboard': 'clipboard_set',
    'wake_screen': 'key_event', 'dismiss_keyguard': 'system_shell',
    'open_url': 'ui_native_input', 'read_web_auth': 'ui_native_input',
    'reload_page': 'ui_native_input',
}
# Local orchestration still dispatches every child via RouterProxy. These are
# declared potential children; unavailable optional branches cannot execute.


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def case_source_revision(case):
    # Use the same source projection as the persisted compilation input. The
    # legacy precondition_raw alias is not part of that input and must not make
    # a freshly compiled plan stale when compared with the public case row.
    from mino_nexus.contract.case_view import source_view
    return digest(source_view(case))


def _filter_catalog_caps(cap_ids):
    from mino_nexus.catalog.skill_channel import canonical_cap
    return [canonical_cap(cid) for cid in cap_ids if canonical_cap(cid) and canonical_cap(cid) not in NON_CATALOG_EVENT_IDS]


def _policy_nav_step(step):
    if step.get('kind') == 'nav':
        return True
    if str(step.get('nav_target') or '').strip():
        return True
    if str(step.get('key_ref') or '') == 'operation.nav_target':
        return True
    from mino_nexus.catalog.skill_channel import canonical_cap
    return canonical_cap(str(step.get('hook_cap') or '')) == 'nav_target'


def compile_steps(plan, *, app_id):
    """Persist flattened capabilities, retaining source references and rejecting cycles."""
    from mino_nexus.catalog.skill_channel import milestone_cap_ids
    from mino_nexus.navigation.atlas.nav_flow_block_catalog import resolve_effective_steps
    rows = []
    def visit(steps, trail=(), inherited_optional=False):
        for index, step in enumerate(steps):
            if not isinstance(step, dict):
                raise ValueError('compiled_step_invalid')
            ref = str(step.get('id') or step.get('point_id') or index)
            caps = _filter_catalog_caps(milestone_cap_ids(step))
            from mino_nexus.contract.business_event_contract import RETIRED
            if set(caps) & RETIRED:
                raise ValueError('business_event_migration_required')
            from mino_nexus.resources.step_requirements import contract
            requirement = contract(step)
            optional = inherited_optional or requirement['requirement'] == 'optional'
            if requirement['requirement'] == 'conditional':
                optional = False
            elif inherited_optional:
                requirement = {'requirement': 'optional'}
            primary = _filter_catalog_caps(milestone_cap_ids({k: v for k, v in step.items() if k != 'exec_caps'}))
            if _policy_nav_step(step):
                if not str(step.get('nav_target') or '').strip():
                    raise ValueError('navigation_target_required')
                primary = list(POLICY_NAV_CAPS)
                caps = list(dict.fromkeys([*caps, *primary]))
            rows.append({'clause_ref': '/'.join((*trail, ref)), 'event_ids': primary, 'optional': optional, **requirement})
            auxiliary = [cid for cid in caps if cid not in primary]
            if auxiliary:
                rows.append({'clause_ref': '/'.join((*trail, ref, 'branches')), 'event_ids': auxiliary, 'optional': True})
            block = str(step.get('block_id') or '')
            if block:
                if block in trail or len(trail) >= 32:
                    raise ValueError('dependency_contract_unsupported')
                children = resolve_effective_steps(app_id=app_id, block_id=block)
                if not children:
                    raise ValueError('compiled_block_missing')
                visit(children, (*trail, block), optional)
    visit(plan.get('steps') or plan.get('checkpoints') or [])
    return rows


def ordinary_compilation_diagnostics(case):
    """Read-only admission check shared by submission and execution."""
    meta = case.get('meta') or {}
    bundles = meta.get('step_program_keys') if isinstance(meta, dict) else None
    base = {'reason': 'requirements_need_compile',
            'user_message': '执行计划缺少当前绑定合同或与原文不一致，请在 Workbench 用例详情重新编译后再运行。'}
    if not isinstance(bundles, dict) or not bundles:
        return [{**base, 'missing_fields': ['step_program_keys']}]
    revision = case_source_revision(case)
    issues = []
    for step_ref, bundle in bundles.items():
        for phase in ('do', 'check'):
            plan = bundle.get(phase + '_program_plan') if isinstance(bundle, dict) else None
            plan = plan if isinstance(plan, dict) else {}
            missing = [key for key in ('binding_steps', 'binding_source_revision', 'binding_event_contract', 'binding_requirement_contract') if key not in plan]
            stale = plan.get('binding_source_revision') != revision or plan.get('binding_event_contract') != 3 or plan.get('binding_requirement_contract') != 1
            if missing or stale:
                issues.append({**base, 'step_ref': str(step_ref), 'phase': phase,
                               'missing_fields': missing, 'source_revision_mismatch': stale})
    return issues


def ordinary_requirements(case, app_id, *, platform, action_scheme='dom_preferred', provider_id=''):
    from mino_nexus.platform.identity import require_user_id
    from mino_nexus.catalog import registry
    from mino_nexus.catalog.skill_channel import canonical_cap
    from mino_nexus.resources.case_resource_claim import _case_resource_key
    from mino_nexus.casework.prep_plan import build_prep_program_plan
    from mino_nexus.contract.errors import BindingAdmissionError
    meta = case.get('meta') or {}
    bundles = meta.get('step_program_keys')
    diagnostics = ordinary_compilation_diagnostics(case)
    if diagnostics:
        raise BindingAdmissionError('requirements_need_compile', diagnostics)
    operations = {}
    block_revisions = {}
    capability_gaps = []
    def add(cid, phase, *, optional=False, source=None, requirement=None, declared_caller=None):
        cid = canonical_cap(cid)
        from mino_nexus.contract.business_event_contract import RETIRED
        if cid in RETIRED or cid in NON_CATALOG_EVENT_IDS:
            return
        if not cid:
            return
        cap = registry.get_capability(cid)
        caller = declared_caller or (cap.caller if cap is not None else '')
        if cid in {'screenshot', 'hierarchy', 'probe_device_state'} or (cid == 'close_app' and platform == 'web'):
            caller = 'system'
        if not caller:
            # 能力目录缺行是 B 类缺口，不是整案死刑（规划 §4）。跳过该 operation，
            # 运行时真正派发它时由 binding 给出 event_not_registered 并归因。
            if not optional:
                capability_gaps.append({'event_id': cid, 'phase': phase,
                                        'reason_code': 'catalog_capability_missing',
                                        'attribution_class': 'B', 'layer': 'dispatch',
                                        'source': deepcopy(source or {})})
            return
        key = phase + ':' + cid
        row = operations.setdefault(key, {'operation_ref': key, 'event_id': cid, 'phase': phase,
            'applicable': True, 'optional': optional, 'caller': caller, 'case_ref': str(case.get('case_id') or ''),
            'dependencies': [{'id': 'model', 'mode': 'all', 'refs': ['model:case_execution']}], 'source_refs': []})
        row['optional'] = row['optional'] and optional
        req = requirement or {'requirement': 'optional' if optional else 'required'}
        row.setdefault('requirement_clauses', [])
        row['requirement_clauses'].append(deepcopy(req))
        clauses = row['requirement_clauses']
        row['requirement'] = 'required' if any(r['requirement'] == 'required' for r in clauses) else 'conditional' if any(r['requirement'] == 'conditional' for r in clauses) else 'optional'
        if source and source not in row['source_refs']:
            row['source_refs'].append(source)
    for step_ref, bundle in sorted(bundles.items()):
        for phase in ('do', 'check'):
            plan = bundle.get(phase + '_program_plan') or {}
            block_revisions.update(plan.get('block_dependencies') or {})
            for row in plan['binding_steps']:
                for cid in _filter_catalog_caps(row['event_ids']):
                    add(cid, phase, optional=row['optional'], source={'step_ref': step_ref, 'clause_ref': row['clause_ref']}, requirement={k:row[k] for k in ('requirement','condition') if k in row})
    claim = _case_resource_key(case) or {}
    scene = case.get('case_scene') or case.get('scene') or claim.get('case_scene') or {}
    precondition = str(case.get('precondition') or case.get('precondition_raw') or '')
    if precondition and not claim:
        raise BindingAdmissionError('resource_requirements_need_compile')
    prep = build_prep_program_plan(claim=claim, scene=scene, precondition=precondition,
        ctx=SimpleNamespace(platform=platform), case=case)
    for row in prep['steps']:
        if row.get('status') != 'skipped':
            add(row.get('hook_cap', ''), 'prep', source={'step_ref': 'prep', 'clause_ref': row['id']})
    if any(r.get('hook_cap') == 'confirm_login_state' and r.get('required_session') == 'logged_in' for r in prep['steps']):
        add('swipe_direction', 'prep', optional=True,
            source={'step_ref': 'prep', 'clause_ref': 'configured_reauthentication'})
        from mino_nexus.catalog.flow_block_seed import LOGIN_BLOCK_ID
        from mino_nexus.navigation.atlas.nav_block_digest import block_dependencies as _block_dependencies
        login_plan = {'steps': [{'id': 'required_login', 'block_id': LOGIN_BLOCK_ID}]}
        block_revisions.update(_block_dependencies(login_plan, app_id))
        for item in compile_steps(login_plan, app_id=app_id):
            for cid in item['event_ids']:
                add(cid, 'prep', optional=item['optional'], source={'step_ref': 'prep', 'clause_ref': item['clause_ref']}, requirement={k:item[k] for k in ('requirement','condition') if k in item})
    for phase in ('prep', 'do'):
        if any(row['event_id'] == 'confirm_login_state' and row['phase'] == phase for row in operations.values()):
            add('swipe_direction', phase, optional=True,
                source={'step_ref': phase, 'clause_ref': 'login_evidence_navigation'})
    # Framework reads and approved recovery are potential operations of this
    # ordinary plan. They are never expanded recursively by the binding resolver.
    for phase in PHASES:
        add('screenshot', phase, optional=phase == 'cleanup')
        for cid in ('hierarchy', 'get_foreground_app', 'probe_device_state', 'get_app_version'):
            add(cid, phase, optional=True)
    if platform != 'web':
        add('kill_app', 'prep', optional=True)
    if platform == 'web':
        for phase in ('prep', 'cleanup'):
            add('close_app', phase)
    from mino_nexus.contract.interrupt_caps import BUILTIN_INTERRUPT_CAPS
    for phase in ('prep', 'do'):
        for cid in BUILTIN_INTERRUPT_CAPS:
            add(cid, phase, optional=True, source={'runtime_policy': 'screen_interrupt.v1'})
    recovery_refs = {}
    for rule in registry.list_recovery_rules(enabled_only=True):
        if rule.lifecycle != 'active' or (rule.platforms and platform not in rule.platforms):
            continue
        children = [canonical_cap(action.capability) for action in rule.actions]
        recovery_refs[rule.id] = {'revision': digest(rule.model_dump()), 'children': children}
        for phase in ('prep', 'do'):
            add('run_flow_block', phase, optional=True, declared_caller='case')
            for cid in children:
                add(cid, phase, optional=True)
    # Known local children are flattened here, independent of target connectivity.
    pending = list(operations)
    visited = set()
    while pending:
        key = pending.pop(0)
        if key in visited:
            continue
        if len(visited) >= 512:
            raise BindingAdmissionError('dependency_contract_unsupported')
        visited.add(key)
        row = operations[key]
        for child in CHILDREN.get(row['event_id'], ()):
            add(child, row['phase'], optional=True,
                source={'parent_operation_ref': row['operation_ref']} if row['event_id'] == 'get_otp' else None)
            child_key = row['phase'] + ':' + canonical_cap(child)
            if child_key not in visited:
                pending.append(child_key)
    if prep['flags']['need_lease']:
        for row in operations.values():
            row['dependencies'].append({'id': 'account', 'mode': 'all', 'refs': ['resource:account']})
    for row in operations.values():
        if block_revisions:
            row['dependencies'].append({'id': 'compiled_blocks', 'mode': 'all', 'refs': ['block:' + key for key in block_revisions]})
        row['dependencies'].append({'id': 'resource', 'mode': 'all', 'refs': ['resource:preflight']})
        if row['event_id'] == 'get_otp':
            row['dependencies'].append({'id': 'otp_source', 'mode': 'all', 'refs': ['resource:otp_source']})
        if row['event_id'] == 'plugin.gmail.fetch_otp':
            row['dependencies'].append({'id': 'otp_gmail', 'mode': 'all', 'refs': ['resource:otp_gmail']})
    rows = []
    for row in operations.values():
        if row['event_id'] == 'input_text':
            targets = ['current_focus'] if action_scheme == 'visual' or platform in {'desktop', 'mac', 'win'} else ['node', 'current_focus']
            for target in targets:
                for mode in ('replace', 'append'):
                    item = deepcopy(row)
                    item['operation_ref'] += ':' + target + ':' + mode
                    item['input_combination'] = {'contract_version': 2, 'target_mode': target, 'write_mode': mode}
                    # Alternative combinations are optional; the group below must
                    # still have at least one supported route if input is required.
                    item['optional'] = True
                    item['requirement'] = 'optional'
                    rows.append(item)
        else:
            rows.append(row)
    required_groups = [{'id': row['operation_ref'], 'refs': [r['operation_ref'] for r in rows
                        if r['event_id'] == 'input_text' and r['phase'] == row['phase']]}
                       for row in operations.values() if row['event_id'] == 'input_text' and not row['optional']]
    revision = digest({'bundles': bundles, 'claim': claim, 'precondition': precondition, 'scene': scene,
                       'recovery': recovery_refs, 'operations': rows, 'scheme': action_scheme})
    identity = str(case['project_id']) + ':' + str(case['case_id']) + ':' + revision
    return {'applicable': True, 'plan_ref': identity, 'project_id': case['project_id'], 'app_id': app_id,
        'owner_scope_ref': 'user:' + require_user_id(), 'requirements_ref': 'requirements-' + digest(rows),
        'source_revision': revision, 'created_at': case.get('updated_at') or identity,
        'timeout_profile_ref': 'ordinary.v1', 'evidence_policy_ref': 'ordinary.v1',
        'execution_mode': 'agent', 'action_scheme': action_scheme, 'operations': rows,
        'required_operation_groups': required_groups, 'recovery_refs': recovery_refs,
        'provider_id': provider_id, 'block_revisions': block_revisions, 'resource_claim': deepcopy(claim), 'resource_scene': deepcopy(scene),
        'resource_precondition': precondition, 'capability_gaps': capability_gaps,
        'lease_requirements': [{'resource_ref': 'case-resource:' + revision}] if prep['flags']['need_lease'] else []}


def adapt_event(value, *, platform, framework=False):
    """Versioned legacy assembly bridge; preserve explicit disabling and contracts."""
    from mino_nexus.catalog.exec_classes import LOCAL_EXECUTOR_IDS
    value = deepcopy(value)
    cid = value['id']
    if cid in {'persona_subtask', 'human_input_text'}:
        value['implementations'] = []  # Existing implementations explicitly report not implemented.
        return value
    if cid in LOCAL_EXECUTOR_IDS or cid.startswith(('recover_', 'signal_')) or cid in LOCAL_EXECUTORS:
        executor = LOCAL_EXECUTORS.get(cid, 'internal')
        value['implementations'] = [{'id': 'nexus.local.v1:' + cid, 'executor': executor, 'runs_on': 'nexus',
            'low_level': {}, 'params_contract': {'version': 1}, 'result_contract': {'version': 1},
            'device_hint_template': {}}]
        return value
    for impl in value.get('implementations') or []:
        impl.setdefault('runs_on', 'scout')
        impl.setdefault('params_contract', {'version': 1})
        impl.setdefault('result_contract', {'version': 1})
        impl.setdefault('device_hint_template', {'execution_mode': 'agent'})
        required = ABSTRACT.get(cid)
        if impl['executor'] == 'playwright' and cid in {'launch_app', 'open_app', 'get_foreground_app', 'close_app', 'press_key', 'hierarchy'}:
            required = 'ui_native_input'
        if required:
            impl.setdefault('requires_caps', [required])
    return value
