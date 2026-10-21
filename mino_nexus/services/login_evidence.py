"""Positive session evidence; credentials and disappearing forms are only hints."""

from mino_nexus.loop.login_transition import update_auth_state
from copy import deepcopy
import hashlib
import time
import math

EVIDENCE_MAX_AGE_SEC = 90

AUTH_KINDS = frozenset({'account_profile', 'logout_control', 'authenticated_content'})
UNAUTH_KINDS = frozenset({'login_form', 'login_entry', 'explicit_guest'})


def normalize_observation(row):
    result = deepcopy(row) if isinstance(row, dict) else {}
    proof = result.get('authentication_evidence') or {}
    valid = (result.get('ok') is True and isinstance(proof, dict)
             and proof.get('kind') in AUTH_KINDS and isinstance(proof.get('observed'), str) and bool(proof['observed'].strip())
             and bool(result.get('observation_ref')))
    if result.get('session') == 'logged_in' and not valid:
        result.update(session='unknown', identity='unknown', reason='缺少当前观察的已登录正向证据')
    unauth = result.get('unauthentication_evidence') or {}
    if result.get('session') == 'logged_out' and not (
            result.get('ok') is True and isinstance(unauth, dict)
            and unauth.get('kind') in UNAUTH_KINDS
            and isinstance(unauth.get('observed'), str) and unauth['observed'].strip()
            and result.get('observation_ref')):
        result.update(session='unknown', identity='unknown', reason='缺少当前观察的未登录正向证据')
    if result.get('identity') == 'match' and (not isinstance(result.get('identity_evidence'), str) or not result['identity_evidence'].strip()):
        result['identity'] = 'unknown'
    if result.get('session') != 'logged_in':
        result['identity'] = 'unknown'
    return result


def observation_ref(image):
    return hashlib.sha256(str(image or '').encode()).hexdigest() if image else ''


def scope(ctx):
    lease = getattr(ctx, 'resource_lease', None) or {}
    return {'run_id': str(getattr(ctx, 'run_id', '') or ''),
            'sn': str(getattr(ctx, 'sn', '') or ''),
            'case_scope': str(getattr(ctx, 'case_session_scope_id', '') or ''),
            'package': str(getattr(ctx, 'target_package', '') or ''),
            'account_id': str(lease.get('account_id') or '')}


def record(ctx, row, *, form_left=None):
    row = normalize_observation(row)
    # Android requires the leased identity or an explicitly configured observed fact.
    identity_required = str(getattr(ctx, 'platform', '')).lower() == 'android'
    evidence = {'version': 1, 'scope': scope(ctx), 'observation_ref': row.get('observation_ref', ''),
        'observed_at_monotonic': row.get('observed_at_monotonic', time.monotonic()),
        'form_left': form_left, 'authentication': row.get('session', 'unknown'),
        'identity': row.get('identity', 'unknown'), 'identity_required': identity_required,
        'business_context': 'unverified', 'business_context_owner': 'check',
        'full_login_complete': False}
    from mino_nexus.services.login_evidence_policy import submission_current
    submitted = (evidence['identity'] != 'mismatch' and submission_current(
        ctx, observed_at=evidence['observed_at_monotonic']))
    from mino_nexus.services.login_evidence_policy import policy_for_context, submitted_account_enabled
    policy = policy_for_context(ctx)
    proof = row.get('authentication_evidence') or {}
    configured_fact = (submitted and submitted_account_enabled(ctx)
        and bool(policy.get('authenticated_content_hint'))
        and proof.get('kind') == 'authenticated_content'
        and bool(proof.get('observed')) and bool(scope(ctx)['account_id']))
    if identity_required and configured_fact and evidence['identity'] == 'unknown':
        evidence['identity'] = 'configured_fact'
    identity_match = evidence['identity'] == 'match' and bool(scope(ctx)['account_id'])
    evidence['account_binding_source'] = ('ui_identity' if identity_match
        else 'configured_fact' if configured_fact else 'submitted_account' if submitted and not identity_required else 'none')
    evidence['login_state_verified'] = (evidence['authentication'] == 'logged_in'
        and evidence['identity'] != 'mismatch'
        and (not identity_required or identity_match or configured_fact))
    update_auth_state(ctx, login_evidence=evidence)
    from mino_nexus.loop.observe.session_log import active_writer
    writer = active_writer()
    if writer:
        # No account identifiers, credential values, model text or screenshot body.
        writer.append('login/evidence', {k: v for k, v in evidence.items() if k not in {'scope', 'observed_at_monotonic'}})
    return row, evidence


def evidence_current(ctx, evidence):
    if not isinstance(evidence, dict):
        return False
    if str(getattr(ctx, 'platform', '')).lower() == 'android':
        if (not scope(ctx)['account_id'] or evidence.get('identity') not in {'match', 'configured_fact'}
                or evidence.get('identity_required') is not True):
            return False
    at = evidence.get('observed_at_monotonic')
    age = time.monotonic() - at if isinstance(at, (int, float)) and not isinstance(at, bool) else float('inf')
    deadline = getattr(ctx, 'case_deadline_ts', 0) or 0
    if evidence.get('verification_method') == 'login_page_transition':
        return False
    return (evidence.get('version') == 1 and evidence.get('scope') == scope(ctx)
            and math.isfinite(age) and 0 <= age <= EVIDENCE_MAX_AGE_SEC
            and (not deadline or time.time() < deadline)
            and bool(evidence.get('observation_ref'))
            and evidence.get('authentication') == 'logged_in'
            and evidence.get('identity') != 'mismatch'
            and (not evidence.get('identity_required') or evidence.get('identity') == 'match'
                 or evidence.get('identity') == 'configured_fact'
                 or (not evidence.get('identity_required') and evidence.get('account_binding_source') == 'submitted_account'
                     and _submission_current(ctx, evidence)))
            and evidence.get('login_state_verified') is True)


def can_commit(ctx):
    return (evidence_current(ctx, getattr(ctx, 'login_evidence', None))
            and not bool(getattr(ctx, 'session_dirty', False)))


def _submission_current(ctx, evidence):
    from mino_nexus.services.login_evidence_policy import submission_current
    return submission_current(ctx, observed_at=evidence.get('observed_at_monotonic'))
