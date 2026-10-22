"""One post-submit page comparison; no identity probe or evidence navigation."""
from copy import deepcopy
import hashlib
import json
import re

def scope(ctx):
    from mino_nexus.navigation.login.evidence import scope as evidence_scope
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
    from mino_nexus.records.eventlog.writer import active_writer
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
    from mino_nexus.navigation.login.state import mark_session_dirty
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
