"""Validate deterministic persisted checkpoint facts without trusting model conclusions."""
from mino_nexus.models.session_event import SessionEvent
from mino_nexus.core.stable_digest import digest
from mino_nexus.contract.web_v5_contract import evaluate


def checkpoint(db, session, point_id, publication_digest):
    context = session.runtime_context or {}
    publication = context.get('publication') or {}
    if (not session.finished_at or not session.finalization_id or session.status != 'pass'
            or (session.verdict or {}).get('verification_status') != 'verified_pass'
            or context.get('execution_mode') != 'v5' or context.get('executor_stopped') is not True
            or digest(publication) != publication_digest):
        raise ValueError('fact_verified_session_required')
    points = [p for p in (publication.get('check_program_plan') or {}).get('checkpoints', [])
              if p.get('point_id') == point_id and p.get('required') is True]
    if len(points) != 1:
        raise ValueError('fact_frozen_checkpoint_missing')
    point = points[0]
    if point_id not in (session.verdict or {}).get('consumed_required', []):
        raise ValueError('fact_checkpoint_not_consumed')
    refs = (session.verdict or {}).get('event_refs') or []
    referenced = []
    for ref in refs:
        if ref.get('session_id') != session.session_id or type(ref.get('seq')) is not int:
            raise ValueError('fact_reference_invalid')
        event = db.query(SessionEvent).filter_by(session_id=session.session_id, seq=ref['seq']).one_or_none()
        if event is None:
            raise ValueError('fact_reference_missing')
        referenced.append(event)
    bundles = [e for e in referenced if e.type == 'verification/evidence']
    verdicts = [e for e in referenced if e.type == 'verification/verdict']
    if len(bundles) != 1 or len(verdicts) != 1 or verdicts[0].payload.get('verification_status') != 'verified_pass':
        raise ValueError('fact_final_evidence_missing')
    bundle = bundles[0]
    checks = [p for p in (bundle.payload or {}).get('checks', []) if p.get('point_id') == point_id]
    if len(checks) != 1 or checks[0].get('truth') != 'true' or checks[0].get('required') is not True:
        raise ValueError('fact_checkpoint_not_satisfied')
    check = checks[0]
    action = check.get('action_ref') or {}
    if action.get('run_id') != session.session_id or type(action.get('step_idx')) is not int:
        raise ValueError('fact_action_identity_missing')
    control = (publication.get('controls') or {}).get(point['control_id']) or {}
    for read in reversed(check.get('reads') or []):
        if read.get('fresh') is not True:
            continue
        ref = read.get('event_ref') or {}
        if ref.get('session_id') != session.session_id or type(ref.get('seq')) is not int:
            continue
        event = db.query(SessionEvent).filter_by(session_id=session.session_id, seq=ref['seq']).one_or_none()
        if event is None or event.type != 'device/observation' or event.seq >= bundle.seq:
            continue
        payload = event.payload or {}
        raw = payload.get('raw_response') or {}
        facts, observation = raw.get('target_facts') or {}, raw.get('observation') or {}
        scout = raw.get('scout') or payload.get('scout') or {}
        identity = scout.get('execution_identity') or {}
        if (payload.get('capability_id') != 'read_web_target_facts' or payload.get('status') != 'pass'
                or observation != read.get('observation') or facts != read.get('facts')
                or observation.get('run_id') != session.session_id
                or not context.get('session_generation')
                or observation.get('session_generation') != context['session_generation']
                or type(observation.get('after_step_idx')) is not int
                or observation['after_step_idx'] < action['step_idx']
                or facts.get('control_id') != point['control_id']
                or facts.get('control_revision') != control.get('revision')
                or not scout.get('node_id') or scout.get('sn') != context.get('sn')
                or scout.get('run_id') != session.session_id
                or identity.get('schema_version') != 1
                or identity.get('node_id') != scout['node_id']
                or not identity.get('node_connection_generation')
                or identity.get('node_connection_generation') != scout.get('node_connection_generation')
                or not identity.get('scout_process_generation')
                or identity.get('scout_process_generation') != scout.get('scout_process_generation')
                or evaluate(point['expected'], facts).get('truth') != 'true'):
            continue
        later = db.query(SessionEvent).filter(SessionEvent.session_id == session.session_id,
            SessionEvent.seq > event.seq, SessionEvent.type == 'device/action').all()
        if any((e.payload or {}).get('capability_id') != 'close_web_v5' for e in later):
            raise ValueError('fact_observation_superseded')
        return {'session_id': session.session_id, 'seq': event.seq, 'point_id': point_id,
            'observed_at': event.ts, 'finalization_id': session.finalization_id,
            'publication_digest': publication_digest, 'control_id': point['control_id']}
    raise ValueError('fact_fresh_observation_required')
