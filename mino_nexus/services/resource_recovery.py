"""Explicit, evidence-bound human recovery. Stopping alone never releases resources."""
from copy import deepcopy
from datetime import datetime
import time
from sqlalchemy import text
from mino_nexus.core.database import session_scope
from mino_nexus.models.execution_resource import ExecutionResource, ResourceRecovery
from mino_nexus.models.run import AppRegressionRun
from mino_nexus.models.pool_account import PoolAccount
from mino_nexus.models.session_event import SessionMeta, SessionEvent
from mino_nexus.services.case_compilation import owned, digest, now
from mino_nexus.services import run_store

OBSERVATIONS = {'observe/screen', 'observe/hierarchy', 'device/observation', 'tool/result'}


def timestamp(value):
    try:
        return datetime.fromisoformat(str(value)).astimezone().timestamp()
    except (ValueError, TypeError, OverflowError):
        raise ValueError('recovery_evidence_time_invalid') from None


def _snapshot(db, project_id, run_id, actor):
    from mino_nexus.services.run_write_safety import unresolved
    from mino_nexus.models.project import App
    run = db.get(AppRegressionRun, run_id)
    if run is None or run.owner_user_id != actor:
        raise ValueError('recovery_task_missing')
    app = db.get(App, run.app_id)
    if app is None or app.project_id != project_id:
        raise ValueError('recovery_project_mismatch')
    doc = deepcopy(run.payload or {})
    if run.status in {'running', 'queued'} or not run.finished_at:
        raise ValueError('recovery_task_running')
    rows = db.query(ExecutionResource).filter_by(project_id=project_id, run_id=run_id,
        owner_user_id=actor).filter(ExecutionResource.state != 'released').order_by(ExecutionResource.resource_key).all()
    if not rows:
        raise ValueError('recovery_resources_missing')
    actions = unresolved(doc)
    if not actions:
        raise ValueError('recovery_stop_evidence_required')
    for action in actions:
        recovery = action.get('recovery') or {}
        proof = recovery.get('proof') or {}
        if (action.get('stop_confirmed') is not True or proof.get('contract') != 'write_stop.v1'
                or proof.get('stop_confirmed') is not True or proof.get('action_ref') != action.get('action_ref')
                or proof.get('sn') != action.get('sn') or recovery.get('reviewed_by') != actor
                or not recovery.get('at')):
            raise ValueError('recovery_stop_evidence_required')
    sns = {row.sn for row in rows}
    if any(action.get('sn') not in sns for action in actions):
        raise ValueError('recovery_resource_scope_incomplete')
    aliases = {run_id} | {str((a.get('action_ref') or {}).get('run_id') or '') for a in actions}
    accounts = db.query(PoolAccount).filter_by(project_id=project_id).all()
    leases = {a.id: deepcopy(a.lease) for a in accounts if (a.lease or {}).get('run_id') in aliases}
    resource_accounts = {str((row.detail or {}).get('account_id') or '') for row in rows}
    if set(leases) - resource_accounts:
        raise ValueError('recovery_resource_scope_incomplete')
    resources = [{key: deepcopy(getattr(row, key)) for key in ('resource_key', 'attempt_id', 'execution_unit_id',
        'sn', 'epoch', 'state', 'expires_at', 'detail')} for row in rows]
    snapshot = {'run_id': run_id, 'app_id': run.app_id, 'env': doc.get('env_profile') or 'test',
        'platform': run.platform, 'resources': resources, 'actions': actions, 'account_leases': leases,
        'finished_at': run.finished_at}
    return run, rows, snapshot, aliases


def _account_ids(db, session):
    context = session.runtime_context or {}
    ids = set((context.get('account_bindings') or {}).keys())
    from mino_nexus.models.account_fact_candidate import AccountFactCandidate
    ids.update(row.account_id for row in db.query(AccountFactCandidate).filter_by(
        session_id=session.session_id, owner_user_id=session.owner_user_id).all())
    return ids


def _evidence(db, session, event, snapshot, resource, actor):
    if session is None or session.owner_user_id != actor or session.app_id != snapshot['app_id']:
        raise ValueError('recovery_evidence_scope_mismatch')
    context = session.runtime_context or {}
    env = context.get('env_profile') or ((context.get('execution_binding') or {}).get('target') or {}).get('env_profile')
    if (session.run_id == snapshot['run_id'] or not session.finished_at or env != snapshot['env']
            or event is None or event.session_id != session.session_id or event.type not in OBSERVATIONS):
        raise ValueError('recovery_observation_required')
    after = max([timestamp(snapshot['finished_at'])] + [timestamp(a['recovery']['at']) for a in snapshot['actions']])
    if timestamp(event.ts) <= after or timestamp(session.finished_at) < timestamp(event.ts):
        raise ValueError('recovery_evidence_not_fresh')
    if time.time() - timestamp(event.ts) > 1800:
        raise ValueError('recovery_evidence_expired')
    payload = event.payload or {}
    if event.type in {'device/observation', 'tool/result'} and payload.get('status') not in {'pass', 'success'}:
        raise ValueError('recovery_observation_failed')
    raw = payload.get('raw_response') or {}
    capability = str(payload.get('capability_id') or '')
    if event.type in {'device/observation', 'tool/result'} and capability not in {'screenshot', 'get_hierarchy', 'read_web_target_facts', 'get_dom'}:
        raise ValueError('recovery_read_observation_required')
    content = (raw.get('target_facts') or raw.get('hierarchy') or raw.get('nodes')
        or raw.get('image_base64') or payload.get('thumb') or payload.get('thumb_ref')
        or payload.get('hierarchy') or payload.get('nodes') or payload.get('hierarchy_text'))
    if not content:
        raise ValueError('recovery_observation_empty')
    sn = context.get('sn') or (raw.get('scout') or payload.get('scout') or {}).get('sn')
    platform = context.get('platform') or (raw.get('scout') or payload.get('scout') or {}).get('platform')
    if resource['resource_key'].startswith('device:') and platform and platform != snapshot['platform']:
        raise ValueError('recovery_platform_mismatch')
    if resource['resource_key'].startswith('device:') and sn != resource['sn']:
        raise ValueError('recovery_device_mismatch')
    account_id = (resource.get('detail') or {}).get('account_id')
    capture = context.get('recovery_observation') or {}
    captured_scope = (capture.get('source_run_id') == snapshot['run_id']
        and capture.get('snapshot_digest') == digest(snapshot)
        and resource['resource_key'] in (capture.get('resource_keys') or []))
    if resource['resource_key'].startswith('account:') and account_id not in _account_ids(db, session) and not captured_scope:
        raise ValueError('recovery_account_identity_missing')
    # An observation followed by another action is not the resource's final state.
    later = db.query(SessionEvent).filter(SessionEvent.session_id == session.session_id,
        SessionEvent.seq > event.seq, SessionEvent.type.in_(['device/action', 'tool/call'])).all()
    for row in later:
        cap = str((row.payload or {}).get('capability_id') or '')
        if cap not in {'close_web_v5', 'screenshot', 'read_web_target_facts'}:
            raise ValueError('recovery_observation_superseded')
    return {'session_id': session.session_id, 'seq': event.seq, 'type': event.type,
            'finalization_id': session.finalization_id or '', 'observed_at': event.ts, 'evidence_digest': digest(payload)}


def preview(project_id, run_id):
    with session_scope() as db:
        actor = owned(db, project_id, write=True)
        _, _, snapshot, _ = _snapshot(db, project_id, run_id, actor)
        sessions = db.query(SessionMeta).filter_by(app_id=snapshot['app_id'], owner_user_id=actor).filter(
            SessionMeta.finished_at.isnot(None)).order_by(SessionMeta.started_at.desc()).limit(200).all()
        options = []
        for resource in snapshot['resources']:
            observations = []
            for session in sessions:
                events = db.query(SessionEvent).filter_by(session_id=session.session_id).filter(
                    SessionEvent.type.in_(OBSERVATIONS)).order_by(SessionEvent.seq.desc()).limit(50).all()
                for event in events:
                    try:
                        observations.append(_evidence(db, session, event, snapshot, resource, actor))
                    except ValueError:
                        continue
            options.append({'resource_key': resource['resource_key'], 'epoch': resource['epoch'],
                'observations': observations[:100]})
        return {'run_id': run_id, 'expected_digest': digest(snapshot), 'resources': options,
                'verification_kind': 'human_confirmed', 'evidence_valid_for_seconds': 1800}


def recover(project_id, run_id, body):
    from mino_nexus.services.control_registry import _assert_runtime_writable
    from mino_nexus.models.resource_ops import DeviceResourceLease
    from mino_nexus.models.device_reservation import DeviceReservation
    from mino_nexus.models.account_fact_candidate import AccountFactCandidate
    _assert_runtime_writable()
    # The task lock and SQLite transaction cover all old claims and their replacement epochs.
    with run_store._LOCK, session_scope() as db:
        db.execute(text('BEGIN IMMEDIATE'))
        actor = owned(db, project_id, write=True)
        recovery_id = 'recovery-' + digest([actor, project_id, run_id, body.request_id])
        request_digest = digest(body.model_dump())
        existing = db.get(ResourceRecovery, recovery_id)
        if existing:
            if existing.request_digest != request_digest:
                raise ValueError('recovery_request_conflict')
            return deepcopy(existing.detail['result'])
        run, rows, snapshot, aliases = _snapshot(db, project_id, run_id, actor)
        if digest(snapshot) != body.expected_digest:
            raise ValueError('recovery_snapshot_changed')
        proofs = {p.resource_key: p for p in body.proofs}
        if len(proofs) != len(body.proofs) or set(proofs) != {r.resource_key for r in rows}:
            raise ValueError('recovery_proof_coverage_incomplete')
        verified = []
        for resource in snapshot['resources']:
            proof = proofs[resource['resource_key']]
            session = db.get(SessionMeta, proof.session_id)
            event = db.query(SessionEvent).filter_by(session_id=proof.session_id, seq=proof.seq).one_or_none()
            ref = _evidence(db, session, event, snapshot, resource, actor)
            verified.append({'resource_key': resource['resource_key'], 'evidence': ref,
                'statement': proof.statement, 'old_epoch': resource['epoch'], 'new_epoch': resource['epoch'] + 1})
        at = now()
        for row in rows:
            row.epoch += 1
            row.state, row.expires_at = 'released', 0
            row.detail = {**(row.detail or {}), 'last_recovery_id': recovery_id}
        for account_id, expected in snapshot['account_leases'].items():
            account = db.get(PoolAccount, account_id)
            if account.lease != expected:
                raise ValueError('recovery_account_lease_changed')
            account.lease = {}
        account_ids = {str((r.detail or {}).get('account_id') or '') for r in rows} - {''}
        for fact in db.query(AccountFactCandidate).filter(AccountFactCandidate.project_id == project_id,
                AccountFactCandidate.account_id.in_(account_ids), AccountFactCandidate.status == 'confirmed').all():
            fact.decision = {**(fact.decision or {}), 'expires_at': 0, 'invalidated_by_recovery': recovery_id}
        sns = {r.sn for r in rows}
        for lease in db.query(DeviceResourceLease).filter(DeviceResourceLease.run_id.in_(aliases), DeviceResourceLease.sn.in_(sns)).all():
            db.delete(lease)
        for reservation in db.query(DeviceReservation).filter_by(run_id=run_id).all():
            db.delete(reservation)
        doc = deepcopy(run.payload)
        for key, action in (doc.get('write_safety') or {}).items():
            if action.get('state') in {'pending', 'isolated'}:
                doc['write_safety'][key] = {**action, 'state': 'recovered', 'reason_code': '',
                    'recovery': {**action['recovery'], 'resource_reset_verified': True,
                        'verification_kind': 'human_confirmed', 'recovery_id': recovery_id}}
        result = {'recovery_id': recovery_id, 'run_id': run_id, 'state': 'recovered',
            'verification_kind': 'human_confirmed', 'resources': verified, 'at': at}
        doc['resource_recoveries'] = [*(doc.get('resource_recoveries') or []), recovery_id]
        run.payload = doc
        db.add(ResourceRecovery(recovery_id=recovery_id, project_id=project_id, owner_user_id=actor,
            run_id=run_id, request_digest=request_digest,
            detail={'reason': body.reason, 'before': snapshot, 'result': result}, created_at=at))
        db.flush()
        # Never mutate verdicts or replay the original write. Future runs acquire new epochs.
        db.commit()
        run_store._LIVE.pop(run_id, None)
        return result


async def observe(project_id, run_id):
    """Only the framework screenshot command is allowed, including while quarantined."""
    import uuid
    from mino_nexus.loop.router_proxy import RouterProxy
    from mino_nexus.services.access_scope import require_owned_device
    from mino_nexus.services import session_store
    from mino_nexus.services.session_atomic import append_batch
    with session_scope() as db:
        actor = owned(db, project_id, write=True)
        _, _, snapshot, _ = _snapshot(db, project_id, run_id, actor)
    snapshot_digest = digest(snapshot)
    outputs = []
    for sn in sorted({r['sn'] for r in snapshot['resources']}):
        require_owned_device(sn)
        session_id = 'recovery-observe-' + uuid.uuid4().hex
        session_store.open_meta(session_id=session_id, run_id=session_id, case_id='', app_id=snapshot['app_id'])
        context = {'project_id': project_id, 'env_profile': snapshot['env'], 'platform': snapshot['platform'], 'sn': sn,
            'recovery_observation': {'source_run_id': run_id, 'snapshot_digest': snapshot_digest,
                'resource_keys': [r['resource_key'] for r in snapshot['resources'] if r['sn'] == sn],
                'verification_kind': 'human_review_required'}}
        append_batch(session_id, [], {'runtime_context': context})
        try:
            shot = await RouterProxy(sn, run_id=session_id).observe_async('screenshot', timeout_sec=8)
            if not shot.has_image():
                raise ValueError('recovery_screenshot_unavailable')
            # Revalidate the original quarantine after the read; do not consume a changed snapshot.
            with session_scope() as db:
                _, _, current, _ = _snapshot(db, project_id, run_id, actor)
                if digest(current) != snapshot_digest:
                    raise ValueError('recovery_snapshot_changed')
            refs = append_batch(session_id, [{'type': 'observe/screen', 'payload': {
                'thumb': 'data:' + (shot.image_mime or 'image/png') + ';base64,' + shot.image_base64,
                'width': shot.width, 'height': shot.height, 'source': 'scout_recovery_observation'}}],
                {'status': 'done', 'finished_at': now(), 'summary': '复位后的只读观察，业务状态待人工核对'})
            outputs.append({'sn': sn, **refs[0]})
        except Exception:
            append_batch(session_id, [], {'status': 'fail', 'finished_at': now(), 'summary': '复位观察未取得可用截图'})
            raise
    return {'observations': outputs, 'verification_kind': 'human_review_required'}
