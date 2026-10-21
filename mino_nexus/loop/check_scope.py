"""Bind business assertions to the observed page and channel."""
import hashlib
import json


def scope_context(cursor, context):
    current = cursor.current()
    case = getattr(cursor, 'case', None) or getattr(context, 'case', None) or {}
    bundles = (case.get('meta') or {}).get('step_program_keys') or {}
    targets = []
    for node in cursor.nodes:
        if node.n > current.n:
            break
        for row in (bundles.get(str(node.n), {}).get('do_program_plan') or {}).get('steps') or []:
            if row.get('nav_target'):
                targets.append({'case_step': node.n, 'target': row['nav_target']})
    return {'case_step': current.n, 'application': getattr(context, 'target_app_name', ''),
            'expected_channel': 'app', 'navigation_targets': targets[-3:],
            'instruction': current.instruction, 'expectation': current.expected,
            'prior_operations': [{'case_step': node.n, 'instruction': node.instruction}
                                 for node in cursor.nodes if node.n < current.n]}


def scope_error(scope):
    if not isinstance(scope, dict) or type(scope.get('matched')) is not bool or not str(scope.get('evidence') or '').strip():
        return 'check_scope_unverified'
    if scope.get('observed_channel') != 'app':
        return 'check_observation_channel_mismatch'
    if not scope['matched']:
        return 'check_scope_mismatch'
    return ''


def verify_dom_scope(cursor, context, shot, *, provider_id=''):
    from mino_nexus.ai.schemas import AssertResult
    from mino_nexus.loop.vision_assert import vision_assert_checkpoints
    image = str(getattr(shot, 'image_base64', '') or '')
    if not image:
        return AssertResult(passed=False, ai_reasoning='缺少当前页面观察，不能把结构中的未命中判为产品失败。',
                            error_kind='check_scope_unverified')
    result = vision_assert_checkpoints(expectation=current_scope_expectation(cursor),
        image_base64=image, image_mime=str(getattr(shot, 'image_mime', '') or 'image/png'),
        context_block=json.dumps({'assertion_scope': scope_context(cursor, context)}, ensure_ascii=False),
        provider_id=provider_id or None, run_context=context, require_scope=True,
        checkpoints=[{'point_id': 'observation_scope', 'required': True,
                      'checkpoint_kind': 'screen_reached', 'title': current_scope_expectation(cursor)}])
    if not result.error_kind and not result.passed:
        result.error_kind = 'check_scope_mismatch'
    return result


def current_scope_expectation(cursor):
    return '确认当前观察属于本步骤要求的页面、数据通道及同一业务实体；本次只检查作用域，不判断业务文案是否符合预期。'


def bind_scope(scope, image):
    return {**scope, 'observation_ref': hashlib.sha256(image.encode()).hexdigest()}
