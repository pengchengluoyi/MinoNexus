"""V7 scope workspaces. All changes use project authorization and DB transactions."""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone

from sqlalchemy import text, or_
from mino_nexus.core.database import session_scope
from mino_nexus.models.mino_bots import CollabTopic, CollabMessage, CollabActivity
from mino_nexus.models.collab_read import CollabWorkspaceRead
from mino_nexus.models.case import ProjectCase



class SimpleTopic:
    """Read-only attribute view over a public() dict."""
    def __init__(self, data):
        self.__dict__.update(data)


def now():
    return datetime.now(timezone.utc).isoformat()


def uid():
    return str(uuid.uuid4())


def identity(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def public(row):
    return {c.name: getattr(row, c.name) for c in row.__table__.columns
            if not (isinstance(row, CollabTopic) and c.name == 'expected_build')}


def authorize(project_id, sess):
    from mino_nexus.resources.project_members import require_maintainer
    require_maintainer(project_id, sess)
    owner = str((sess or {}).get("user_id") or "").strip()
    if not owner:
        raise ValueError('缺少执行账号')
    return owner


def owned(db, project_id, topic_id, *, write=False):
    from mino_nexus.platform.identity import require_user_id
    owner = require_user_id()
    row = db.get(CollabTopic, topic_id)
    if row is None or row.project_id != project_id or row.lead_user_id != owner:
        raise KeyError('工作区不存在')
    if write and (row.archived or row.workspace_kind.startswith('legacy')):
        raise ValueError('已归档工作区只读，请先恢复有效范围工作区')
    return row


def activity(db, topic, kind, body, **refs):
    db.add(CollabActivity(activity_id=uid(), topic_id=topic.topic_id, kind=kind,
                         body=body, refs=refs, created_at=now()))
    topic.updated_at = now()


def message(db, topic, body='', *, kind='system', card='', ref='', author='bot', owner='', origin='', role='', thread_id=''):
    # Reference cards update in place; a heartbeat is not a new chat message.
    if card in ('resources', 'run', 'workspace', 'run_finished', 'work'):
        query = db.query(CollabMessage).filter_by(topic_id=topic.topic_id, card_kind=card)
        if card != 'resources':
            query = query.filter_by(ref_id=ref)
        existing = query.first()
        if existing:
            existing.body, existing.role, existing.thread_id = body, role or ('user' if author=='user' else 'conductor'), thread_id
            existing.agent_run_id = thread_id
            existing.plan_id = ref if card=='plan' else ''
            topic.updated_at = now()
            return existing
    row = CollabMessage(message_id=uid(), topic_id=topic.topic_id, author_kind=author,
        author_id=owner, body=body, message_kind='card' if card else kind, card_kind=card,
        ref_id=ref, origin=origin, role=role or ('user' if author=='user' else 'conductor'), thread_id=thread_id, agent_run_id=thread_id, plan_id=ref if card=='plan' else '', created_at=now())
    db.add(row)
    topic.updated_at = now()
    return row


def scope_ids(db, topic):
    terms = []
    if topic.requirement_id:
        terms.append(ProjectCase.requirement_id == topic.requirement_id)
    if topic.modules:
        terms.append(ProjectCase.module.in_(topic.modules))
    if topic.scope_case_ids:
        terms.append(ProjectCase.case_id.in_(topic.scope_case_ids))
    if not terms:
        return []
    rows = db.query(ProjectCase.case_id).filter(ProjectCase.project_id == topic.project_id,
        or_(*terms)).order_by(ProjectCase.sort_index, ProjectCase.pk).all()
    return list(dict.fromkeys(r[0] for r in rows))


def scope_options(project_id, sess):
    authorize(project_id, sess)
    from mino_nexus.resources import project_store as ps
    from mino_nexus.casework.case_import import list_project_requirements
    from mino_nexus.resources.node_registry import get_registry
    with session_scope() as db:
        modules = sorted({r[0] for r in db.query(ProjectCase.module).filter_by(project_id=project_id) if r[0]})
        cases = [{'case_id': c.case_id, 'name': c.name, 'module': c.module} for c in db.query(ProjectCase).filter_by(project_id=project_id)]
    owner = authorize(project_id, sess)
    registry = get_registry()
    nodes = {n.node_id for n in registry.nodes() if n.owner_user_id == owner and n.alive}
    devices = [{k: d.get(k) for k in ('sn', 'model', 'platform', 'node_id')} for d in registry.devices()
               if d.get('node_alive') and d.get('node_id') in nodes]
    env = ps.project_env(project_id)
    project = ps.require_project(project_id)
    return {'project': {'id': project_id, 'name': project.get('name') or project_id},
        'modules': modules, 'cases': cases, 'requirements': list_project_requirements(project_id),
        'apps': [{'id': a['id'], 'name': a.get('name', a['id'])} for a in ps.list_apps(project_id)],
        'environments': [{'key': e['key'], 'label': e.get('label', e['key'])} for e in env.get('environments', [])],
        'channels': [{'id': c['id'], 'app_id': c['app_id'], 'platform': c.get('platform', ''),
                      'label': next((a['name'] for a in ps.list_apps(project_id) if a['id'] == c['app_id']), c['app_id']) + ' · ' + c.get('platform', '')}
                     for c in env.get('channels', []) if c.get('app_id')],
        'devices': devices, 'nodes': [{'node_id': n.node_id} for n in registry.nodes() if n.node_id in nodes]}


def validate_scope(project_id, body, sess):
    opts = scope_options(project_id, sess)
    rid = str(body.get('requirement_id') or '').strip()
    modules = sorted({str(x).strip() for x in body.get('modules', []) if str(x).strip()})
    if rid and rid not in {r['id'] for r in opts['requirements']}:
        raise ValueError('需求不属于当前项目')
    if set(modules) - set(opts['modules']):
        raise ValueError('模块不属于当前项目')
    return rid, modules


def target_selection(opts, body, current=None):
    current = current or {}
    surface = str(body.get('env_surface', current.get('env_surface', '')) or '')
    aid = str(body.get('app_id', current.get('app_id', '')) or '')
    if 'app_id' in body and 'env_surface' not in body and aid != current.get('app_id'):
        matches = [c for c in opts['channels'] if c['app_id'] == aid]
        if aid and len(matches) != 1:
            raise ValueError('请选择明确的应用·渠道')
        surface = matches[0]['id'] if matches else ''
    channel = next((c for c in opts['channels'] if c['id'] == surface), None)
    if surface and not channel:
        choices = '、'.join(c['label'] + '（' + c['id'] + '）' for c in opts['channels'])
        suffix = '当前可选：' + choices + '。请确认本次测试渠道。' if choices else '当前项目尚未配置应用渠道，请先配置测试目标。'
        raise ValueError(f'渠道「{surface}」不在当前项目的渠道配置中，可能已删除或调整。' + suffix)
    if channel and 'app_id' in body and aid and aid != channel['app_id']:
        raise ValueError('应用与渠道不一致，请重新选择测试目标')
    return (channel['app_id'], surface, channel['platform']) if channel else ('', '', '')


def resolve_target(opts, view, args=None):
    """Environment and channel come from the request, then the project config.
    A value saved on the workspace is only a hint and is ignored once it is stale."""
    args = args or {}
    channels, envs = opts['channels'], opts['environments']
    project_name = (opts.get('project') or {}).get('name')
    project_label = f'项目「{project_name}」' if project_name else '当前项目'
    if args.get('app_id'):
        channels = [channel for channel in channels if channel['app_id'] == args['app_id']]
    if not channels:
        raise ValueError(project_label + '的目标应用尚未配置测试渠道，请先确认应用与渠道；本轮未执行用例。')
    env_keys = [e['key'] for e in envs]
    env = str(args.get('env_profile') or '')
    if env and env not in env_keys:
        raise ValueError(f"环境「{env}」不在项目配置中，可选：{'、'.join(env_keys) or '无'}。")
    if not env:
        saved = view.get('env_profile') or ''
        env = saved if saved in env_keys else ('test' if 'test' in env_keys else (env_keys[0] if env_keys else ''))
    ids = {c['id']: c for c in channels}
    surface = str(args.get('env_surface') or '')
    if surface and surface not in ids:
        raise ValueError(f"渠道「{surface}」不在项目配置中，可选：{'、'.join(c['label'] for c in channels) or '无'}。")
    if not surface and args.get('app_id'):
        mine = [c for c in channels if c['app_id'] == args['app_id']]
        surface = mine[0]['id'] if len(mine) == 1 else ''
    if not surface and view.get('env_surface') in ids:
        surface = view['env_surface']
    if not surface:
        online = {d.get('platform') for d in opts.get('devices', [])}
        pool = [c for c in channels if c['platform'] in online] or channels
        if len(pool) == 1:
            surface = pool[0]['id']
        elif view.get('platform'):
            same = [c for c in pool if c['platform'] == view['platform']]
            surface = same[0]['id'] if len(same) == 1 else ''
    if not surface:
        raise ValueError(project_label + '有多个应用渠道，请告诉我测哪一个：' + '、'.join(c['label'] for c in channels) + '。')
    ch = ids[surface]
    return {'app_id': ch['app_id'], 'env_surface': surface, 'platform': ch['platform'], 'env_profile': env, 'label': ch['label']}


def create_workspace(project_id, sess, body):
    owner = authorize(project_id, sess)
    rid, modules = validate_scope(project_id, body, sess)
    opts = scope_options(project_id, sess)
    aid, surface, platform = target_selection(opts, body)
    if aid and aid not in {a['id'] for a in opts['apps']}:
        raise ValueError('应用不属于当前项目')
    env = str(body.get('env_profile') or '')
    if env and env not in {e['key'] for e in opts['environments']}:
        raise ValueError('环境不属于当前项目')
    key = identity([project_id, owner, rid, modules]) if rid or modules else None
    with session_scope() as db:
        db.execute(text('BEGIN IMMEDIATE'))
        existing = db.query(CollabTopic).filter_by(project_id=project_id, lead_user_id=owner).filter(CollabTopic.scope_key.in_([key, identity([project_id, rid, modules])])).first() if key else None
        if existing:
            if not existing.archived:
                # Reusing a conversation must retain explicit choices from this request.
                if 'env_surface' in body or 'app_id' in body:
                    existing.app_id, existing.env_surface, existing.platform = aid, surface, platform
                if 'env_profile' in body:
                    existing.env_profile = env
                existing.updated_at = now()
            return public(existing)
        title = body.get('title') or next((r['title'] for r in opts['requirements'] if r['id'] == rid), '') or '、'.join(modules) or '新对话'
        row = CollabTopic(topic_id=uid(), project_id=project_id, app_id=aid, title=title,
            requirement_id=rid, modules=modules, scope_case_ids=[], workspace_kind='scope', scope_revision=1,
            scope_key=key, archived=0, env_profile=env, env_surface=surface, platform=platform, lead_user_id=owner, created_at=now(), updated_at=now())
        db.add(row)
        db.flush()
        from mino_nexus.assistant.workspace.workspace_members import add_defaults
        add_defaults(db, row.topic_id)
        activity(db, row, 'created', '已建立范围工作区')
        message(db, row, '想测什么？可以告诉我模块名称、发来用例，或者先聊聊测试目标。应用和环境等需要时再补充。')
        return public(row)


def list_workspaces(project_id, sess, archived=False):
    user_id = authorize(project_id, sess)
    with session_scope() as db:
        rows = db.query(CollabTopic).filter_by(project_id=project_id, lead_user_id=user_id, archived=int(archived)).order_by(CollabTopic.updated_at.desc()).all()
        output = []
        for t in rows:
            if not archived and t.workspace_kind not in ('scope', 'project'):
                continue
            seen = db.get(CollabWorkspaceRead, (t.topic_id, user_id))
            unread = db.query(CollabMessage).filter(CollabMessage.topic_id == t.topic_id,
                CollabMessage.author_kind != 'user', CollabMessage.created_at > (seen.read_at if seen else '')).count()
            latest = db.query(CollabMessage).filter_by(topic_id=t.topic_id).order_by(CollabMessage.created_at.desc()).first()
            output.append({**public(t), 'total': len(scope_ids(db, t)), 'unread': unread,
                           'progress': (latest.body or '已引用运行，打开查看实时进度')[:120] if latest else '尚无消息'})
        return output


def mark_read(project_id, topic_id, sess):
    user_id = authorize(project_id, sess)
    with session_scope() as db:
        db.execute(text('BEGIN IMMEDIATE'))
        owned(db, project_id, topic_id)
        latest = db.query(CollabMessage).filter_by(topic_id=topic_id).order_by(CollabMessage.created_at.desc()).first()
        seen = db.get(CollabWorkspaceRead, (topic_id, user_id))
        if not seen:
            seen = CollabWorkspaceRead(topic_id=topic_id, user_id=user_id)
            db.add(seen)
        seen.read_at = latest.created_at if latest else now()
        return {'read_at': seen.read_at}



def update_workspace(project_id, topic_id, sess, body):
    owner = authorize(project_id, sess)
    opts = scope_options(project_id, sess)
    with session_scope() as db:
        db.execute(text('BEGIN IMMEDIATE'))
        t = owned(db, project_id, topic_id, write=not (set(body) <= {'archived'}))
        if t.workspace_kind.startswith('legacy'):
            raise ValueError('历史工作区只读，请创建或打开范围工作区')
        if 'requirement_id' in body or 'modules' in body:
            rid = str(body.get('requirement_id', t.requirement_id) or '')
            mods = sorted(set(body.get('modules', t.modules) or []))
            if rid and rid not in {r['id'] for r in opts['requirements']} or set(mods) - set(opts['modules']):
                raise ValueError('范围不属于当前项目')
            key = identity([project_id, owner, rid, mods]) if rid or mods else None
            other = db.query(CollabTopic).filter_by(project_id=project_id, lead_user_id=owner).filter(CollabTopic.scope_key.in_([key, identity([project_id, rid, mods])]), CollabTopic.topic_id != topic_id).first()
            if key and other:
                if other.archived:
                    return public(other)
                # Reuse the existing scope, while retaining resource choices supplied this turn.
                t = other
            t.requirement_id, t.modules, t.scope_key = rid, mods, key
            if t.title == '新对话':
                t.title = next((r['title'] for r in opts['requirements'] if r['id'] == rid), '') or '、'.join(mods) or t.title
        if 'env_surface' in body or 'app_id' in body:
            t.app_id, t.env_surface, t.platform = target_selection(opts, body, public(t))
        for name, allowed in [('env_profile', {e['key'] for e in opts['environments']})]:
            if name in body:
                value = str(body[name] or '')
                if value and value not in allowed:
                    raise ValueError(name + ' 不属于当前项目配置')
                setattr(t, name, value)
        for key in ('title',):
            if key in body:
                setattr(t, key, str(body[key] or '')[:200])
        for key in ('paused', 'archived'):
            if key in body:
                setattr(t, key, int(bool(body[key])))
        if 'scope_case_ids' in body:
            ids = sorted(set(body['scope_case_ids']))
            available = {r[0] for r in db.query(ProjectCase.case_id).filter_by(project_id=project_id)}
            if set(ids) - available:
                raise ValueError('用例不属于当前项目')
            t.scope_case_ids = ids
        activity(db, t, 'updated', '工作区配置已更新')
        return public(t)


def pin_case(project_id, topic_id, sess, case_id):
    authorize(project_id, sess)
    with session_scope() as db:
        db.execute(text('BEGIN IMMEDIATE'))
        t = owned(db, project_id, topic_id, write=True)
        case = db.query(ProjectCase).filter_by(project_id=project_id, case_id=case_id).first()
        if case is None:
            raise KeyError('用例不存在')
        if case_id not in (t.scope_case_ids or []):
            t.scope_case_ids = sorted(set(t.scope_case_ids or []) | {case_id})
            message(db, t, card='case', ref=case_id)
            activity(db, t, 'pin', '已加入用例：' + case.name, case_id=case_id)
        return public(t)


def resources(project_id, topic, owner):
    from mino_nexus.resources.node_registry import get_registry
    from mino_nexus.models.pool_account import PoolAccount
    registry = get_registry()
    node_ids = {n.node_id for n in registry.nodes() if n.owner_user_id == owner and n.alive}
    devices = [d for d in registry.devices() if d.get('node_alive') and d.get('node_id') in node_ids
        and (not topic.platform or d.get('platform') == topic.platform)]
    free = None
    if topic.env_profile:
        with session_scope() as db:
            accounts = db.query(PoolAccount).filter_by(project_id=project_id, env=topic.env_profile).all()
            free = [a for a in accounts if not a.locked and not a.lease]
    return {'online_devices': len(devices), 'available_accounts': len(free) if free is not None else None,
        'devices': [{k: d.get(k) for k in ('sn', 'platform', 'model')} for d in devices],
        'env_profile': topic.env_profile or '',
        'account_note': '未锁定且无租约的账号数；具体资源条件仍以执行前准入为准' if free is not None else '尚未选择环境，账号可用数量未核验'}


def snapshot(project_id, topic, owner):
    from mino_nexus.casework.case_store import get_case
    from mino_nexus.casework.case_preflight import partition_cases
    with session_scope() as db:
        ids = scope_ids(db, topic)
    cases = [get_case(project_id, cid) for cid in ids]
    cases = [c for c in cases if c]
    _, check = partition_cases(cases)
    res = resources(project_id, topic, owner)
    blocked = {b['case_id']: b for b in check.get('blocked', [])}
    return {'total': len(ids), 'case_ids': ids, 'executable': [c for c in ids if c not in blocked],
        'blocked': list(blocked.values()), 'cases': [{'case_id': c['case_id'],
            'title': c.get('name') or c.get('title') or c['case_id'], 'module': c.get('module', ''),
            'priority': c.get('priority') or (c.get('meta') or {}).get('priority', ''),
            'status': (blocked.get(c['case_id']) or {}).get('status') or ('blocked' if c['case_id'] in blocked else 'ready'),
            'reasons': blocked.get(c['case_id'], {}).get('reasons', []), 'updated_at': c.get('updated_at', '')}
            for c in cases], 'resources': {**res, 'compile_ready': len(check.get('runnable', [])),
                'binding_ready': len(check.get('runnable', []))}}


def detail(project_id, topic_id, sess):
    owner = authorize(project_id, sess)
    with session_scope() as db:
        t = owned(db, project_id, topic_id)
        topic = public(t)
        messages = [public(x) for x in reversed(db.query(CollabMessage).filter_by(topic_id=topic_id)
            .order_by(CollabMessage.created_at.desc()).limit(200).all())]
        from mino_nexus.models.collab_message_job import CollabMessageJob
        jobs = {j.message_id: j for j in db.query(CollabMessageJob).filter_by(topic_id=topic_id)}
        from mino_nexus.assistant.workspace.workspace_members import sender_name
        for m in messages:
            m['sender_name'] = sender_name(m)
            j = jobs.get(m['message_id'])
            m['reply_status'] = j.status if j else ''
            m['reply_error'] = j.error if j else ''
        snap = snapshot(project_id, t, owner)
    # Run cards render live from run_store; nothing is frozen into the message.
    from mino_nexus.casework import run_store
    from mino_nexus.assistant.workspace.workspace_review import freshness
    versions = freshness(project_id, topic_id)
    runs = {}
    for m in messages:
        if m['card_kind'] == 'run' and m['ref_id'] and m['ref_id'] not in runs:
            doc = run_store.get(m['ref_id']) or {}
            runs[m['ref_id']] = {'run_id': m['ref_id'], 'app_id': doc.get('app_id', ''), 'status': doc.get('status', 'unknown'), 'freshness': versions.get(m['ref_id'], 'unknown'), **run_outcome(doc), 'total': doc.get('total'),
                'passed': doc.get('passed'), 'failed': doc.get('failed'), 'cases': [
                    {'case_id': c.get('case_id'), 'status': c.get('status'), 'session_id': c.get('session_id')}
                    for c in (doc.get('cases') or []) if isinstance(c, dict)]}
    from mino_nexus.assistant.workspace.workspace_members import list_members
    return {'topic': topic, 'snapshot': snap, 'messages': messages, 'runs': runs, 'members': list_members(project_id, topic_id, sess)}


def reference_run(project_id, topic_id, run_id, *, source=''):
    with session_scope() as db:
        from mino_nexus.assistant.workspace.workspace_review import capture
        capture(db, owned(db, project_id, topic_id), run_id)
        message(db, owned(db, project_id, topic_id), card='run', ref=run_id, role='conductor', origin='message:' + source if source else '')
    from mino_nexus.assistant.workspace.workspace_automation import attach_notifications
    attach_notifications(project_id, topic_id, run_id)
    from mino_nexus.casework import run_store
    doc = run_store.get(run_id) or {}
    if doc.get('status') in ('done', 'finished', 'failed', 'cancelled', 'blocked', 'passed'):
        from mino_nexus.casework.collab_events import on_run_finished
        on_run_finished(doc)


def run_outcome(doc):
    """Nothing passed and nothing failed means nothing ran: say so instead of 'failed 0'."""
    doc = doc or {}
    if int(doc.get('passed') or 0) + int(doc.get('failed') or 0) > 0:
        return {'outcome': str(doc.get('status') or ''), 'reason': ''}
    if str(doc.get('status') or '') not in ('done', 'finished', 'failed', 'cancelled', 'blocked'):
        return {'outcome': str(doc.get('status') or ''), 'reason': ''}
    reasons = [str(c.get('fail_reason') or c.get('failure') or c.get('reason') or '') for c in (doc.get('cases') or []) if isinstance(c, dict)]
    reason = str(doc.get('error') or '') or next((r for r in reasons if r), '')
    return {'outcome': 'not_executed', 'reason': reason}


def notify_run_finished(doc):
    """Single link from execution to workspaces: only runs a workspace card already references."""
    run_id = str((doc or {}).get('task_id') or (doc or {}).get('run_id') or '')
    if not run_id:
        return
    with session_scope() as db:
        db.execute(text('BEGIN IMMEDIATE'))
        topics = {m.topic_id for m in db.query(CollabMessage).filter_by(card_kind='run', ref_id=run_id)}
        for tid in topics:
            t = db.get(CollabTopic, tid)
            if t is None or not doc.get('owner_user_id') or t.lead_user_id != doc['owner_user_id']:
                continue
            res = run_outcome(doc)
            if res['outcome'] == 'not_executed':
                body = '本次没有执行任何用例' + ('：' + res['reason'] if res['reason'] else '，原因未记录，请查看运行与证据。')
            else:
                body = f"运行已结束：{doc.get('status') or ''}。通过 {doc.get('passed', 0)}，失败 {doc.get('failed', 0)}。"
            message(db, t, body, role='conductor', card='run_finished', ref=run_id)




def pin_cases(project_id, topic_id, sess, case_ids):
    authorize(project_id, sess)
    ids = list(dict.fromkeys(case_ids))
    if not ids or len(ids) > 1000:
        raise ValueError('请选择 1—1000 条用例')
    with session_scope() as db:
        db.execute(text('BEGIN IMMEDIATE'))
        topic = owned(db, project_id, topic_id, write=True)
        if topic.paused:
            raise ValueError('工作区已暂停，请先恢复')
        available = {r[0] for r in db.query(ProjectCase.case_id).filter(ProjectCase.project_id == project_id, ProjectCase.case_id.in_(ids))}
        if set(ids) != available:
            raise ValueError('所选用例已删除或不属于当前项目；本次未加入任何用例')
        topic.scope_case_ids = sorted(set(topic.scope_case_ids or []) | set(ids))
        message(db, topic, f'已钉住 {len(ids)} 条用例。', card='case_set')
        return {'topic_id': topic_id, 'case_ids': scope_ids(db, topic)}


def list_runs(project_id, topic_id, sess, offset=0, limit=20):
    """Read existing referenced runs; independent from the chat history window."""
    authorize(project_id, sess)
    from sqlalchemy import func
    from mino_nexus.casework import run_store
    with session_scope() as db:
        owned(db, project_id, topic_id)
        refs = db.query(CollabMessage.ref_id, func.max(CollabMessage.created_at).label('stamp')).filter_by(
            topic_id=topic_id, card_kind='run').group_by(CollabMessage.ref_id)
        total = refs.count()
        rows = refs.order_by(func.max(CollabMessage.created_at).desc(), CollabMessage.ref_id).offset(offset).limit(limit).all()
    items = []
    for rid, _ in rows:
        doc = run_store.get(rid) or {}
        items.append({'run_id': rid, 'app_id': doc.get('app_id', ''),
            'name': doc.get('name') or doc.get('app_name') or '测试任务',
            'status': doc.get('status', 'unknown'), **run_outcome(doc),
            **{key: doc.get(key) for key in ('total', 'completed', 'passed', 'failed')},
            'started_at': doc.get('started_at') or '', 'finished_at': doc.get('finished_at') or ''})
    return {'items': items, 'total': total, 'offset': offset, 'limit': limit}


from mino_nexus.contract.workspace_hooks import register_run_finished  # noqa: E402

register_run_finished(notify_run_finished)
