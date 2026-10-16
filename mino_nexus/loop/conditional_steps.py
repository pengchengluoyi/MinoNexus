"""Bounded, read-only applicability gate. No absence inference from an empty/partial tree."""
from mino_nexus.services.step_requirements import contract, condition_complete


def classify(nodes, observation):
    from mino_nexus.loop.ui_consent import find_consent_control
    from mino_nexus.loop.hierarchy_slots import node_flag
    snapshot = observation.get('snapshot_id')
    if not snapshot or not nodes:
        return 'unknown', 'observation_missing'
    if observation.get('truncated') is not False or observation.get('missing') != []:
        return 'unknown', 'condition_scope_incomplete'
    ctrl = find_consent_control(nodes, require_unique=True)
    if ctrl is not None:
        checkable = [n for n in nodes if node_flag(n, 'checkable') or n.get('role') in {'checkbox', 'switch'}]
        if len(checkable) != 1 or ctrl not in checkable:
            return 'unknown', 'condition_target_ambiguous'
        if ctrl.get('snapshot_id') and ctrl['snapshot_id'] != snapshot:
            return 'unknown', 'observation_stale'
        if ctrl.get('checked') is None or not node_flag(ctrl, 'checked_known'):
            return 'unknown', 'checked_state_unknown'
        return ('satisfied', 'observed_checked_state') if node_flag(ctrl, 'checked') else ('required', 'observed_unchecked_state')
    # This fact is collected over a complete, visible semantic form, independently
    # of the compact viewport node list. It does not cover custom/native renderers.
    scope = observation.get('form_controls') or {}
    if (observation.get('truncated') is False and observation.get('missing') == []
            and scope.get('version') == 1 and scope.get('snapshot_id') == snapshot
            and scope.get('complete') is True and scope.get('credential_forms') == 1
            and scope.get('checkable_count') == 0):
        return 'not_applicable', 'complete_login_form_scope'
    return 'unknown', 'condition_scope_incomplete'


def gate(row, state, cursor, ctx, proxy, *, turn_seq, writer=None):
    if row.get('requirement') != 'conditional' or condition_complete(row):
        return None
    from mino_nexus.loop.milestones import write_state
    from mino_nexus.loop.channel_observation import bind_fresh_observation
    try:
        requirement = contract(row)
    except ValueError:
        requirement = None
    def finish(status, reason, *, done=False):
        if status == 'blocked':
            row.update(status='blocked', error_kind=reason, optional=False)
        write_state(cursor, state)
        if writer:
            writer.append('requirement/condition', {'milestone_id': row['id'], 'requirement': 'conditional',
                'resolution': row.get('condition_resolution', 'unknown'), 'status': status, 'reason_code': reason,
                'observation_count': row.get('condition_reads', 0), 'proof': row.get('condition_proof') or {}})
        return {'ok': done, 'defer': status == 'defer', 'case_fail': status == 'blocked',
                'status': status, 'milestone_id': row['id'], 'capability_id': row.get('hook_cap', ''),
                'error_kind': reason if status == 'blocked' else '', 'summary': reason}
    if requirement is None:
        return finish('blocked', 'conditional_requirement_invalid')
    limit = requirement['condition']['max_observations']
    if row.get('condition_last_turn') == turn_seq:
        return finish('defer', 'conditional_observation_pending')
    if int(row.get('condition_reads') or 0) >= limit:
        return finish('blocked', 'applicability_unknown')
    row['condition_last_turn'] = turn_seq
    row['condition_reads'] = int(row.get('condition_reads') or 0) + 1
    # Clear the previous observation first; capture failure cannot reuse it.
    ctx.nav_hierarchy_nodes = []
    ctx.observation_contract = {}
    try:
        bind_fresh_observation(ctx, proxy)
    except Exception:
        pass
    observation = dict(getattr(ctx, 'observation_contract', None) or {})
    resolution, reason = classify(list(getattr(ctx, 'nav_hierarchy_nodes', None) or []), observation)
    from mino_nexus.loop.hierarchy_slots import node_flag
    from mino_nexus.loop.ui_consent import find_consent_control
    observed_nodes = list(getattr(ctx, 'nav_hierarchy_nodes', None) or [])
    checkable_nodes = [n for n in observed_nodes if node_flag(n, 'checkable') or n.get('role') in {'checkbox', 'switch'}]
    target = find_consent_control(observed_nodes, require_unique=True)
    condition_observation = {
        'node_count': len(observed_nodes), 'checkable_count': len(checkable_nodes),
        'unique_checkable_bounds': len({tuple(n.get('bounds') or []) for n in checkable_nodes}),
        'target_found': target is not None,
        'target_checkable': bool(target and node_flag(target, 'checkable')),
        'target_checked_known': bool(target and node_flag(target, 'checked_known')),
        'truncated': observation.get('truncated'), 'missing': observation.get('missing'),
        'form_controls': observation.get('form_controls') or {},
    }
    visual_proof = {}
    # 原生自绘控件可能没有 checkable 语义。只对完整观察中唯一显式候选看图，
    # 正向证明存在与实际状态；截图缺失/不可见仍 unknown，绝不证明不适用。
    if (resolution == 'unknown' and target is not None and observation.get('snapshot_id')
            and observation.get('truncated') is False and observation.get('missing') == []
            and str(getattr(ctx, 'platform', '') or '').lower() in {'android', 'web'}):
        from mino_nexus.loop.checkbox_observation import observe_checkbox
        visual_proof = observe_checkbox(ctx, proxy, target, consent=True)
        if visual_proof.get('verified'):
            resolution = 'satisfied' if visual_proof['checked'] else 'required'
            reason = 'visual_checked_state' if visual_proof['checked'] else 'visual_unchecked_state'
    scope = observation.get('login_scope') or {}
    if (resolution == 'unknown' and str(getattr(ctx, 'platform', '')).lower() == 'web'
            and observation.get('truncated') is False and observation.get('missing') == []
            and scope.get('version') == 1 and scope.get('snapshot_id') == observation.get('snapshot_id')
            and scope.get('complete') is True and scope.get('candidate_count') == 1
            and scope.get('checkable_count') == 0):
        from mino_nexus.loop.checkbox_observation import observe_checkbox
        visual_proof = observe_checkbox(ctx, proxy, scope, login_scope=True)
        if visual_proof.get('verified') and visual_proof.get('scope_absent'):
            resolution, reason = 'not_applicable', 'complete_visual_login_scope'
    condition_observation['login_scope'] = scope
    row['condition_resolution'] = resolution
    row['optional'] = False
    row['condition_proof'] = {'version': 1, 'snapshot_id': observation.get('snapshot_id', ''),
                              'verified_by': reason, 'turn_seq': turn_seq, 'observation': condition_observation,
                              **({'visual_observation': visual_proof} if visual_proof.get('verified') else {})}
    if resolution == 'not_applicable':
        row.update(status='skipped', skip_reason='not_applicable', skipped_by='program_condition', evidence='')
        return finish('skipped', reason, done=True)
    if resolution == 'satisfied':
        row.update(status='pass', evidence=reason)
        return finish('pass', reason, done=True)
    if resolution == 'required':
        row['status'] = 'in_progress'
        # Applicability is known. The regular bound dispatch must enforce all
        # current availability/caller/phase rules; no binding is created here.
        write_state(cursor, state)
        if writer:
            writer.append('requirement/condition', {'milestone_id': row['id'], 'resolution': 'required',
                'proof': row['condition_proof'], 'status': 'pending_action'})
        return None
    return finish('blocked' if row['condition_reads'] >= limit else 'defer', 'applicability_unknown')
