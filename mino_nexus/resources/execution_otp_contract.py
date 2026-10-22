"""Metadata-only admission for the finite same-node Gmail OTP child contract."""
from mino_nexus.resources.execution_dependencies import fact, plugin_fact

GMAIL_CAP = 'plugin.gmail.fetch_otp'


def otp_facts(otp, *, node, uid, events, executors):
    mode = str(otp.get('mode') or 'auto').strip().lower()
    fixed = mode in {'fixed', 'auto'} and bool(str(otp.get('fixed') or '').strip())
    unavailable = fact('needs_input', 'otp_source_requires_bound_contract')
    if fixed:
        return {'resource:otp_source': fact('ready', 'otp_fixed_configured'),
                'resource:otp_gmail': fact('needs_input', 'otp_gmail_not_selected')}
    if mode not in {'gmail', 'auto'}:
        return {'resource:otp_source': unavailable, 'resource:otp_gmail': unavailable}
    plugin = plugin_fact([node] if node is not None else [],
        {'kind': 'plugin', 'class': 'mail', 'plugin_id': 'gmail',
         'node_id': node.node_id if node else 'unavailable'}, uid)
    if plugin['status'] != 'ready':
        return {'resource:otp_source': plugin, 'resource:otp_gmail': plugin}
    cap = events.get(GMAIL_CAP) or {}
    implementations = cap.get('implementations') or []
    supported = (cap.get('enabled') is True and cap.get('lifecycle') == 'active'
        and cap.get('ui_coverable') is not False and cap.get('caller') == 'program'
        and {'prep', 'do'} <= set(cap.get('phases') or [])
        and any(row.get('id') == 'host_plugin' and row.get('available') is True
                and GMAIL_CAP in (row.get('provides') or []) for row in executors)
        and any(impl.get('executor') == 'host_plugin' and impl.get('runs_on') == 'scout'
                and bool(impl.get('id')) and impl.get('low_level') == {}
                and impl.get('params_contract') == {'version': 1}
                and impl.get('result_contract') == {'version': 1}
                and isinstance(impl.get('device_hint_template'), dict) for impl in implementations))
    result = plugin if supported else fact('blocked', 'otp_plugin_contract_unavailable')
    return {'resource:otp_source': result, 'resource:otp_gmail': result}
