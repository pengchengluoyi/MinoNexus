"""Workspace entry point over the shared assistant tools; no separate executor."""
import json
import time
from urllib.parse import urlparse

from mino_nexus.core.database import session_scope
from mino_nexus.models.case import ProjectCase
from mino_nexus.models.mino_bots import CollabMessage
from mino_nexus.services import collab_workspace as ws

MAX_STEPS = 8
FACT_LIMIT = 20000


def bounded_json(value, limit=FACT_LIMIT):
    """Trim complete fields, never cut serialized JSON in the middle."""
    value = json.loads(json.dumps(value, ensure_ascii=False, default=str))
    def candidates(node):
        if isinstance(node, dict):
            for key, item in node.items():
                if isinstance(item, (list, dict)):
                    yield from candidates(item)
                if isinstance(item, str) and len(item) > 300:
                    yield len(item), node, key
        elif isinstance(node, list):
            if node:
                yield len(json.dumps(node, ensure_ascii=False)), node, None
    while len(json.dumps(value, ensure_ascii=False)) > limit:
        choices = list(candidates(value))
        if not choices:
            return json.dumps({'truncated': True, 'summary': '结果超过上下文预算，请缩小查询范围。'}, ensure_ascii=False)
        _, parent, key = max(choices, key=lambda item: item[0])
        if key is None:
            parent.pop()
        else:
            parent[key] = parent[key][:300] + '…'
    return json.dumps(value, ensure_ascii=False)


def facts(project_id, topic_id, sess, content):
    owner = ws.authorize(project_id, sess)
    opts = ws.scope_options(project_id, sess)
    with session_scope() as db:
        topic = ws.owned(db, project_id, topic_id, write=True)
        view = ws.public(topic)
        recent = [{'author': m.author_kind, 'body': (m.body or '')[:800]} for m in reversed(
            db.query(CollabMessage).filter_by(topic_id=topic_id).order_by(CollabMessage.created_at.desc()).limit(6).all())]
        run_ids = [m.ref_id for m in db.query(CollabMessage).filter_by(topic_id=topic_id, card_kind='run')
                   .order_by(CollabMessage.created_at.desc()).limit(8) if m.ref_id]
    snap = ws.snapshot(project_id, ws.SimpleTopic(view), owner)
    from mino_nexus.services import run_store
    runs = []
    for rid in dict.fromkeys(run_ids):
        doc = run_store.get(rid) or {}
        if doc:
            runs.append({k: doc.get(k) for k in ('run_id', 'status', 'total', 'passed', 'failed')})
    return {
        'project_id': project_id, 'topic_id': topic_id,
        'scope': {k: view[k] for k in ('title', 'requirement_id', 'modules', 'app_id', 'env_profile', 'env_surface', 'platform', 'paused')},
        'cases': {'total': snap['total'], 'executable': len(snap['executable']), 'blocked': len(snap['blocked']),
                  'case_ids': snap['case_ids'][:200], 'items': snap['cases'][:50], 'truncated': len(snap['case_ids']) > 200},
        'resources': snap['resources'], 'runs_from_this_workspace': runs,
        'choices': {k: opts[k] for k in ('modules', 'requirements', 'apps', 'environments', 'channels')},
        'recent_messages': recent,
    }


def _say(project_id, topic_id, body, card='', ref=''):
    with session_scope() as db:
        ws.message(db, ws.owned(db, project_id, topic_id), body, card=card, ref=ref, role='conductor')


def execution_args(project_id, topic_id, sess, spec, args):
    """Resolve workspace selection before invoking the unchanged execution tool."""
    args = dict(args)
    with session_scope() as db:
        topic = ws.owned(db, project_id, topic_id, write=True)
        scope = ws.scope_ids(db, topic)
        view = ws.public(topic)
    if view['paused'] and spec.get('risk') == 'write':
        raise ValueError('工作区已暂停，请在菜单中恢复后再提交操作。')
    if args.get('project_id', project_id) != project_id:
        raise ValueError('请使用当前工作区所属项目。')
    if spec['id'] == 'collab.scope_update':
        if args.get('topic_id', topic_id) != topic_id:
            raise ValueError('请修改当前工作区的范围。')
        return {**args, 'project_id': project_id, 'topic_id': topic_id}
    if spec['id'] not in ('case.run', 'case.retry_failed'):
        return args
    if not scope:
        raise ValueError('当前范围为空，请先选择已有模块、需求或钉住用例。')
    if spec['id'] == 'case.retry_failed':
        from mino_nexus.services import run_store
        from mino_nexus.services.project_store import require_app
        doc = run_store.get(str(args.get('task_id') or '')) or {}
        if not doc or require_app(doc['app_id']).get('project_id') != project_id:
            raise ValueError('原运行不可读取或不属于当前项目。')
        if not {c['case_id'] for c in doc.get('cases', [])} <= set(scope):
            raise ValueError('原运行包含当前范围之外的用例，请先明确本次范围。')
        return args
    for key in ('app_id', 'env_profile', 'env_surface'):
        if not args.get(key) and view.get(key):
            args[key] = view[key]
    if not args.get('app_id') or not args.get('env_profile'):
        raise ValueError('执行前请明确应用与环境；可以先保存到工作区范围与资源。')
    if args.get('all_cases'):
        raise ValueError('工作区只执行已绑定的范围，请明确模块、需求或用例。')
    if any(args.get(key) for key in ('module', 'modules', 'search', 'ordinal')) and not args.get('case_ids'):
        raise ValueError('请从当前范围选择准确的 case_ids；修改范围请先保存工作区。')
    ids = args.get('case_ids') or scope
    if not isinstance(ids, list) or not set(ids) <= set(scope):
        raise ValueError('执行用例必须属于当前工作区范围。')
    args['case_ids'] = list(dict.fromkeys(ids))
    args['project_id'] = project_id
    return args


def process(project_id, topic_id, sess, content, *, source_message_id):
    owner = ws.authorize(project_id, sess)
    parsed = urlparse(content)
    if parsed.scheme in ('http', 'https') and parsed.netloc and not any(c.isspace() for c in content):
        _say(project_id, topic_id, '已收到来源链接，可打开导入预览后提交。', card='import', ref=content)
        return
    with session_scope() as db:
        case = db.query(ProjectCase).filter_by(project_id=project_id, case_id=content).first()
        if case:
            ws.message(db, ws.owned(db, project_id, topic_id), '已找到用例，加入本工作区？', card='case_candidate', ref=case.case_id)
            return
    from mino_nexus.ai import dispatch_log
    from mino_nexus.ai.prompt_render import render
    from mino_nexus.services.assistant_agent import _parse_args, _persona, _call_model
    from mino_nexus.services.assistant_tools import openai_tools, run_tool, tool_by_function_name

    fact = facts(project_id, topic_id, sess, content)
    history = '\n'.join(f"{'用户' if m['author'] == 'user' else '助手'}：{m['body']}" for m in fact.pop('recent_messages'))
    messages, meta = render('workspace-route', {'persona_block': _persona({}),
        'workspace_facts': bounded_json(fact), 'recent_turns': history, 'utterance': content})
    target = {'project_id': project_id}
    tools = openai_tools()
    deadline = time.monotonic() + float((meta.get('call') or {}).get('total_timeout_sec', 300))
    seen, wrote, answer, last_summary = set(), False, '', ''
    token = dispatch_log.bind(assistant_mutation_scope=source_message_id)
    try:
        for _ in range(MAX_STEPS):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            call_meta = {**meta, 'call': {**meta.get('call', {}), 'timeout_sec': min(remaining, float(meta.get('call', {}).get('timeout_sec', 60)))}}
            reply, calls, model_meta = _call_model(messages, call_meta, tools=tools,
                trace={'job': 'workspace-route', 'trigger': 'workspace_chat', 'source': 'workspace_agent', 'role': 'conductor'})
            if model_meta.get('error') and not reply and not calls:
                raise ValueError('大模型暂时不可用：' + str(model_meta['error'])[:200])
            if not calls:
                answer = reply
                break
            messages.append({'role': 'assistant', 'content': reply or '', 'tool_calls': calls})
            for call in calls:
                if time.monotonic() >= deadline:
                    answer = '本轮处理时间已用完。' + last_summary
                    break
                fn = call.get('function') or {}
                name = str(fn.get('name') or '')
                args = _parse_args(fn.get('arguments'))
                if name == 'reply':
                    answer = str(args.get('text') or reply)
                    break
                spec = tool_by_function_name(name)
                if not spec:
                    answer = '请求的工具不可用，请明确要查询的任务或用例。'
                    break
                sig = json.dumps([spec['id'], args], sort_keys=True, ensure_ascii=False)
                if sig in seen:
                    answer = '相同查询没有新信息，已停止继续查询。' + last_summary
                    break
                seen.add(sig)
                if spec.get('risk') == 'write' and wrote:
                    answer = last_summary + ' 本条消息已提交一次写操作，后续操作请另发消息。'
                    break
                try:
                    args = execution_args(project_id, topic_id, sess, spec, args)
                except ValueError as exc:
                    answer = str(exc)
                    break
                if spec.get('risk') == 'write':
                    wrote = True  # Includes failures and uncertain submissions; never retry this turn.
                    if spec['id'] == 'case.run':
                        args['client_request_id'] = source_message_id
                out = run_tool(spec, args, user_id=owner, target=target, surface='workspace')
                last_summary = out.ask or out.summary
                if out.ask or not out.ok:
                    labels = [str(c.get('label') or c.get('value') or '') for c in out.candidates] or out.options
                    answer = last_summary + ('\n可选：' + '、'.join(labels) if labels else '')
                    break
                if spec['id'] in ('case.run', 'case.retry_failed'):
                    data = out.data
                    rid = data if isinstance(data, str) else (data.get('task_id') or data.get('run_id')) if isinstance(data, dict) else ''
                    if rid:
                        ws.reference_run(project_id, topic_id, str(rid))
                        last_summary = '执行请求已提交，实时结果见运行卡片。'
                if spec['id'] == 'collab.scope_update' and isinstance(out.data, dict):
                    tid = out.data.get('topic_id')
                    if tid and tid != topic_id:
                        _say(project_id, topic_id, '同范围工作区已存在，请在该工作区继续。', card='workspace', ref=tid)
                        answer = '已找到同范围工作区，可点击卡片继续。'
                        break
                    last_summary = '工作区范围与资源已保存。'
                messages.append({'role': 'tool', 'tool_call_id': call.get('id') or name,
                    'content': bounded_json({'ok': out.ok, 'summary': last_summary, 'data': out.data}, 4000)})
            if answer:
                break
    finally:
        dispatch_log.reset(token)
    _say(project_id, topic_id, answer or last_summary or '本轮处理达到上限，请补充范围或任务后继续。')
