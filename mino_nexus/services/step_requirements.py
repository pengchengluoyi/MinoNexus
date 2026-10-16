"""Frozen step requirements. Unsupported conditions fail compilation, never become optional."""
from copy import deepcopy

VERSION = 1


def contract(step):
    kind = step.get('requirement') or ('optional' if step.get('optional') else 'required')
    if kind not in {'required', 'conditional', 'optional'}:
        raise ValueError('step_requirement_invalid')
    condition = step.get('condition')
    if kind == 'conditional':
        if (step.get('optional') or not isinstance(condition, dict)
                or condition.get('kind') != 'login_consent'
                or set(condition) - {'kind', 'max_observations'}
                or type(condition.get('max_observations')) is not int
                or not 1 <= condition['max_observations'] <= 3
                or step.get('hook_cap') != 'operation.check_box'):
            raise ValueError('conditional_requirement_invalid')
        return {'requirement': kind, 'condition': deepcopy(condition)}
    if condition or kind == 'required' and step.get('optional'):
        raise ValueError('step_requirement_conflict')
    return {'requirement': kind}


def condition_complete(row):
    if row.get('requirement') != 'conditional':
        return False
    proof = row.get('condition_proof') or {}
    if not isinstance(proof, dict) or not proof.get('snapshot_id') or proof.get('version') != VERSION:
        return False
    resolution = row.get('condition_resolution')
    if resolution == 'not_applicable':
        return (row.get('status') == 'skipped' and row.get('skip_reason') == 'not_applicable'
                and (proof.get('verified_by') == 'complete_login_form_scope' or _visual_absence_complete(proof)))
    if resolution == 'satisfied' and row.get('status') == 'pass' and proof.get('verified_by') == 'visual_checked_state':
        visual = proof.get('visual_observation') or {}
        return (visual.get('verified') is True and visual.get('checked') is True
                and visual.get('snapshot_id') == proof['snapshot_id'] and bool(visual.get('image_hash'))
                and visual.get('verified_by') == 'inspect-checkbox-state')
    if resolution == 'satisfied' and row.get('status') == 'pass' and proof.get('verified_by') == 'checkbox_post_state':
        observed = proof.get('post_observation')
        return _checked_post_observation(observed) and observed['snapshot_id'] == proof['snapshot_id']
    return (resolution == 'satisfied' and row.get('status') == 'pass'
            and proof.get('verified_by') == 'observed_checked_state')


def _checked_post_observation(observed):
    if (not isinstance(observed, dict) or observed.get('verified') is not True
            or observed.get('checked') is not True or not observed.get('snapshot_id')):
        return False
    method = observed.get('verified_by')
    if method == 'structural_checked_state':
        return True
    return (method == 'inspect-checkbox-state' and bool(observed.get('image_hash'))
            and isinstance(observed.get('bounds'), list) and len(observed['bounds']) == 4)


def checkbox_post_proof(previous, observed):
    """把独立动作后观察投影到条件准出；缺证据不生成满足结论。"""
    if (not isinstance(previous, dict) or previous.get('version') != VERSION
            or not previous.get('snapshot_id') or not _checked_post_observation(observed)):
        return None
    proof = deepcopy(previous)
    proof['applicability_snapshot_id'] = previous.get('applicability_snapshot_id') or previous['snapshot_id']
    before_visual = proof.pop('visual_observation', None)
    if before_visual:
        proof['applicability_visual_observation'] = before_visual
    proof['snapshot_id'] = observed['snapshot_id']
    proof['verified_by'] = 'checkbox_post_state'
    proof['post_observation'] = {key: deepcopy(observed[key]) for key in (
        'verified', 'checked', 'snapshot_id', 'verified_by', 'image_hash', 'bounds', 'viewport_wh'
    ) if key in observed}
    if observed.get('verified_by') == 'inspect-checkbox-state':
        proof['visual_observation'] = deepcopy(proof['post_observation'])
    return proof


def _visual_absence_complete(proof):
    visual = proof.get('visual_observation') or {}
    scope = (proof.get('observation') or {}).get('login_scope') or {}
    return (proof.get('verified_by') == 'complete_visual_login_scope'
            and scope.get('version') == 1 and scope.get('complete') is True
            and scope.get('candidate_count') == 1 and scope.get('checkable_count') == 0
            and scope.get('snapshot_id') == proof['snapshot_id']
            and visual.get('snapshot_id') == proof['snapshot_id']
            and visual.get('verified') is True and visual.get('scope_absent') is True
            and visual.get('verified_by') == 'inspect-login-consent-scope' and bool(visual.get('image_hash')))
