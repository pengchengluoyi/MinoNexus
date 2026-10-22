"""Existing roles as workspace members; no independent execution state."""
from mino_nexus.platform.llm.repo import role_specs, get_job
from mino_nexus.core.database import session_scope
from mino_nexus.models.mino_bots import CollabTopicMember
from mino_nexus.models.settings import Settings
from mino_nexus.assistant.workspace import collab_workspace as ws

DEFAULT_KEY = 'workspace-default-members'
DEFAULT_MEMBERS = ['conductor', 'req-qa-bm', 'version-qa-bm', 'test-engineer', 'product-expert']


def role_directory():
    rows = []
    for r in role_specs():
        jobs = list(r.get('chat_jobs') or [])
        available = bool(jobs) and r.get('called') != 'sandbox'
        reason = '' if available else ('尚未接入业务输入，仅供观察' if r.get('called') == 'sandbox' else
            '设备执行角色，由执行流程调用' if r.get('group') == 'runtime' else '尚未开放群聊任务')
        rows.append({'id': r['id'], 'label': r.get('label', r['id']),
                     'sender_name': '管家' if r['id'] == 'conductor' else r.get('label', r['id']),
                     'summary': r.get('summary', ''), 'chat_jobs': jobs,
                     'capabilities': [{'id': j, 'label': (get_job(j) or {}).get('label', j)} for j in jobs],
                     'fixed': r['id'] == 'conductor', 'available': available, 'unavailable_reason': reason})
    return rows


def catalog():
    return {r['id']: r for r in role_directory() if r['available']}


def mentions(content):
    """Parse explicit @names/ids, not natural-language task intent."""
    import re
    aliases = {alias: r for r in catalog().values() for alias in (r['id'], r['label'], r['sender_name'])}
    names = '|'.join(re.escape(a) for a in sorted(aliases, key=len, reverse=True))
    pattern = re.compile(r'(?<![A-Za-z0-9_.+\-])@(' + names + r')(?=$|[\s，。！？,:：、;；])')
    roles = list({aliases[m.group(1)]['id']: aliases[m.group(1)] for m in pattern.finditer(content)}.values())
    return roles, pattern.sub('', content).strip()


def sender_name(message):
    if message.get('author_kind') == 'user':
        return '你'
    rid = message.get('role') or 'conductor'
    if rid == 'conductor':
        return '管家'
    return next((r.get('label') or rid for r in role_specs() if r['id'] == rid), rid)


def defaults(db):
    row = db.get(Settings, DEFAULT_KEY)
    return list((row.payload or {}).get('members', DEFAULT_MEMBERS)) if row else DEFAULT_MEMBERS


def default_settings(members=None):
    with session_scope() as db:
        if members is not None:
            if 'conductor' not in members or set(members) - set(catalog()):
                raise ValueError('默认成员必须包含管家，且只能选择支持聊天的角色')
            db.merge(Settings(id=DEFAULT_KEY, payload={'members': list(dict.fromkeys(members))}))
            return {'members': list(dict.fromkeys(members))}
        return {'members': defaults(db)}


def add_defaults(db, topic_id):
    for rid in dict.fromkeys(['conductor', *defaults(db)]):
        if rid not in catalog():
            raise ValueError('默认成员配置包含不支持聊天的角色：' + rid)
        if not db.get(CollabTopicMember, (topic_id, 'role', rid)):
            db.add(CollabTopicMember(topic_id=topic_id, member_kind='role', member_id=rid, joined_at=ws.now()))


def list_members(project_id, topic_id, sess):
    ws.authorize(project_id, sess)
    with session_scope() as db:
        ws.owned(db, project_id, topic_id)
        ids = [r.member_id for r in db.query(CollabTopicMember).filter_by(topic_id=topic_id, member_kind='role')]
    roles = catalog()
    # Older conversations need no migration; the conductor is always their receiver.
    return [roles[r] for r in dict.fromkeys(['conductor', *ids]) if r in roles]


def change_member(project_id, topic_id, sess, member, remove=False):
    ws.authorize(project_id, sess)
    if member not in catalog():
        raise ValueError('该角色没有可分配的聊天任务，不能加入工作区')
    if remove and member == 'conductor':
        raise ValueError('管家不可移出')
    with session_scope() as db:
        t = ws.owned(db, project_id, topic_id, write=True)
        row = db.get(CollabTopicMember, (topic_id, 'role', member))
        if remove and row:
            db.delete(row)
        elif not remove and not row:
            db.add(CollabTopicMember(topic_id=topic_id, member_kind='role', member_id=member, joined_at=ws.now()))
        t.updated_at = ws.now()
    return list_members(project_id, topic_id, sess)


def validate_assignment(project_id, topic_id, sess, member, job):
    roles = {r['id']: r for r in list_members(project_id, topic_id, sess)}
    if member not in roles:
        raise ValueError('该角色不在本群，请先在成员页拉入：' + str(member))
    if job not in roles[member]['chat_jobs']:
        raise ValueError('该成员不能执行此聊天任务：' + str(job))
    spec = get_job(job)
    if not spec or not spec.get('enabled') or (spec.get('engine') not in ('text_chat', 'chat_reply', 'rule') and not (member == 'report-writer' and job == 'draft_test_report' and spec.get('engine') == 'json_chat')):
        raise ValueError('此任务不可从聊天执行')
    return spec


def restore_defaults(project_id, topic_id, sess):
    from sqlalchemy import text
    ws.authorize(project_id, sess)
    with session_scope() as db:
        db.execute(text('BEGIN IMMEDIATE'))
        topic = ws.owned(db, project_id, topic_id, write=True)
        add_defaults(db, topic_id)
        topic.updated_at = ws.now()
    return list_members(project_id, topic_id, sess)
