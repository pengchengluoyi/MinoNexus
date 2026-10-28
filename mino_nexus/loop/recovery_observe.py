"""仅框架截图：隔离中的 run 也允许的一次观察。"""
from __future__ import annotations

from mino_nexus.casework.resource_recovery import _snapshot
from mino_nexus.core.database import session_scope
from mino_nexus.core.stable_digest import digest, now
from mino_nexus.platform.project_guard import owned


from typing import Any


async def observe(project_id, run_id):
    """Only the framework screenshot command is allowed, including while quarantined."""
    from mino_nexus.loop.router_proxy import RouterProxy
    from mino_nexus.resources.access_scope import require_owned_device
    from mino_nexus.records.eventlog import store as session_store
    from mino_nexus.records.eventlog.atomic import append_batch
    with session_scope() as db:
        actor = owned(db, project_id, write=True)
        _, _, snapshot, _ = _snapshot(db, project_id, run_id, actor)
    snapshot_digest = digest(snapshot)
    outputs = []
    for sn in sorted({r['sn'] for r in snapshot['resources']}):
        require_owned_device(sn)
        from mino_nexus.core.ids import prefixed
        session_id = prefixed('recoveryobserve')
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
