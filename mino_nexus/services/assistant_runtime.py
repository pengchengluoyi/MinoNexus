"""不依赖聊天正文的追问续接与控制指令。"""
from __future__ import annotations
import re
from typing import Any
from mino_nexus.core.execution_user import require_user_id
from mino_nexus.services import assistant_conversation as conversation
from mino_nexus.services.assistant_cards import build_card, render_wechat_text
from mino_nexus.services.assistant_tools import ToolOutcome, get_tool, run_tool


def _task_id(outcome: ToolOutcome | None) -> str:
    if not outcome or not outcome.ok or outcome.tool_id not in ('case.run', 'case.retry_failed', 'app.explore'):
        return ''
    data = outcome.data
    return str(data if isinstance(data, str) else (data.get('task_id') or data.get('run_id') or '') if isinstance(data, dict) else '')



def _target_items(args: dict) -> list[list[str]]:
    from mino_nexus.services.project_store import require_app, require_project, project_env
    items = [['任务', str(args['task_id'])]] if args.get('task_id') else []
    try:
        app = require_app(str(args['app_id'])) if args.get('app_id') else None
        pid = str((app or {}).get('project_id') or args.get('project_id') or '')
        if pid:
            project = require_project(pid)
            items.append(['项目', str(project.get('name') or pid)])
        if app:
            items.append(['应用', str(app.get('name') or app['id'])])
        if pid and args.get('env_surface'):
            doc = project_env(pid)
            channel = next((row for row in doc.get('channels') or [] if row['id'] == args['env_surface']), {})
            items.append(['应用渠道', str(channel.get('alias') or channel.get('label') or args['env_surface'])])
        if args.get('sn'):
            items.append(['设备', str(args['sn'])])
        if args.get('modules') or args.get('module'):
            items.append(['测试模块', '、'.join(args.get('modules') or [str(args['module'])])])
        if args.get('case_ids'):
            if len(args['case_ids']) == 1 and pid:
                from mino_nexus.services.case_store import get_case
                case = get_case(pid, str(args['case_ids'][0])) or {}
                items.append(['用例', str(case.get('name') or args['case_ids'][0])])
            else:
                items.append(['测试范围', str(len(args['case_ids'])) + ' 条用例'])
        elif args.get('ordinal'):
            items.append(['测试范围', '第 ' + str(args['ordinal']) + ' 条用例'])
    except (ValueError, KeyError, PermissionError):
        pass
    return items

def _input_hint(question: dict) -> str:
    if question.get('candidates'):
        return '回复选项序号或名称后继续；回复“取消本次请求”可撤销。'
    label = {'options.simple-names': '空间链接或别名', 'options.project-key': '空间链接或空间标识',
             'options.name': '标题'}.get(question.get('field'), '需要补充的信息')
    return f'直接回复{label}即可继续；回复“取消本次请求”可撤销。'


def result_for(*, answer: str, origin: dict, outcome: ToolOutcome | None = None, steps: list | None = None,
               target: dict | None = None, usage: dict | None = None, model_error: str = '', request: dict | None = None, runtime_stop: str = '') -> dict:
    from mino_nexus.services.assistant_errors import explain
    question = bool(outcome and outcome.ask)
    failed = bool(runtime_stop or model_error or (outcome and not outcome.ok and not outcome.ask))
    task_id = _task_id(outcome) if outcome and outcome.tool_id in ('case.run', 'case.retry_failed', 'app.explore') else ''
    card_type = 'clarification' if question else 'notification' if outcome or failed else 'dialogue'
    uncertain = bool(failed and outcome and outcome.status >= 500 and (request or {}).get('state') == 'uncertain')
    status = 'need_input' if question else 'uncertain' if uncertain else 'blocked' if failed else 'submitted' if task_id else 'done'
    title = '请补充信息' if question else '受理结果待确认' if uncertain else '任务已提交' if task_id else '尚未执行' if failed else '查询结果' if outcome else '助手回复'
    issue, suggestions = '', []
    if failed:
        issue, suggestions = explain(answer, outcome.data if outcome else None)
        answer = '本次请求未完成。' if model_error or (outcome and not outcome.tool_id.endswith(('run', 'explore'))) else '本次测试尚未开始。'
    if runtime_stop:
        title, answer = '请求未完成', '本次请求未完成，尚未提交新的执行任务。'
        issue = ('这次请求的处理时间已用完，不能再启动新的操作。' if runtime_stop == 'deadline'
                 else '相同查询反复返回相同结果，暂时无法确定下一步。')
        suggestions = ['请明确要操作的应用、模块或任务；已经查询到的结果列在下面。' if outcome and outcome.ok
                       else '请补充要操作的应用、模块或任务，再继续处理。']
        if outcome and outcome.tool_id in ('cli.query', 'cli.execute'):
            answer = '本次请求未完成，尚未提交新的 CLI 写入操作。'
            suggestions = ['请明确飞书或 Meego 的目标空间、文档或工作项；不要重复提交尚未确认结果的写操作。']
    if uncertain:
        answer = '本次操作是否已受理暂时无法确认。'
        issue = '服务未返回可确认的结果，当前不会重复提交。'
        suggestions = ['先到任务页查询是否已经创建任务，确认后再决定下一步。']
        if outcome and outcome.tool_id == 'cli.execute':
            title = '写入结果待确认'
            suggestions = ['先查询飞书或 Meego 中是否已经写入，确认后再决定下一步。']
    if question and not request:
        if outcome.args.get('intent') == 'execute_test' and outcome.tool_id in ('case.list', 'device.list', 'app.list'):
            outcome.tool_id = 'case.run'
        request = conversation.create(outcome.tool_id, outcome.args, target or {},
                                      {'field': outcome.field, 'candidates': outcome.candidates}, origin)
    if task_id:
        answer = '执行请求已受理，任务完成后会通知你。'
        if not request:
            request = conversation.create(outcome.tool_id, outcome.args, target or {}, {}, origin)
            from mino_nexus.core.database import session_scope
            from mino_nexus.models.assistant_conversation import AssistantRequest
            with session_scope() as db:
                row = db.get(AssistantRequest, request['id'])
                row.state, row.task_id, row.question = 'submitted', task_id, {}
            request['state'], request['task_id'], request['question'] = 'submitted', task_id, {}
        conversation.focus(origin, request_id=request['id'], task_id=task_id)
    preferences = conversation.preferences(require_user_id())
    card = build_card(template_id='answer', title=title, summary=answer, status=status,
                      card_type=card_type, problem=issue, suggestions=suggestions,
                      steps=steps if preferences.get('process') == 'show' else None)
    target_args = (outcome.args if outcome and outcome.args else (request or {}).get('args') or target or {})
    target_items = _target_items(target_args) if outcome else []
    if target_items:
        card['blocks'].insert(1, {'type': 'kv', 'items': target_items})
    if runtime_stop and outcome and outcome.ok and outcome.summary:
        card['blocks'].append({'type': 'kv', 'items': [['已查询到的结果', outcome.summary]]})
    if task_id:
        card['blocks'].append({'type': 'kv', 'items': [['任务', task_id]]})
    data = outcome.data if outcome and isinstance(outcome.data, dict) else {}
    preflight = data.get('preflight') or {}
    detail = data.get('detail') if isinstance(data.get('detail'), dict) else {}
    blocked = preflight.get('blocked') or data.get('blocked') or detail.get('blocked') or []
    if blocked:
        from mino_nexus.services.case_preflight import blocked_message
        from mino_nexus.services.assistant_reports import configuration, case_edit_url

        heading = '以下用例没有执行' if task_id else '以下用例未通过预检'
        card['blocks'].append({'type': 'problem', 'text': heading + '：\n' + blocked_message(blocked)})
        base = configuration(require_user_id())['effective_url']
        if base:
            for case in blocked:
                url = case_edit_url(str(target_args.get('app_id') or ''), str(case.get('case_id') or ''), base_url=base)
                if url:
                    card['actions'].append({'id': 'edit_' + str(case['case_id']),
                                            'label': '编辑用例 ' + str(case['case_id']), 'url': url})
        else:
            card['blocks'].append({'type': 'next_steps', 'items': ['请在 Workbench 对话设置中配置可访问的地址；也可按上述用例编号进入用例页编辑。']})
        if task_id:
            card['blocks'][0]['text'] = f"已受理 {len(preflight.get('runnable') or [])} 条可执行用例；另有 {len(blocked)} 条没有执行。任务完成后会通知你。"
    if request:
        card['request'] = {key: request[key] for key in ('id', 'revision', 'state', 'expires_at', 'question')}
        if question:
            request_question = request.get('question') or {}
            candidates = request_question.get('candidates') or []
            if candidates:
                card['blocks'].append({'type': 'choices', 'items': candidates})
            card['blocks'].append({'type': 'next_steps', 'items': [_input_hint(request_question)]})
    return {'answer': answer, 'intent': ' → '.join(str(step.get('label') or step.get('tool')) for step in steps or []),
            'text': render_wechat_text(card), 'card': card, 'steps': steps or [], 'usage': usage or {}, 'model_error': model_error,
            'request_id': (request or {}).get('id', ''), 'task_id': task_id, 'runtime_stop': runtime_stop,
            'app_id': (outcome.args if outcome else {}).get('app_id', '')}


def continue_request(request_id: str, revision: int, patch: dict, *, origin: dict | None = None) -> dict:
    from mino_nexus.ai import dispatch_log
    from mino_nexus.services import assistant_store
    from mino_nexus.services.assistant_envelope import lane_for
    uid = require_user_id()
    from mino_nexus.models.assistant_conversation import AssistantRequest
    from mino_nexus.core.database import session_scope
    with session_scope() as db:
        saved = db.get(AssistantRequest, request_id)
        if saved is None or saved.user_id != uid:
            raise ValueError('请求不存在或不属于当前账号')
        saved_origin = dict(saved.origin or {})
    if saved_origin.get('channel'):
        assistant_store.require_im_binding(uid, saved_origin['channel'], saved_origin.get('node_id', ''), saved_origin)
    request = conversation.claim(request_id, revision, lane=lane_for(uid, origin) if origin else '', patch=patch)
    return execute_request(request)


def execute_request(request: dict) -> dict:
    from mino_nexus.ai import dispatch_log
    from mino_nexus.services import assistant_store
    uid = require_user_id()
    saved_origin = request['_origin']
    request_id = request['id']
    if saved_origin.get('channel'):
        assistant_store.require_im_binding(uid, saved_origin['channel'], saved_origin.get('node_id', ''), saved_origin)
    policy = conversation.group_policy(saved_origin) if saved_origin.get('chat_type') == 'group' else None
    token = dispatch_log.bind(assistant_mutation_scope=request_id, assistant_group_policy=policy,
                              assistant_origin=saved_origin)
    try:
        spec = get_tool(request['tool_id'])
        if spec is None:
            outcome = ToolOutcome(ok=False, summary='这个工具已经不可用，请重新发起请求。', args=request['args'])
        else:
            outcome = run_tool(spec, request['args'], user_id=uid, target=request['target'])
        request = conversation.finish(request_id, request['revision'], outcome, task_id=_task_id(outcome))
    except Exception:
        outcome = ToolOutcome(ok=False, status=503, summary='操作结果暂时无法确认，请先查询任务状态。', args=request['args'])
        request = conversation.finish(request_id, request['revision'], outcome)
    finally:
        dispatch_log.reset(token)
    task_id = _task_id(outcome)
    if task_id and saved_origin.get('channel'):
        assistant_store.add_subscription({'user_id': uid, 'kind': 'run_done', 'target_id': task_id,
            'channel': saved_origin['channel'], 'chat_id': saved_origin['chat_id'], 'sender_id': saved_origin.get('sender_id', ''),
            'node_id': saved_origin.get('node_id', ''), 'reply_ctx': saved_origin})
    return result_for(answer=outcome.ask or outcome.summary, outcome=outcome, origin=saved_origin, request=request,
                      steps=[{'tool': outcome.tool_id, 'label': (spec or {}).get('title') or outcome.tool_id,
                              'status': 'ok' if outcome.ok else 'ask' if outcome.ask else 'fail', 'ms': outcome.elapsed_ms}])


def route_control(text: str, origin: dict, inbound: dict) -> dict | None:
    raw = text.strip()
    reply_to = str(inbound.get('reply_to') or '')
    pending = conversation.pending_for(origin, reply_to=reply_to)
    if inbound.get('event_type') == 'button':
        callback = inbound.get('callback') if isinstance(inbound.get('callback'), dict) else {}
        selected = conversation.pending_for(origin, request_id=str(callback.get('request_id') or ''))
        if not selected:
            return result_for(answer='这个按钮已失效或不属于当前会话，请查看最新请求。', origin=origin)
        try:
            return continue_request(selected['id'], int(callback.get('revision') or 0),
                                    {selected['question']['field']: callback.get('value')}, origin=origin)
        except ValueError:
            return result_for(answer='这个选择已失效，请查看最新请求。', origin=origin)
    if inbound.get('event_type') in ('edit', 'retract'):
        changed = conversation.cancel_source(origin, str(inbound.get('target_message_id') or inbound.get('reply_to') or ''))
        return result_for(answer='这条消息关联的待执行请求已撤销。已提交任务会继续；停止时请指定任务。' if changed else '没有找到这条消息关联的待执行请求，其他请求保持原状态。', origin=origin)
    if pending and pending['tool_id'] == 'case.run':
        replacement = re.fullmatch(r'把(.+?)换(?:成|为)?(.+?)(?:模块)?', raw)
        union = ('都测' in raw or '都测试' in raw) and any(word in raw for word in ('和', '、', '及'))
        if replacement or union:
            scope_info = conversation.available_modules(pending['id'])
            available = scope_info['items']
            desired = replacement.group(2) if replacement else re.sub(r'(都测试|都测)(?:一下)?$', '', raw)
            names = [re.sub(r'模块$', '', value.strip()) for value in re.split(r'[、,，]|和|及', desired)]
            known = {item['name'] for item in available}
            names = names if set(names).issubset(known) else []
            if names and replacement:
                old_name = re.sub(r'模块$', '', replacement.group(1).strip())
                current = scope_info.get('selected') or []
                if old_name not in current or len(names) != 1:
                    return result_for(answer='当前请求不包含要替换的模块，范围尚未修改。请在 Workbench 检查当前选择。', origin=origin)
                names = list(dict.fromkeys(names[0] if value == old_name else value for value in current))
            if names:
                revised = conversation.revise_scope(pending['id'], pending['revision'], names)
                return execute_request(revised)
            return result_for(answer='没有找到明确的模块，请在 Workbench 选择当前项目已有的范围。', origin=origin)
    if raw in ('取消本次请求' , '/取消请求'):
        answer = '没有等待补充信息的请求。'
        if pending and conversation.cancel(pending['id'], pending['revision']):
            answer = '本次待执行请求已取消。'
        return result_for(answer=answer, origin=origin)
    if raw in ('好的', '好', '收到', '谢谢', 'ok', 'OK', '嗯'):
        return result_for(answer=_input_hint(pending.get('question') or {}) if pending else '好的。', origin=origin)
    if raw in ('/帮助', '/help') or re.search(r'(平台|Mino|Workbench).*(怎么用|如何使用|有什么功能)', raw, re.I):
        return result_for(answer='可以查询项目和任务，也可以按应用与模块发起测试。\n\n例如：测试造好物的登录模块。\n\n缺少应用渠道或设备时，我会只列出当前可用的选项；任务受理后会单独通知结果。\n\nWorkbench 的“我的助手”可以管理待执行请求、展示偏好、账号绑定和群策略。', origin=origin)
    if raw in ('你好', '您好', '早上好', '晚上好', 'hi', 'hello'):
        return result_for(answer='你好，有什么需要我帮忙的？', origin=origin)
    match = re.fullmatch(r'(?:继续|/继续)\s+(req-[a-f0-9]{32})', raw)
    if match:
        from mino_nexus.services.assistant_store import require_im_binding
        require_im_binding(require_user_id(), origin['channel'], origin['node_id'], origin)
        candidate = next((row for row in conversation.list_requests(require_user_id())['items'] if row['id'] == match[1]), None)
        if not candidate:
            return result_for(answer='请求不存在或不属于当前账号。', origin=origin)
        request = conversation.transfer(candidate['id'], candidate['revision'], origin)
        outcome = ToolOutcome(ok=False, ask='已将待补充请求转到当前会话，请补充所需信息。', field=request['question'].get('field', ''))
        return result_for(answer=outcome.ask, origin=origin, outcome=outcome, request=request)
    if raw in ('继续', '继续本次请求', '/继续'):
        if pending:
            outcome = ToolOutcome(ok=False, ask='请补充本次请求所需的信息。', summary='',
                                  field=pending['question'].get('field', ''), candidates=pending['question'].get('candidates', []),
                                  data=pending.get('result') or {}, args=pending.get('args') or {}, tool_id=pending.get('tool_id') or '')
            return result_for(answer=outcome.ask, outcome=outcome, origin=origin, request=pending)
        return result_for(answer='当前没有待补充的请求。可以查询任务进度，或说明要测试的应用和模块。', origin=origin)
    match = re.fullmatch(r'(?:切换到|关注任务|/关注)\s*(\S+)', raw)
    if match:
        outcome = run_tool(get_tool('task.get'), {'task_id': match[1]}, user_id=require_user_id())
        if not outcome.ok:
            return result_for(answer=outcome.summary, outcome=outcome, origin=origin)
        conversation.focus(origin, task_id=match[1])
        return result_for(answer=f'当前关注任务已切换为 {match[1]}。其他任务继续执行。', origin=origin)
    if raw in ('停止当前任务', '取消当前任务', '查询当前任务', '当前任务进度'):
        task = conversation.get_focus(origin).get('task_id')
        if not task:
            return result_for(answer='当前没有关注的任务，请指定任务编号。', origin=origin)
        spec = get_tool('task.cancel' if raw.startswith(('停止', '取消')) else 'task.get')
        outcome = run_tool(spec, {'task_id': task}, user_id=require_user_id())
        return result_for(answer=outcome.summary, outcome=outcome, origin=origin)
    if raw.startswith(('申请权限', '/申请权限')):
        return result_for(answer='请在 Workbench 中由有管理权限的账号设置资源或群策略。聊天中的“同意”不能授予平台权限。', origin=origin)
    if raw in ('以后回复简短一点', '以后简洁回复'):
        conversation.set_preference(require_user_id(), 'detail', 'brief')
        return result_for(answer='已记住简洁回复偏好。回复“撤销简洁回复”可恢复。', origin=origin)
    if raw == '撤销简洁回复':
        conversation.set_preference(require_user_id(), 'detail', None)
        return result_for(answer='已撤销简洁回复偏好。', origin=origin)
    if raw.isdigit() and pending and not (0 < int(raw) <= len(pending['question'].get('candidates') or [])):
        outcome = ToolOutcome(ok=False, ask='这个序号不在本次选项中，请选择下面的有效选项。')
        return result_for(answer=outcome.ask, origin=origin, outcome=outcome, request=pending)
    if raw.isdigit() or reply_to:
        if not pending:
            return result_for(answer='没有找到这条回复对应的有效追问。请回复最新追问，或明确要执行的任务。', origin=origin)
    return None
