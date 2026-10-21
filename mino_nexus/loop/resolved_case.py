"""Immutable Android ordinary-case admission and dispatch contract."""
from dataclasses import dataclass
import json
import math


class ContractError(ValueError):
    def __init__(self, reason_code, status='tool_blocked'):
        self.reason_code, self.status = reason_code, status
        super().__init__(reason_code)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


@dataclass(frozen=True, slots=True)
class ResolvedCase:
    run_id: str
    case_id: str
    sn: str
    session_id: str
    platform: str
    app_id: str
    target_package: str
    target_app_name: str
    expected_build: str
    account_lease_id: str
    execution_mode: str
    max_steps: int
    wall_budget_sec: float
    deadline_at: float
    case_json: str
    binding_json: str
    capabilities_json: str

    @property
    def case(self):
        # Consumers get a detached working value; the admission source cannot change.
        return json.loads(self.case_json)

    @property
    def execution_binding(self):
        return json.loads(self.binding_json)

    @property
    def capability_snapshot(self):
        return json.loads(self.capabilities_json)

    def check_points(self, step):
        bundle = (self.case.get('meta') or {}).get('step_program_keys') or {}
        plan = (bundle.get(str(step)) or {}).get('check_program_plan') or {}
        rows = [row for row in plan.get('checkpoints') or [] if isinstance(row, dict)]
        points = {}
        for index, row in enumerate(rows):
            point = str(row.get('id') or f'ck{index}').strip()
            if not point or point in points:
                raise ContractError('check_plan_invalid')
            points[point] = not bool(row.get('optional'))
        if not any(points.values()):
            raise ContractError('check_plan_missing')
        return points

    def summary(self):
        binding = self.execution_binding
        return {key: getattr(self, key) for key in (
            'run_id', 'case_id', 'sn', 'session_id', 'platform', 'app_id',
            'target_package', 'target_app_name', 'expected_build', 'account_lease_id',
            'execution_mode', 'max_steps', 'wall_budget_sec', 'deadline_at')} | {
                'schema': 'resolved_case.v1', 'binding_id': binding['binding_id'],
                'binding_revision': binding['binding_revision']}


def resolve_case(case, ctx, *, expected_build, started_at, max_steps):
    from mino_nexus.services.case_semantic_integrity import operation_diagnostics, precondition_diagnostics
    issues = operation_diagnostics(case) + precondition_diagnostics(case)
    if issues:
        raise ContractError(issues[0]['reason'])
    from mino_nexus.services.execution_binding import validate_binding
    from mino_nexus.loop.step_pointer import compute_case_wall_budget_sec
    from mino_nexus.catalog.registry import get_capability
    if str(getattr(ctx, 'platform', '') or '') != 'android':
        raise ContractError('device_platform_invalid', 'env_blocked')
    if getattr(ctx, 'execution_mode', '') != 'agent':
        raise ContractError('execution_mode_invalid')
    binding = getattr(ctx, 'execution_binding', None)
    if not binding or getattr(ctx, 'execution_contract_version', '') != 'execution_binding.v1':
        raise ContractError('binding_not_committed')
    validate_binding(binding)
    identity = {key: str(getattr(ctx, key, '') or '').strip() for key in (
        'run_id', 'sn', 'session_id', 'platform', 'app_id', 'target_package', 'target_app_name', 'execution_mode')}
    identity.update(case_id=str(case.get('case_id') or '').strip(), expected_build=str(expected_build or '').strip())
    if any(not value for key, value in identity.items() if key != 'expected_build'):
        raise ContractError('resolved_case_fields_missing')
    target = binding['target']
    if target.get('sn') != identity['sn'] or target.get('platform') != identity['platform']:
        raise ContractError('binding_target_mismatch')
    budget = float(compute_case_wall_budget_sec(case))
    if not math.isfinite(budget) or budget <= 0 or type(max_steps) is not int or max_steps < 1:
        raise ContractError('case_budget_invalid')
    deadline = started_at + budget
    fixed = float(case.get('attempt_deadline_at') or 0)
    if fixed:
        if not math.isfinite(fixed):
            raise ContractError('case_budget_invalid')
        deadline = min(deadline, fixed)
    snapshot = []
    for op in binding['operations']:
        candidate = binding['implementations'].get(op.get('selected_ref'))
        if not candidate:
            raise ContractError('binding_assembly_invalid')
        cap = get_capability(op['event_id'])
        # These framework observation events are bound read-only check operations.
        kind = 'check' if op['event_id'] in {'screenshot', 'hierarchy', 'get_foreground_app', 'probe_device_state', 'get_app_version'} else str(getattr(cap, 'kind', '') or '')
        snapshot.append({key: op[key] for key in ('operation_ref', 'event_id', 'phase')} | {
            'kind': kind, **{key: candidate[key] for key in ('executor_order', 'low_level', 'selected_impl')}})
    lease = getattr(ctx, 'resource_lease', None) or {}
    # The existing lease is identified by its owned run/account pair, not a fabricated ID.
    lease_id = str(lease.get('lease_id') or '')
    if not lease_id and lease.get('account_id') and lease.get('run_id'):
        lease_id = str(lease['run_id']) + ':' + str(lease['account_id'])
    resolved = ResolvedCase(**identity, account_lease_id=lease_id, max_steps=max_steps,
        wall_budget_sec=budget, deadline_at=deadline, case_json=_json(case),
        binding_json=_json(binding), capabilities_json=_json(snapshot))
    from mino_nexus.loop.step_pointer import build_seq_nodes
    for node in build_seq_nodes(case):
        if str(node.expected or '').strip():
            resolved.check_points(node.n)
    return resolved


def persist_contract(resolved, writer):
    from mino_nexus.services.session_atomic import append_batch
    from mino_nexus.loop.observe.session_log import SessionPersistenceError
    writer.require_persisted()
    try:
        append_batch(writer.session_id, [], meta_patch={
            'runtime_context': {'resolved_case': resolved.summary()}})
        writer.append('execution/resolved', resolved.summary())
        writer.require_persisted()
    except Exception as exc:
        writer._store_failure = 'resolved_case_persistence'
        raise SessionPersistenceError('resolved_case_persistence') from exc


def preparation_gate(ctx):
    resolved = getattr(ctx, 'resolved_case', None)
    if resolved is None:
        return
    from mino_nexus.services.resource_preflight import claim_requires_clear_cache
    from mino_nexus.runtime.session_gate import required_session
    from mino_nexus.services.login_evidence import can_commit
    case = resolved.case
    scene = getattr(ctx, 'case_scene', None) or {}
    from mino_nexus.services.case_semantic_integrity import precondition_diagnostics
    if precondition_diagnostics({**case, 'case_scene': scene}):
        raise ContractError('precondition_coverage_missing', 'env_blocked')
    need_clear = claim_requires_clear_cache(case.get('resource_key'), scene, case.get('precondition', ''))
    cursor = getattr(ctx, '_step_cursor', None)
    if cursor is not None and getattr(cursor, 'phase', '') == 'prep':
        from mino_nexus.loop.milestones import read_state
        from mino_nexus.services.step_requirements import condition_complete
        rows = read_state(cursor).get('milestones') or []
        verified_login = all(any(row.get('id') == mid and row.get('status') == 'pass' for row in rows)
                             for mid in ('submit_login', 'login_state'))
        def complete(row):
            if row.get('requirement') == 'conditional':
                return condition_complete(row)
            if row.get('status') == 'pass':
                return True
            if row.get('status') != 'skipped':
                return False
            return (row.get('id') == 'prep_clear_cache' and row.get('skip_reason') == 'not_required' and not need_clear
                    or row.get('id') == 'login_entry' and row.get('skip_reason') == 'login_form_visible' and verified_login
                    or row.get('skip_reason') == 'wait_already_ready' and row.get('hook_cap') == 'wait_screen_ready'
                    and int(getattr(cursor, 'wait_ready_passes', 0) or 0) >= 1)
        if any(row.get('status') == 'failed' for row in rows if not row.get('optional')):
            raise ContractError('environment_precondition_unmet', 'env_blocked')
        if any(not complete(row) for row in rows if not row.get('optional')):
            raise ContractError('prepare_steps_incomplete', 'env_blocked')
    lease = getattr(ctx, 'resource_lease', None) or {}
    from mino_nexus.loop.session_ensure import account_need_from_case
    need = account_need_from_case(case, scene)
    needs_account = bool(need.get('need_account')) or required_session(scene=scene) == 'logged_in'
    if needs_account and (not lease.get('account_id') or lease.get('run_id') != resolved.run_id):
        raise ContractError('account_lease_missing', 'env_blocked')
    if need_clear and not getattr(ctx, 'prep_clear_done', False):
        raise ContractError('prepare_clear_incomplete', 'env_blocked')
    if not getattr(ctx, 'app_launch_confirmed', False):
        raise ContractError('prepare_launch_unverified', 'env_blocked')
    version = str(getattr(ctx, 'app_version', '') or '')
    if resolved.expected_build and version != resolved.expected_build:
        raise ContractError('app_build_mismatch' if version else 'app_build_unverified', 'env_blocked')
    cursor = getattr(ctx, '_step_cursor', None)
    reference = getattr(cursor, 'login_submit_reference', None) or {}
    if reference.get('attempt_ref'):
        transition = getattr(cursor, 'login_transition_result', None) or {}
        if transition.get('attempt_ref') != reference['attempt_ref'] or (transition.get('result') or {}).get('status') != 'completed':
            raise ContractError('login_submission_unverified', 'unverified')
    if required_session(scene=scene) == 'logged_in' and not can_commit(ctx):
        raise ContractError('login_identity_unverified', 'env_blocked')


def dispatch_gate(ctx, event_id, *, requested_phase=''):
    resolved = getattr(ctx, 'resolved_case', None)
    if resolved is None:
        return
    import time
    if time.time() >= resolved.deadline_at:
        raise ContractError('case_budget_exhausted', 'unverified')
    if any(getattr(ctx, key, None) != getattr(resolved, key) for key in ('run_id', 'sn', 'session_id', 'platform', 'target_package', 'target_app_name', 'app_id', 'execution_mode')):
        raise ContractError('resolved_case_context_changed')
    if _json(getattr(ctx, 'execution_binding', None)) != resolved.binding_json:
        raise ContractError('resolved_binding_changed')
    cursor = getattr(ctx, '_step_cursor', None)
    phase = str(getattr(cursor, 'phase', '') or 'prep')
    # Only the finalizer can enter cleanup; dispatch kwargs cannot bypass Check.
    if getattr(ctx, 'execution_cleanup_active', False):
        phase = 'cleanup'
    elif requested_phase and requested_phase != phase:
        raise ContractError('dispatch_phase_mismatch')
    if phase == 'done':
        raise ContractError('dispatch_after_case_done')
    rows = [row for row in resolved.capability_snapshot if row['event_id'] == event_id and row['phase'] == phase]
    if not rows:
        raise ContractError('capability_outside_resolved_phase')
    if phase == 'check' and any(row['kind'] != 'check' for row in rows):
        raise ContractError('check_mutation_forbidden')
    if phase == 'do':
        preparation_gate(ctx)


def record_check(cursor, requested, result, *, writer):
    if not getattr(cursor, 'resolved_case', None):
        return
    if cursor.phase != 'check':
        raise ContractError('check_evidence_incomplete', 'unverified')
    scope = getattr(result, 'observation_scope', None) or {}
    from mino_nexus.loop.check_scope import scope_error
    scope_reason = scope_error(scope)
    if scope_reason or not scope.get('observation_ref'):
        reason = getattr(result, 'error_kind', '')
        raise ContractError(reason if reason in {'check_scope_mismatch', 'check_observation_channel_mismatch'} else scope_reason or 'check_scope_unverified', 'unverified')
    current = cursor.current()
    declared = cursor.resolved_case.check_points(current.n)
    if any(row.get('point_id') not in declared or (declared[row['point_id']] and row.get('required') is False) for row in requested):
        raise ContractError('check_plan_changed')
    required = {row['point_id'] for row in requested if row.get('required') is not False}
    rows = list(getattr(result, 'checkpoint_results', None) or [])
    ids = [row.get('point_id') for row in rows]
    if (not required or len(ids) != len(set(ids)) or not required.issubset(ids) or not set(ids).issubset({row['point_id'] for row in requested})
            or any(type(row.get('passed')) is not bool or not str(row.get('evidence') or '').strip() for row in rows)
            or getattr(result, 'error_kind', '') or getattr(result, 'parse_warnings', None)):
        raise ContractError('check_evidence_incomplete', 'unverified')
    if not all(row['passed'] for row in rows if row['point_id'] in required):
        raise ContractError('required_assertion_failed', 'product_fail')
    if getattr(result, 'passed', None) is not True:
        raise ContractError('check_evidence_incomplete', 'unverified')
    current = cursor.current()
    cursor.verified_check_points.setdefault(current.n, set()).update(required)
    writer.append('check/consumed', {'case_step': current.n, 'point_ids': sorted(required), 'status': 'pass'})


def case_pass_reason(cursor):
    if not getattr(cursor, 'resolved_case', None):
        return ''
    required_steps = [node.n for node in cursor.nodes if str(node.expected or '').strip()]
    operation_steps = {node.n for node in cursor.nodes if str(getattr(node, 'instruction', '') or '').strip()}
    if not operation_steps.issubset(getattr(cursor, 'completed_do_steps', set())):
        raise ContractError('required_operation_unverified', 'unverified')
    if (not getattr(cursor, 'preparation_verified', False) or cursor.phase != 'done' or not required_steps
            or any(not {point for point, required in cursor.resolved_case.check_points(step).items() if required}.issubset(cursor.verified_check_points.get(step, set())) for step in required_steps)):
        raise ContractError('case_checks_incomplete', 'unverified')
    return 'consumed point_id: ' + ', '.join(
        f'{step}/{point}' for step in required_steps for point in sorted(cursor.verified_check_points[step]))
