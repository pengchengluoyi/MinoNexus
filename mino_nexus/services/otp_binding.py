"""Fixed Gmail child contract for get_otp; never a model-selectable plugin API."""
from contextvars import ContextVar
import math
import time

GMAIL_EVENT = 'plugin.gmail.fetch_otp'
GMAIL_EXECUTOR = 'host_plugin'
GMAIL_IMPL = 'program.v1:plugin.gmail.fetch_otp'
OTP_CHILD = ContextVar('bound_otp_child', default=None)


def implementation():
    return {'id': GMAIL_IMPL, 'executor': GMAIL_EXECUTOR, 'runs_on': 'scout',
            'low_level': {}, 'params_contract': {'version': 1},
            'result_contract': {'version': 1}, 'device_hint_template': {'execution_mode': 'legacy'}}


def request_params(ctx):
    from mino_nexus.loop.local_executors import _otp_context, _lease_email_for_otp
    _, _, otp = _otp_context(ctx)
    recipient = _lease_email_for_otp(ctx)
    if not recipient or '@' not in recipient or any(c.isspace() for c in recipient):
        raise ValueError('otp_recipient_missing')
    since = float(getattr(ctx, 'otp_sent_at', 0) or 0)
    deadline = float(getattr(ctx, 'case_deadline_ts', 0) or 0)
    now = time.time()
    if not math.isfinite(since) or since <= 0 or since > now:
        raise ValueError('otp_send_window_missing')
    if not math.isfinite(deadline) or deadline <= now:
        raise ValueError('otp_deadline_expired')
    wait = min(90_000, int(otp.get('max_wait_ms') or 90_000), int((deadline - now) * 1000))
    if wait <= 0:
        raise ValueError('otp_deadline_expired')
    return {'to_address': recipient, 'since_ts': since, 'deadline_ts': deadline,
            'from_allowlist': list(otp.get('from_allowlist') or []),
            'subject_contains': str(otp.get('subject_contains') or ''),
            'poll_interval_ms': max(500, min(10_000, int(otp.get('poll_interval_ms') or 3000))),
            'max_wait_ms': wait}
