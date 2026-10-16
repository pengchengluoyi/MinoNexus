"""E10 event retirement and reviewable candidates. Never edits source/history or DB."""
from copy import deepcopy
import hashlib
import json

VERSION = 3
RETIRED = frozenset({'request_sms_code', 'accept_legal_consent', 'open_app', 'open_url', 'relogin', 'fsm_navigate', 'recover_fsm_navigate', 'check_run_env', 'operation.open_cart'})
EVENT_FIELDS = frozenset({'cap', 'hook_cap', 'device_cap', 'capability', 'capability_id', 'event_id', 'event_kind'})
PRESERVE = frozenset({'v5_publication', 'publications', 'session_events', 'session_meta',
                     'history', 'instruction', 'expected', 'precondition', 'author_text',
                     'source_text', 'steps_raw', 'expected_raw', 'precondition_raw',
                     'password', 'secrets', 'credentials'})
BLOCKS = {'relogin': 'fb.global.login', 'request_sms_code': 'fb.global.login.send_code',
          'accept_legal_consent': 'fb.global.login.legal_consent'}


def references(document):
    found = []
    def walk(value, path=(), depth=0):
        if depth > 40:
            raise ValueError('event_migration_document_too_deep')
        if isinstance(value, list):
            for i, item in enumerate(value):
                walk(item, (*path, i), depth + 1)
        elif isinstance(value, dict):
            for key, item in value.items():
                if key in PRESERVE:
                    continue
                if key in EVENT_FIELDS and isinstance(item, str) and item in RETIRED:
                    found.append({'path': [*path, key], 'event_id': item,
                                  'reason_code': 'business_event_migration_required'})
                elif key in {'exec_caps', 'event_ids'} and isinstance(item, list):
                    for i, cid in enumerate(item):
                        if isinstance(cid, str) and cid in RETIRED:
                            found.append({'path': [*path, key, i], 'event_id': cid,
                                          'reason_code': 'requirements_need_compile'})
                elif key == 'key_ref' and isinstance(item, str) and any(item == cid or item.endswith('.' + cid) for cid in RETIRED):
                    found.append({'path': [*path, key], 'reason_code': 'author_key_reference_requires_review'})
                else:
                    walk(item, (*path, key), depth + 1)
    walk(document)
    return found


def launch_params(params, *, former_id):
    if not isinstance(params, dict):
        raise ValueError('launch_params_invalid')
    result = deepcopy(params)
    # No implicit conversion of selectors, paths, navigation targets or credentials.
    if set(result) - {'url', 'package', 'wait_ms', 'wait_sec'}:
        raise ValueError('launch_params_require_review')
    url, package = result.get('url'), result.get('package')
    if any(x is not None and (not isinstance(x, str) or not x.strip()) for x in (url, package)):
        raise ValueError('launch_target_invalid')
    for key in ('wait_ms', 'wait_sec'):
        if key in result and (type(result[key]) not in (int, float) or not 0 <= result[key] <= 300000):
            raise ValueError('launch_wait_invalid')
    if 'wait_ms' in result and 'wait_sec' in result:
        raise ValueError('launch_wait_conflict')
    if not (url or package):
        raise ValueError('launch_target_required')
    if url and package and url != package:
        raise ValueError('launch_target_conflict')
    if former_id == 'open_url' and (url or package):
        from urllib.parse import urlsplit
        parsed = urlsplit(str(url or package))
        if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('launch_url_invalid')
        result['url'] = str(url or package)
        result.pop('package', None)
    if former_id == 'open_app' and url:
        raise ValueError('launch_native_target_requires_review')
    return result


def migration_candidate(document, *, context='plan'):
    """New candidate only; recovery business actions require explicit block redesign."""
    if context not in {'plan', 'block', 'recovery'}:
        raise ValueError('event_migration_context_invalid')
    original = deepcopy(document)
    candidate = deepcopy(document)
    changes, blockers = [], []
    def walk(value, path=()):
        if isinstance(value, list):
            for i, item in enumerate(value):
                walk(item, (*path, i))
        elif isinstance(value, dict):
            retired = {v for k, v in value.items() if k in EVENT_FIELDS and isinstance(v, str) and v in RETIRED}
            if retired:
                if len(retired) != 1:
                    blockers.append({'path': list(path), 'reason_code': 'mixed_business_event_requires_review'})
                    return
                cid = next(iter(retired))
                try:
                    if cid in {'fsm_navigate', 'recover_fsm_navigate'}:
                        params = value.get('params') or {}
                        targets = {str(params[k]).strip() for k in ('to_state', 'expected_state', 'goal_state', 'to') if params.get(k)}
                        if context == 'recovery' or len(targets) != 1 or set(params) - {'to_state', 'expected_state', 'goal_state', 'to'}:
                            raise ValueError('navigation_policy_requires_review')
                        for key in EVENT_FIELDS | {'exec_caps', 'event_ids', 'params'}:
                            value.pop(key, None)
                        value.update(kind='nav', nav_target=targets.pop(), tier='policy')
                    elif cid in {'check_run_env', 'operation.open_cart'}:
                        raise ValueError('retired_business_reference_requires_replacement')
                    elif cid in BLOCKS:
                        if context == 'recovery' or value.get('params'):
                            raise ValueError('business_recovery_or_params_require_review')
                        if value.get('block_id') and value['block_id'] != BLOCKS[cid]:
                            raise ValueError('business_block_reference_conflict')
                        if any(k in EVENT_FIELDS and v != cid for k, v in value.items()):
                            raise ValueError('mixed_event_fields_require_review')
                        # Recompile the new block reference; never reuse old binding/publication.
                        for key in EVENT_FIELDS | {'exec_caps', 'event_ids'}:
                            value.pop(key, None)
                        value.update(kind='flow_block', block_id=BLOCKS[cid])
                    else:
                        params = launch_params(value.get('params') or {}, former_id=cid)
                        for key in EVENT_FIELDS:
                            if value.get(key) == cid:
                                value[key] = 'launch_app'
                        value['params'] = params
                    changes.append({'path': list(path), 'former_event_id': cid})
                except ValueError as exc:
                    blockers.append({'path': list(path), 'reason_code': str(exc)})
            for key, item in list(value.items()):
                if key not in PRESERVE and key not in EVENT_FIELDS:
                    walk(item, (*path, key))
    references(candidate)  # Depth/shape guard before recursive transformation.
    walk(candidate)
    def clear_caches(value):
        if isinstance(value, list):
            for item in value:
                clear_caches(item)
        elif isinstance(value, dict):
            for cache in ('binding_steps', 'binding_source_revision', 'binding_event_contract', 'block_dependencies'):
                value.pop(cache, None)
            for key, item in value.items():
                if key not in PRESERVE:
                    clear_caches(item)
    if changes:
        clear_caches(candidate)
    blockers.extend(references(candidate))
    revision = hashlib.sha256(json.dumps(original, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    return {'schema': 'business-event-migration.v1', 'source_revision': revision,
            'status': 'blocked' if blockers else 'candidate' if changes else 'unchanged',
            'requires_recompile': bool(changes), 'requires_publication_review': bool(changes),
            'changes': changes, 'blockers': blockers, 'candidate': candidate}


def tier_for(event_id, *, recovery_rule=False):
    if recovery_rule or event_id.startswith('guard.') or event_id in {'fsm_navigate', 'recover_fsm_navigate'}:
        return 'policy'
    if event_id in BLOCKS:
        return 'block'
    if event_id in {'get_otp', 'get_phone', 'lease_account', 'release_account', 'confirm_login_state'}:
        return 'tool'
    return 'event'
