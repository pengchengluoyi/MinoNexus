"""Workspace triggers enqueue durable messages; all execution uses shared tools."""
import hashlib
import json
import time
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from mino_nexus.core.database import session_scope
from mino_nexus.platform.identity import execution_user
from mino_nexus.models.mino_bots import CollabTopic, CollabMessage
from mino_nexus.models.collab_message_job import CollabMessageJob
from mino_nexus.assistant.workspace import collab_workspace as ws
from mino_nexus.casework import run_store


class Policy(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool = False
    analyze_finished: bool = False
    report_finished: bool = False
    interval_minutes: int = Field(default=0, ge=0, le=525600)
    watch_manual_failures: bool = False
    resource_ready: bool = False
    scope_changed: bool = False
    cases_changed: bool = False
    compilation_ready: bool = False
    retry_failed: int = Field(default=0, ge=0, le=3)
    device_sn: str = Field(default='', max_length=200)
    notification_subscription_id: int = Field(default=0, ge=0)


def config(topic):
    saved = dict(topic.workspace_policy or {})
    return Policy.model_validate({k: v for k, v in (saved.get('config') or {}).items() if k != 'build_changed'}).model_dump(), saved


def get(project_id, topic_id, sess):
    owner = ws.authorize(project_id, sess)
    from mino_nexus.assistant.store import list_subscriptions
    with session_scope() as db:
        topic = ws.owned(db, project_id, topic_id)
        policy, saved = config(topic)
    return {'config': policy, 'revision': int(saved.get('revision', 0)),
        'next_due': saved.get('next_due'), 'last_error': saved.get('last_error', ''),
        'notifications': [{'id': s['id'], 'label': f"{s['channel']} · 订阅 {s['id']}"} for s in list_subscriptions(owner)
                          if s['kind'] == 'run_done' and s['status'] != 'revoked']}


def subscription(owner, sid):
    from mino_nexus.models.assistant import AssistantSubscription
    from mino_nexus.assistant.store import require_im_binding
    with session_scope() as db:
        row = db.get(AssistantSubscription, sid)
        if not row or row.user_id != owner or row.kind != 'run_done' or row.status == 'revoked':
            raise ValueError('通知目标不可用，请选择当前账号已绑定的通知会话。')
        result = {k: getattr(row, k) for k in ('user_id', 'channel', 'node_id', 'chat_id', 'sender_id', 'reply_ctx')}
    require_im_binding(owner, result['channel'], result['node_id'], result['reply_ctx'])
    return result


def save(project_id, topic_id, sess, policy, revision):
    owner = ws.authorize(project_id, sess)
    value = Policy.model_validate(policy).model_dump()
    if value['enabled'] and value['report_finished']:
        from mino_nexus.assistant.workspace.workspace_members import validate_assignment
        validate_assignment(project_id, topic_id, sess, 'report-writer', 'draft_test_report')
    if value['notification_subscription_id']:
        subscription(owner, value['notification_subscription_id'])
    with session_scope() as db:
        db.execute(text('BEGIN IMMEDIATE'))
        topic = ws.owned(db, project_id, topic_id, write=True)
        _, prior = config(topic)
        if revision != int(prior.get('revision', 0)):
            raise ValueError('自动化配置已改变，请刷新后保存。')
        executes = any(value[k] for k in ('interval_minutes', 'resource_ready', 'scope_changed', 'cases_changed', 'compilation_ready', 'retry_failed'))
        if value['enabled'] and executes and (not ws.scope_ids(db, topic) or not topic.env_surface or not topic.env_profile):
            raise ValueError('开启自动执行前，请保存非空用例范围、应用·渠道和环境。')
        topic.workspace_policy = {'config': value, 'revision': revision + 1, 'activated_at': ws.now(),
            'next_due': time.time() + value['interval_minutes'] * 60 if value['interval_minutes'] else None}
        ws.message(db, topic, '自动化配置已更新。' + ('触发时使用已确认范围；暂停或归档会停止自动执行。' if value['enabled'] else '自动化已关闭。'))
    return get(project_id, topic_id, sess)


def enqueue(db, topic, action, key, *, run_id='', attempt=0, root=''):
    policy, saved = config(topic)
    if not policy['enabled'] or topic.archived:
        return False
    revision = int(saved.get('revision', 0))
    mid = 'auto-' + hashlib.sha256(f'{topic.topic_id}:{revision}:{action}:{key}'.encode()).hexdigest()
    if db.get(CollabMessage, mid):
        return False
    payload = {'action': action, 'run_id': run_id, 'attempt': attempt, 'root': root,
        'revision': revision, 'selection': selection(db, topic)}
    labels = {'run': '触发自动回归', 'retry': '触发失败项重跑', 'analyze': '运行结束，分析结果', 'report': '运行结束，生成报告'}
    stamp = ws.now()
    db.add(CollabMessage(message_id=mid, topic_id=topic.topic_id, author_kind='system', role='conductor',
        body=labels[action], origin='workspace_automation', ref_id=json.dumps(payload, ensure_ascii=False), created_at=stamp))
    db.add(CollabMessageJob(message_id=mid, project_id=topic.project_id, topic_id=topic.topic_id,
        owner_user_id=topic.lead_user_id, status='queued', created_at=stamp, updated_at=stamp))
    topic.updated_at = stamp
    return True


def selection(db, topic):
    return {'case_ids': sorted(ws.scope_ids(db, topic)), 'app_id': topic.app_id, 'env_surface': topic.env_surface,
        'env_profile': topic.env_profile}


def on_finished(doc):
    rid = str(doc.get('run_id') or doc.get('task_id') or '')
    if not rid or run_store.task_is_live(doc):
        return
    with session_scope() as db:
        db.execute(text('BEGIN IMMEDIATE'))
        # Adoption is explicitly configured and never expands a workspace's scope.
        if doc.get('run_type', 'manual') == 'manual' and any(c.get('status') in ('fail', 'failed', 'blocked', 'declined') for c in doc.get('cases') or []):
            for topic in db.query(CollabTopic).filter_by(lead_user_id=doc.get('owner_user_id'), app_id=doc.get('app_id'), archived=0):
                policy, saved = config(topic)
                if not policy['enabled'] or not policy['watch_manual_failures'] or not doc.get('started_at') or doc['started_at'] < saved.get('activated_at', ''):
                    continue
                ids = {c.get('case_id') for c in doc.get('cases') or []}
                if not ids or not ids <= set(ws.scope_ids(db, topic)) or topic.env_surface != doc.get('env_surface') or topic.env_profile != doc.get('env_profile'):
                    continue
                # No submission-time snapshot exists for adopted runs; freshness stays unknown.
                if not db.query(CollabMessage).filter_by(topic_id=topic.topic_id, card_kind='run', ref_id=rid).first():
                    ws.message(db, topic, '已按配置纳入当前范围的手动失败运行。', card='run', ref=rid, origin='manual_adoption')
            db.flush()
        cards = db.query(CollabMessage).filter_by(card_kind='run', ref_id=rid).all()
        for card in cards:
            topic = db.get(CollabTopic, card.topic_id)
            if not topic or topic.lead_user_id != doc.get('owner_user_id'):
                continue
            policy, saved = config(topic)
            if card.created_at < saved.get('activated_at', ''):
                continue  # Enabling a policy never replays historical runs.
            source = db.get(CollabMessage, card.origin[8:]) if card.origin.startswith('message:') else None
            payload = json.loads(source.ref_id) if source and source.origin == 'workspace_automation' else {}
            depth, root = int(payload.get('attempt', 0)), payload.get('root') or rid
            if policy['analyze_finished']:
                enqueue(db, topic, 'analyze', rid, run_id=rid)
            if policy['report_finished']:
                enqueue(db, topic, 'report', rid, run_id=rid)
            failed = any(c.get('status') in ('fail', 'failed', 'blocked', 'declined') for c in doc.get('cases', []) if isinstance(c, dict))
            if failed and doc.get('status') != 'cancelled' and depth < policy['retry_failed']:
                enqueue(db, topic, 'retry', rid, run_id=rid, attempt=depth + 1, root=root)
    from mino_nexus.assistant.workspace.collab_message_queue import wake
    wake()


def attach_notifications(project_id, topic_id, run_id):
    with session_scope() as db:
        topic = ws.owned(db, project_id, topic_id)
        policy, _ = config(topic)
        owner = topic.lead_user_id
    if not policy['enabled'] or not policy['notification_subscription_id']:
        return
    try:
        target = subscription(owner, policy['notification_subscription_id'])
        from mino_nexus.assistant.store import add_subscription
        add_subscription({**target, 'kind': 'run_done', 'target_id': run_id})
    except (ValueError, PermissionError):
        with session_scope() as db:
            ws.message(db, ws.owned(db, project_id, topic_id), '运行已提交，但通知绑定已失效；请重新选择通知会话。')


def process(project_id, topic_id, sess, message_id, payload):
    from mino_nexus.assistant.workspace.workspace_agent import process as agent, run_one_tool
    from mino_nexus.assistant.assistant_tools import get_tool
    from mino_nexus.platform.llm import dispatch_log
    owner = ws.authorize(project_id, sess)
    with session_scope() as db:
        topic = ws.owned(db, project_id, topic_id, write=True)
        policy, saved = config(topic)
        if not policy['enabled'] or payload['revision'] != saved.get('revision'):
            raise ValueError('自动化已关闭或配置已变化，本次触发已停止。')
        if payload['action'] in ('run', 'retry') and payload['selection'] != selection(db, topic):
            raise ValueError('测试范围或目标已变化，本次自动执行已停止。')
    action, rid = payload['action'], payload.get('run_id', '')
    if action == 'analyze':
        return agent(project_id, topic_id, sess, '分析本次已结束运行的失败原因和证据，并说明后续建议。',
            source_message_id=message_id, read_only=True, event_context={'run_id': rid})
    if action == 'report':
        from mino_nexus.assistant.workspace.workspace_members import validate_assignment
        validate_assignment(project_id, topic_id, sess, 'report-writer', 'draft_test_report')
        from mino_nexus.assistant.workspace.workspace_reports import generate, markdown
        report = generate(project_id, topic_id, sess, rid)
        with session_scope() as db:
            ws.message(db, ws.owned(db, project_id, topic_id), markdown(report), card='report', ref=rid, role='report-writer')
        return
    if action not in ('run', 'retry'):
        raise ValueError('未知自动触发类型')
    state = {'wrote': False, 'summary': '', 'source': message_id}
    args = {'task_id': rid} if action == 'retry' else dict(payload['selection'])
    if action == 'run' and policy['device_sn']:
        args['sn'] = policy['device_sn']
    token = dispatch_log.bind(assistant_mutation_scope=message_id)
    try:
        result = run_one_tool(project_id, topic_id, sess, owner,
            get_tool('case.retry_failed' if action == 'retry' else 'case.run'), args, state, {'project_id': project_id})
        with session_scope() as db:
            ws.message(db, ws.owned(db, project_id, topic_id), result.get('stop') or state['summary'], role='conductor')
    finally:
        dispatch_log.reset(token)


_last_tick = 0.0


def tick(_worker_id=''):
    global _last_tick
    if time.monotonic() - _last_tick < 15:
        return False
    _last_tick = time.monotonic()
    from mino_nexus.platform.auth_store import api_session_for
    from mino_nexus.resources.device_targets import select
    from mino_nexus.resources.project_store import require_app
    from mino_nexus.models.case import ProjectCase
    with session_scope() as db:
        targets = [(t.topic_id, t.project_id, t.lead_user_id) for t in db.query(CollabTopic).filter_by(archived=0)
                   if (t.workspace_policy or {}).get('config', {}).get('enabled')]
    worked = False
    for tid, pid, owner in targets:
        try:
            with execution_user(owner):
                sess = api_session_for(owner)
                ws.authorize(pid, sess)
                with session_scope() as db:
                    topic = ws.owned(db, pid, tid)
                    policy, saved = config(topic)
                    if db.query(CollabMessageJob).filter_by(topic_id=tid).filter(CollabMessageJob.status.in_(('queued', 'running'))).first():
                        continue
                    run_ids = [m.ref_id for m in db.query(CollabMessage).filter_by(topic_id=tid, card_kind='run')]
                    current = selection(db, topic)
                    rows = db.query(ProjectCase).filter(ProjectCase.project_id == pid, ProjectCase.case_id.in_(current['case_ids'])).all()
                    changed = sorted((r.case_id, r.updated_at) for r in rows)
                    view = ws.public(topic)
                docs = [run_store.get(rid) for rid in run_ids]
                if policy['watch_manual_failures']:
                    known = set(run_ids)
                    docs.extend(d for d in run_store.list_runs(app_id=current['app_id'], limit=100) if d['run_id'] not in known and not run_store.task_is_live(d))
                if any(d and run_store.task_is_live(d) for d in docs):
                    continue
                # Catch up completion hooks lost at shutdown; message IDs deduplicate.
                for doc in docs:
                    if doc:
                        on_finished(doc)
                ready = False
                try:
                    select(require_app(current['app_id']), [policy['device_sn']] if policy['device_sn'] else [],
                           surface=current['env_surface'], profile=current['env_profile'])
                    ready = True
                except Exception:
                    pass
                resource_view = ws.resources(pid, ws.SimpleTopic(view), owner) if policy['resource_ready'] else {}
                compilation = ws.snapshot(pid, ws.SimpleTopic(view), owner) if policy['compilation_ready'] else {}
                observed = {'scope': hashlib.sha256(json.dumps(current, sort_keys=True).encode()).hexdigest(),
                    'cases': hashlib.sha256(json.dumps(changed).encode()).hexdigest(), 'ready': ready,
                    'accounts': resource_view.get('available_accounts'), 'compiled': bool(compilation.get('case_ids')) and not compilation.get('blocked')}
                previous = saved.get('observed')
                reasons = []
                if previous:
                    for flag, key in [('scope_changed', 'scope'), ('cases_changed', 'cases'), ('resource_ready', 'ready'), ('compilation_ready', 'compiled')]:
                        if policy[flag] and observed[key] and observed[key] != previous.get(key):
                            reasons.append(flag)
                if previous and policy['resource_ready'] and ready and isinstance(observed['accounts'], int) and isinstance(previous.get('accounts'), int) and observed['accounts'] > previous['accounts']:
                    reasons.append('accounts_recovered')
                if policy['interval_minutes'] and (saved.get('next_due') or float('inf')) <= time.time():
                    reasons.append('schedule:' + str(saved['next_due']))
                with session_scope() as db:
                    db.execute(text('BEGIN IMMEDIATE'))
                    topic = ws.owned(db, pid, tid)
                    _, latest = config(topic)
                    if latest.get('revision') != saved.get('revision'):
                        continue
                    # An on_finished call above may just have queued analysis/retry work.
                    if db.query(CollabMessageJob).filter_by(topic_id=tid).filter(CollabMessageJob.status.in_(('queued', 'running'))).first():
                        continue
                    latest['observed'] = observed
                    if reasons:
                        sequence = int(latest.get('trigger_sequence', 0)) + 1
                        worked |= enqueue(db, topic, 'run', json.dumps([sequence, reasons, observed], sort_keys=True))
                        latest['trigger_sequence'] = sequence
                        if policy['interval_minutes']:
                            latest['next_due'] = time.time() + policy['interval_minutes'] * 60
                    latest['last_error'] = ''
                    topic.workspace_policy = latest
        except Exception as exc:
            from mino_nexus.core.log import SLog
            SLog.w('WorkspaceAutomation', f'topic={tid} {type(exc).__name__}')
            with session_scope() as db:
                topic = db.get(CollabTopic, tid)
                if topic:
                    topic.workspace_policy = {**(topic.workspace_policy or {}), 'last_error': '自动化检查失败，请检查目标配置与账号权限。'}
    return worked


from mino_nexus.contract.workspace_hooks import register_run_finished  # noqa: E402

register_run_finished(on_finished)
