"""Bounded member assignments over existing role and QA entry points."""
import time
from mino_nexus.assistant.workspace import collab_workspace as ws
from mino_nexus.assistant.workspace.workspace_members import validate_assignment
from mino_nexus.core.database import session_scope
from mino_nexus.platform.llm import dispatch_log

RULE_JOBS = {'map_cases', 'classify_fail', 'draft_sign', 'draft_gate', 'pick_regression'}


def rule_inputs(project_id, topic_id, job, target):
    from mino_nexus.resources import project_store as ps, app_automation as aa
    from mino_nexus.casework import run_store
    with session_scope() as db:
        topic = ws.public(ws.owned(db, project_id, topic_id))
    configs = [(a['id'], aa.get_automation_config(ps.require_app(a['id']), hydrate_cases=False))
               for a in ps.list_apps(project_id)]
    requirements = [(aid, r) for aid, cfg in configs for r in cfg['qa_process']['requirements']]
    releases = [(aid, r) for aid, cfg in configs for r in cfg['qa_process']['releases']]
    rid = target.get('requirement_id') or topic['requirement_id']
    req_pair = next(((aid, r) for aid, r in requirements if r['id'] == rid), None)
    rel_pair = next(((aid, r) for aid, r in releases if r['id'] == target.get('release_id')), None)
    if job in ('map_cases', 'draft_sign') and not req_pair:
        raise ValueError('请指定当前项目中的需求，再进行覆盖或验收分析')
    if job in ('draft_gate', 'pick_regression') and not rel_pair:
        raise ValueError('请指定当前项目中的版本，再进行回归或发布分析')
    if target.get('requirement_id') and not req_pair:
        raise ValueError('需求不属于当前项目')
    if target.get('release_id') and not rel_pair:
        raise ValueError('版本不属于当前项目')
    req = dict(req_pair[1]) if req_pair else None
    rel = dict(rel_pair[1]) if rel_pair else None
    if target.get('run_id'):
        doc = run_store.get(target['run_id'])
        if not doc or ps.require_app(doc['app_id']).get('project_id') != project_id:
            raise ValueError('运行不存在或不属于当前项目')
        if job != 'classify_fail':
            raise ValueError('run_id 仅用于失败归类；验收和发布分析读取需求或版本已关联的运行')
        req = {'runs': [{'kind': 'req_test', 'task_id': target['run_id']}]}
        rel = None
    if job == 'classify_fail' and not req and not rel:
        raise ValueError('请指定运行、需求或版本后再分析失败')
    entity = req if job in ('map_cases', 'draft_sign', 'classify_fail') and req else rel
    tasks = []
    for ref in (entity or {}).get('runs') or []:
        run_id = ref.get('task_id')
        doc = run_store.get(run_id) if run_id else None
        if doc and ps.require_app(doc['app_id']).get('project_id') == project_id:
            tasks.append({**doc, 'task_id': run_id, 'taskId': run_id})
    if any(t.get('status') not in ('done', 'finished', 'failed', 'cancelled', 'blocked', 'passed') for t in tasks):
        raise ValueError('运行尚未结束，请等待终态后再分析结果')
    if job == 'classify_fail' and not tasks:
        raise ValueError('目标尚无可读取的运行结果')
    aid = (rel_pair or req_pair or (topic['app_id'],))[0]
    config = next((cfg for app_id, cfg in configs if app_id == aid), {})
    return {'requirement': req, 'release': rel,
            'requirements': [r for app_id, r in requirements if app_id == aid],
            'cases': aa.list_project_cases(project_id), 'tasks': tasks, 'suites': config.get('suites', [])}


def rule_message(job, result, inputs):
    payload = result['payload']
    if job == 'map_cases':
        points = [p for p in (inputs.get('requirement', {}).get('understanding') or {}).get('points', []) if not p.get('waived')]
        if not points:
            return '需求尚未维护测试点，暂时无法判断覆盖；请先补齐测试点。匹配到的需求用例池：' + str(payload['pool_count']) + ' 条。'
        lines = [result['suggest'], f"需求用例池 {payload['pool_count']} 条；可建议关联 {len(payload['mappings'])} 个测试点；缺口 {len(payload['gaps'])} 个。"]
        lines.extend('待关联：' + str(m.get('point_text') or m.get('point_id')) + ' → ' + '、'.join(c['title'] for c in m['suggest']) for m in payload['mappings'])
        lines.extend('缺口：' + str(g.get('point_text') or g.get('point_id')) + '；' + g['reason'] for g in payload['gaps'])
        return '\n'.join(lines)
    if job == 'classify_fail':
        return '\n'.join([result['suggest'], *[str(i.get('title') or i.get('case_id')) + '：' + i['kind'] for i in payload['items']]])
    if job == 'pick_regression':
        return result['suggest'] + ('\n未验收需求：' + '、'.join(payload['unsigned']) if payload['unsigned'] else '')
    report = payload['report']
    return result['suggest'] + '\n' + '；'.join(label + ' ' + str(report[key]) for key, label in [('passed', '通过'), ('failed', '失败'), ('blocked', '阻塞')] if key in report)


def _role(member):
    from mino_nexus.platform.llm.repo import role_specs
    return next((r for r in role_specs() if r['id'] == member), {})


def assign(project_id, topic_id, sess, args, *, context, deadline, state=None):
    from mino_nexus.assistant.workspace.workspace_agent import bounded_json
    from mino_nexus.ai.roles_catalog import chat_with_role
    member, job = args.get('member'), args.get('job')
    spec = validate_assignment(project_id, topic_id, sess, member, job)
    target = args.get('target') or {}
    if not isinstance(target, dict) or set(target) - {'requirement_id', 'release_id', 'run_id'} or any(not isinstance(v, str) for v in target.values()):
        raise ValueError('任务目标只能包含需求、版本或运行的 id')
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ValueError('本轮处理时间已用完')
    trace = dispatch_log.bind(job=job, role=member, trigger='workspace_member', source='workspace_assign', engine=spec['engine'])
    try:
        if member == 'report-writer' and job == 'draft_test_report':
            from mino_nexus.assistant.workspace.workspace_reports import generate, markdown
            result = generate(project_id, topic_id, sess, str(target.get('run_id') or ''), deadline=deadline)
            body = markdown(result)
        elif spec['engine'] == 'rule':
            if job not in RULE_JOBS:
                raise ValueError('规则任务尚未接入业务输入合同')
            from mino_nexus.casework.qa.qa_process_assist import run_job
            inputs = rule_inputs(project_id, topic_id, job, target)
            result = run_job(job, **inputs)
            # Rule-produced explanation and payload are authoritative; never ask a model to recalculate them.
            dispatch_log.record_job(status='ok', job=job, role=member, input_data=target, output_data=result)
            body = rule_message(job, result, inputs)
            result['explanation'] = body
            result['needs_input'] = job == 'map_cases' and not [p for p in (inputs.get('requirement', {}).get('understanding') or {}).get('points', []) if not p.get('waived')]
        elif state is not None and (_role(member).get('tools')):
            from mino_nexus.assistant.workspace.workspace_agent import member_loop
            result = member_loop(project_id, topic_id, sess, member, job, args.get('question', ''), target, context, state, deadline)
            body = result['reply']
        else:
            result = chat_with_role(role_id=member, job_id=job, timeout_sec=remaining,
                messages=[{'role': 'user', 'content': bounded_json({'question': args.get('question', ''),
                    'target': target, 'workspace_context': context}, 7500)}])
            body = result['reply']
        with session_scope() as db:
            ws.message(db, ws.owned(db, project_id, topic_id, write=True), body, role=member)
        return {'member': member, 'job': job, 'result': result, 'needs_input': bool(result.get('needs_input')),
                'stopped': bool(result.get('stopped'))}
    finally:
        dispatch_log.reset(trace)
