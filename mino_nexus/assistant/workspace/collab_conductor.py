"""Accept a workspace message immediately; the durable queue hands it to workspace_agent."""
from mino_nexus.core.database import session_scope
from mino_nexus.models.collab_message_job import CollabMessageJob
from mino_nexus.assistant.workspace import collab_workspace as ws


def enqueue(project_id, topic_id, sess, content, thread_id='', *, card='', ref=''):
    owner = ws.authorize(project_id, sess)
    content = str(content or '').strip()
    if not content or len(content) > 8000:
        raise ValueError('请填写消息，最长 8000 字符')
    with session_scope() as db:
        t = ws.owned(db, project_id, topic_id, write=True)
        if t.title == '新对话':
            t.title = ' '.join(content.split())[:60]
        m = ws.message(db, t, content, kind='text', card=card, ref=ref, author='user', owner=owner)
        db.flush()
        db.add(CollabMessageJob(message_id=m.message_id, project_id=project_id, topic_id=topic_id,
            owner_user_id=owner, status='queued', created_at=ws.now(), updated_at=ws.now()))
        from mino_nexus.assistant.workspace.workspace_members import sender_name
        result = {**ws.public(m), 'reply_status': 'queued'}
        result['sender_name'] = sender_name(result)
    from mino_nexus.assistant.workspace.collab_message_queue import wake
    wake()
    return result


def process(project_id, topic_id, sess, content, *, source_message_id):
    from mino_nexus.assistant.workspace.workspace_agent import process as run
    run(project_id, topic_id, sess, content, source_message_id=source_message_id)
