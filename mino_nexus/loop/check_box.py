"""Generic checked-state operation. No login, consent text, or app-specific lookup."""
import time


def execute(event, *, ctx, router, t0):
    from mino_nexus.records.eventlog.writer import active_writer
    from mino_nexus.loop.checkbox_observation import observe_checkbox
    from mino_nexus.core.protocol import EventStatus
    from mino_nexus.loop.local_executors import _result, _dispatch_device, _exec_ok, _hierarchy_nodes
    from mino_nexus.navigation.geometry.hierarchy_slots import node_flag
    from mino_nexus.loop.channel_observation import bind_fresh_observation
    from mino_nexus.loop.recovery import invalidate_evidence_cache
    params = dict(event.params or {})
    wanted = params.get('checked')
    target = params.get('target') or {}
    if not isinstance(target, dict):
        target = {}
    ref = str(params.get('node_ref') or target.get('node_ref') or '')
    resource = str(target.get('resource_id') or '')
    visual_target_verified = False
    native_shape = target.get('bounds')
    native_snapshot = str(params.get('snapshot_id') or '')

    def result(status, summary, raw=None):
        return _result(event, status=status, summary=summary, error='' if status == EventStatus.PASS else summary,
                       executor='internal', elapsed_ms=int((time.time() - t0) * 1000), raw_response=raw)

    def select(*, previous=None):
        nodes = _hierarchy_nodes(ctx)
        if previous is not None and not resource:
            # node_ref 仅对原采集有效。动作后只能在新完整观察中唯一重定位，
            # 不复用旧引用，不按 checked 状态筛选目标以免挑中另一个已勾选框。
            contract = getattr(ctx, 'observation_contract', None) or {}
            if not contract or contract.get('missing') or contract.get('truncated'):
                return None
            keys = ('role', 'class', 'text', 'content_desc', 'bounds', 'frame_path')
            if not previous.get('bounds') or not (node_flag(previous, 'checkable') or visual_target_verified):
                return None
            hits = [n for n in nodes if (node_flag(n, 'checkable') or visual_target_verified)
                    and all(n.get(k) == previous.get(k) for k in keys)]
        elif not ref and not resource and native_shape:
            contract = getattr(ctx, 'observation_contract', None) or {}
            if (contract.get('snapshot_id') != native_snapshot or contract.get('truncated') is not False
                    or contract.get('missing') != []):
                return None
            keys = ('bounds', 'class', 'text', 'content_desc')
            hits = [n for n in nodes if all(n.get(k) == target.get(k) for k in keys)]
        else:
            hits = [n for n in nodes if (previous is not None or not ref or n.get('node_ref') == ref) and
                    (not resource or n.get('resource_id') == resource)]
        return hits[0] if len(hits) == 1 else None

    def state_known(node):
        return node is not None and node_flag(node, 'checkable') and node.get('checked') is not None and (
            'checked_known' not in node or node_flag(node, 'checked_known'))

    if type(wanted) is not bool or not (ref or resource or native_shape and native_snapshot) or router is None:
        return result(EventStatus.BLOCKED, 'checkbox_explicit_target_and_state_required')
    node = select()
    visual_before = {}
    if not state_known(node):
        if node is None or str(getattr(ctx, 'platform', '') or '').lower() not in {'android', 'web'}:
            return result(EventStatus.BLOCKED, 'checkbox_state_unavailable')
        from mino_nexus.loop.checkbox_observation import observe_checkbox
        visual_before = observe_checkbox(ctx, router, node)
        if not visual_before.get('verified'):
            return result(EventStatus.BLOCKED, 'checkbox_state_unavailable')
        visual_target_verified = True
    before_checked = visual_before['checked'] if visual_target_verified else node_flag(node, 'checked')
    if before_checked == wanted:
        observed_state = visual_before if visual_target_verified else {
            'verified': True, 'checked': before_checked,
            'snapshot_id': str((getattr(ctx, 'observation_contract', None) or {}).get('snapshot_id') or ''),
            'verified_by': 'structural_checked_state'}
        return result(EventStatus.PASS, 'checkbox_already_in_requested_state',
                      {'checked_observation': observed_state,
                       'execution': {'execution_started': 'no', 'phase': 'resolve', 'reason_code': 'state_already_satisfied'}})
    if ('enabled' in node and not node_flag(node, 'enabled')) or ('visible' in node and not node_flag(node, 'visible')):
        return result(EventStatus.BLOCKED, 'checkbox_target_not_actionable')
    from mino_nexus.navigation.geometry.ui_consent import tap_params_for_control
    tap_params = tap_params_for_control(node, _hierarchy_nodes(ctx))
    if visual_target_verified and not resource and not ref:
        width, height = visual_before['viewport_wh']
        left, top, right, bottom = node['bounds']
        tap_params = {'x': round((left + right) * 500 / width), 'y': round((top + bottom) * 500 / height),
                      'point_policy': 'coordinate'}
        from mino_nexus.action_space.scheme import use_visual_fallback
        from mino_nexus.records.eventlog.writer import active_writer
        # 已用当前截图正向核对这个精确原生控件，坐标动作必须同步切换观察策略。
        # 否则 device_execute_params 会把 point_policy 改回 node，拒绝实际有效的视觉目标。
        use_visual_fallback(ctx, reason='native_checkbox_positive_visual_state', writer=active_writer())
    if resource:
        tap_params['target'] = {'resource_id': resource}
    if ref:
        tap_params.update(node_ref=ref, snapshot_id=node.get('snapshot_id', ''))
    tap = _dispatch_device(router, ctx=ctx, seq=event.seq, cap='tap_element',
                           params=tap_params, label='设置复选框状态')
    if not _exec_ok(tap):
        return result(tap.status, str(tap.error or tap.summary), tap.raw_response)
    # Exactly one write. A missing post-state never causes a second toggle.
    try:
        invalidate_evidence_cache(ctx)
        # Never accept a stale pre-action node as post-action evidence.
        ctx.nav_hierarchy_nodes = []
        bind_fresh_observation(ctx, router)
    except Exception:
        return result(EventStatus.FAIL, 'checkbox_post_state_unavailable', tap.raw_response)
    observed = select(previous=node)
    visual_after = {}
    if visual_target_verified and observed is not None:
        from mino_nexus.loop.checkbox_observation import observe_checkbox
        visual_after = observe_checkbox(ctx, router, observed)
    after_known = state_known(observed) or visual_after.get('verified') is True
    after_checked = visual_after['checked'] if visual_after.get('verified') else (node_flag(observed, 'checked') if after_known else None)
    from mino_nexus.records.eventlog.writer import active_writer
    writer = active_writer()
    if writer:
        writer.append('checkbox/observation', {
            'target_resolution': 'resource_id' if resource else 'fresh_unique_structure',
            'target_resolved': observed is not None,
            'before_snapshot_id': str(node.get('snapshot_id') or '')[:120],
            'after_snapshot_id': str((observed or {}).get('snapshot_id') or '')[:120],
            'before_checked': before_checked,
            'after_checked': after_checked,
            'wanted_checked': wanted, 'write_attempts': 1,
        })
    if not after_known:
        return result(EventStatus.BLOCKED, 'checkbox_post_state_unavailable', tap.raw_response)
    if after_checked != wanted:
        return result(EventStatus.FAIL, 'checkbox_post_state_unverified', tap.raw_response)
    observed_state = visual_after if visual_after.get('verified') else {
        'verified': True, 'checked': after_checked,
        'snapshot_id': str((getattr(ctx, 'observation_contract', None) or {}).get('snapshot_id') or ''),
        'verified_by': 'structural_checked_state'}
    return result(EventStatus.PASS, 'checkbox_state_verified',
                  {**(tap.raw_response if isinstance(tap.raw_response, dict) else {}),
                   'checked_observation': observed_state})
