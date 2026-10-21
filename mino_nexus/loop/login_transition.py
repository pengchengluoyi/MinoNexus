"""One post-submit page comparison; no identity probe or evidence navigation."""
from copy import deepcopy
import hashlib
import json
import re

def scope(ctx):
    from mino_nexus.services.login_evidence import scope as evidence_scope
    return evidence_scope(ctx)


AUTH_FIELDS = frozenset({
    'login_evidence', 'session_fact', 'task_session', 'device_session',
    'device_session_source', 'login_flow_macro_active', 'login_flow_macro_done',
    'login_auto_submit_attempts',
})


def update_auth_state(ctx, **changes):
    """The Auth state writer. Copy mutable values; never log identity or secrets."""
    unknown = set(changes) - AUTH_FIELDS
    if unknown:
        raise ValueError(f'Undeclared Auth fields: {sorted(unknown)}')
    values = {key: deepcopy(value) for key, value in changes.items()}
    from mino_nexus.loop.observe.session_log import active_writer
    writer = active_writer()
    if writer:
        writer.append('login/evidence', {
            'source': 'auth_state_writer', 'changed_fields': sorted(values),
        })
    for key, value in values.items():
        setattr(ctx, key, value)


def _nodes(rows):
    return [deepcopy(row) for row in (rows or []) if isinstance(row, dict)
            and (row.get('text') or row.get('content_desc') or row.get('editable')
                 or row.get('clickable') or row.get('resource_id'))]


def _page_structure(rows):
    """Ignore input values, countdown digits, focus and keyboard geometry."""
    return sorted((str(row.get('resource_id') or ''),
                   str(row.get('class') or row.get('class_name') or row.get('role') or ''),
                   bool(row.get('editable')),
                   '' if row.get('editable') else re.sub(r'\d+', '#', str(row.get('text') or row.get('content_desc') or '')).strip())
                  for row in rows)


def _dom_available(rows, contract):
    return bool(rows) and not (contract.get('missing') or contract.get('truncated'))


def remember_submit_frame(cursor, ctx, shot, *, writer=None):
    """Keep only the current observation in memory; bind it after a real submit."""
    cursor.login_submit_frame_candidate = {
        'context_id': id(ctx), 'scope': scope(ctx),
        'turn': int(getattr(writer, 'turn', 0) or 0),
        'nodes': _nodes(getattr(ctx, 'nav_hierarchy_nodes', None)),
        'contract': deepcopy(getattr(ctx, 'observation_contract', None) or {}),
        'image_base64': str(getattr(shot, 'image_base64', '') or ''),
        'image_mime': str(getattr(shot, 'image_mime', '') or 'image/png'),
    }


def bind_submit_frame(cursor, ctx, *, attempt_ref, writer=None):
    # Invalidate the prior account fact when binding the actual submitted action.
    # The later comparison must not overwrite a separate identity observation.
    from mino_nexus.loop.observe.session_persist import mark_session_dirty
    mark_session_dirty(ctx, reason="login_submission_transition")
    candidate = getattr(cursor, 'login_submit_frame_candidate', None) or {}
    if (not attempt_ref or candidate.get('context_id') != id(ctx)
            or candidate.get('scope') != scope(ctx)
            or candidate.get('turn') != int(getattr(writer, 'turn', 0) or 0)):
        cursor.login_submit_reference = None
        return False
    cursor.login_submit_reference = {**deepcopy(candidate), 'attempt_ref': attempt_ref}
    return True


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def confirm_submission_transition(proxy, ctx, cursor, *, shot=None, writer=None):
    """DOM first, screenshot fallback. Each submitted attempt is compared once."""
    ref = getattr(cursor, 'login_submit_reference', None) or {}
    fail = {'ok': False, 'status': 'unverified', 'error_kind': 'login_transition_observation_error'}
    if (ref.get('context_id') != id(ctx) or ref.get('scope') != scope(ctx)
            or not ref.get('attempt_ref')):
        return _finish(cursor, ctx, ref, {**fail, 'summary': '缺少本次实际提交前的登录界面，无法比较；已停止确认'}, writer=writer)
    cached = getattr(cursor, 'login_transition_result', None) or {}
    if cached.get('attempt_ref') == ref['attempt_ref']:
        return dict(cached['result'])
    if writer is not None and int(getattr(writer, 'turn', 0) or 0) <= ref['turn']:
        return _finish(cursor, ctx, ref, {**fail, 'summary': '提交后尚无独立的新观察，已停止确认'}, writer=writer)
    current = getattr(cursor, 'login_submit_frame_candidate', None) or {}
    fresh = (current.get('context_id') == id(ctx) and current.get('scope') == scope(ctx)
             and current.get('turn', 0) > ref.get('turn', 0))
    nodes = current.get('nodes', []) if fresh else []
    contract = current.get('contract', {}) if fresh else {}
    # DOM is read only once if this turn has no usable hierarchy already.
    if not _dom_available(nodes, contract):
        from mino_nexus.loop.hierarchy_slots import capture
        snap = capture(proxy, turn_id=int(getattr(writer, 'turn', 0) or 0))
        nodes = _nodes(getattr(snap, 'nodes', None)) if getattr(snap, 'ok', False) else []
        contract = dict(getattr(snap, 'observation', None) or {}) if getattr(snap, 'ok', False) else {'missing': ['capture_failed']}
    before = ref.get('nodes', [])
    if _dom_available(before, ref.get('contract', {})) and _dom_available(nodes, contract):
        from mino_nexus.loop.channel_observation import login_credential_form_open
        changed = (_page_structure(before) != _page_structure(nodes)
                   and not login_credential_form_open(nodes))
        method = 'dom'
        before_hash = _hash(json.dumps(_page_structure(before), ensure_ascii=False))
        after_hash = _hash(json.dumps(_page_structure(nodes), ensure_ascii=False))
        result = {'ok': changed}
    else:
        image = str(getattr(shot, 'image_base64', '') or '')
        if not image and fresh:
            image = current.get('image_base64', '')
        if not image:
            shot = proxy.observe('screenshot', force_fresh=True)
            image = str(getattr(shot, 'image_base64', '') or '')
        if not image or not ref.get('image_base64'):
            result = {**fail, 'summary': 'DOM 不可用且缺少本次提交前后截图，已停止确认'}
            return _finish(cursor, ctx, ref, result, writer=writer)
        from mino_nexus.loop.vision_assert import vision_assert_checkpoints
        comparison = vision_assert_checkpoints(
            expectation='当前界面是否已经离开参考图中的本次登录提交界面？只比较是否切换页面，不判断真实登录态或账号身份。仅输入变化、键盘收起、倒计时、Toast、加载动画或错误提示不算切换；仍保留同一登录表单则不通过。',
            image_base64=image,
            image_mime=str(getattr(shot, 'image_mime', '') or current.get('image_mime') or 'image/png'),
            reference_image_base64=ref['image_base64'], reference_image_mime=ref['image_mime'],
            run_context=ctx)
        if comparison.error_kind or comparison.parse_warnings:
            result = {**fail, 'summary': '本次登录提交前后画面比较失败，已停止确认'}
            return _finish(cursor, ctx, ref, result, writer=writer)
        result = {'ok': bool(comparison.passed)}
        method, before_hash, after_hash = 'screenshot', _hash(ref['image_base64']), _hash(image)
    if result['ok']:
        # A page transition completes the submission observation only.
        # Authentication and identity must come from a separate current observation.
        result.update(status='completed', error_kind='',
                      summary='登录提交后页面已切换；账号身份及业务预期尚未核验')
    else:
        result.update(status='product_fail', error_kind='login_page_unchanged',
                      summary='提交登录后仍是原登录界面，登录未完成')
    return _finish(cursor, ctx, ref, result, writer=writer, method=method,
                   before_ref=before_hash, after_ref=after_hash)


def _finish(cursor, ctx, ref, result, *, writer=None, method='', before_ref='', after_ref=''):
    cursor.login_transition_result = {'attempt_ref': ref.get('attempt_ref', ''), 'result': dict(result)}
    if writer:
        writer.append('login/submission_transition', {'attempt_ref': ref.get('attempt_ref', ''),
            'comparison_source': method, 'passed': result['ok'], 'status': result['status'],
            'error_kind': result['error_kind'], 'before_ref': before_ref, 'after_ref': after_ref,
            'business_context_verified': False})
    return result
