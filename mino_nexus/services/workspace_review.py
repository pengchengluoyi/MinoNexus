"""Workspace evidence views and append-only human decisions; no execution path."""
from mino_nexus.core.database import session_scope
from mino_nexus.models.mino_bots import CollabMessage, CollabActivity
from mino_nexus.models.case import ProjectCase
from mino_nexus.services import collab_workspace as ws, run_store


def fingerprint(db, topic):
    ids = ws.scope_ids(db, topic)
    rows = db.query(ProjectCase).filter(ProjectCase.project_id == topic.project_id, ProjectCase.case_id.in_(ids)).all()
    return ws.identity({'cases': sorted((r.case_id, r.updated_at) for r in rows),
        'app_id': topic.app_id, 'surface': topic.env_surface, 'profile': topic.env_profile,
        'requirement': topic.requirement_id, 'modules': sorted(topic.modules or [])})


def capture(db, topic, run_id):
    existing = db.query(CollabActivity).filter_by(topic_id=topic.topic_id, kind='run_scope').all()
    if not any((r.refs or {}).get('run_id') == run_id for r in existing):
        ws.activity(db, topic, 'run_scope', '记录运行对应的范围版本', run_id=run_id, fingerprint=fingerprint(db, topic))


def freshness(project_id, topic_id):
    with session_scope() as db:
        topic = ws.owned(db, project_id, topic_id)
        current = fingerprint(db, topic)
        records = db.query(CollabActivity).filter_by(topic_id=topic_id, kind='run_scope').all()
        return {(r.refs or {}).get('run_id'): 'current' if (r.refs or {}).get('fingerprint') == current else 'stale' for r in records}


def overview(project_id, topic_id, sess):
    ws.authorize(project_id, sess)
    with session_scope() as db:
        ws.owned(db, project_id, topic_id)
        cards = db.query(CollabMessage).filter_by(topic_id=topic_id, card_kind='run').order_by(CollabMessage.created_at.desc()).limit(101).all()
        run_ids = list(dict.fromkeys(c.ref_id for c in cards[:100]))
        decisions = [ws.public(r) for r in db.query(CollabMessage).filter_by(topic_id=topic_id, card_kind='decision').order_by(CollabMessage.created_at.desc()).limit(100)]
    versions = freshness(project_id, topic_id)
    groups = {}
    for rid in run_ids:
        doc = run_store.get(rid)
        if not doc:
            continue
        for case in doc.get('cases') or []:
            if case.get('status') not in ('fail', 'failed', 'blocked', 'declined'):
                continue
            reason = str(case.get('fail_reason') or case.get('error') or case.get('summary') or '未记录原因')[:800]
            key = ws.identity([case.get('case_id'), reason])
            item = groups.setdefault(key, {'id': key, 'case_id': case.get('case_id'), 'name': case.get('name') or case.get('case_id'),
                'reason': reason, 'occurrences': []})
            item['occurrences'].append({'run_id': rid, 'app_id': doc.get('app_id'), 'session_id': case.get('session_id') or '',
                'status': case.get('status'), 'freshness': versions.get(rid, 'unknown')})
    from mino_nexus.core.secret_redaction import sanitize
    return sanitize({'issues': list(groups.values()), 'decisions': decisions, 'truncated': len(cards) > 100,
        'run_count': len(run_ids), 'freshness': versions})


def decide(project_id, topic_id, sess, body, run_id, request_id):
    owner = ws.authorize(project_id, sess)
    if not body.strip():
        raise ValueError('请输入决策内容')
    mid = 'decision-' + ws.identity([topic_id, owner, request_id])
    from sqlalchemy import text
    with session_scope() as db:
        db.execute(text('BEGIN IMMEDIATE'))
        topic = ws.owned(db, project_id, topic_id, write=True)
        if run_id and not db.query(CollabMessage).filter_by(topic_id=topic_id, card_kind='run', ref_id=run_id).first():
            raise ValueError('决策只能引用本工作区已有运行')
        existing = db.get(CollabMessage, mid)
        if existing:
            if existing.body != body.strip() or existing.ref_id != run_id:
                raise ValueError('请求标识已被其他决策使用')
            return ws.public(existing)
        row = CollabMessage(message_id=mid, topic_id=topic_id, author_kind='user', author_id=owner,
            message_kind='text', card_kind='decision', ref_id=run_id, origin='human_decision',
            body=body.strip(), created_at=ws.now())
        db.add(row); db.flush()
        topic.updated_at = row.created_at
        return ws.public(row)
