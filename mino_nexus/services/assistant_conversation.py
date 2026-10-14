"""请求、焦点、偏好和群策略。只有规范业务参数持久化。"""
from __future__ import annotations
import time
import hashlib
import json
import uuid
from typing import Any
from sqlalchemy import or_
from mino_nexus.core.database import session_scope
from mino_nexus.core.execution_user import require_user_id
from mino_nexus.models.assistant_conversation import (
    AssistantRequest as Pending, AssistantFocus as Focus, AssistantPreference as Preference,
    AssistantGroupPolicy as Group, AssistantMessageReference as Reference,
)
from mino_nexus.services.assistant_envelope import lane_for

ARG_KEYS = {'project_id', 'app_id', 'env_surface', 'env_profile', 'sn', 'case_ids', 'module',
            'search', 'ordinal', 'modules', 'intent', 'scope', 'scope_revision', 'task_id', 'limit', 'offset',
            'action_scheme', 'playwright_headless', 'all_cases'}
DISPLAY_PREFERENCES = {'detail': {'brief', 'full'}, 'language': {'zh-CN', 'en-US'}, 'process': {'show', 'hide'}}
TERMINAL = {'submitted', 'cancelled', 'expired', 'failed', 'uncertain', 'done'}


def _now() -> int:
    return int(time.time())


def _view(row: Pending) -> dict[str, Any]:
    return {key: getattr(row, key) for key in ('id', 'lane', 'tool_id', 'args', 'target', 'question', 'state',
                                              'revision', 'task_id', 'created_at', 'updated_at', 'expires_at', 'result')}


def origin_for(channel: str, chat_id: str, node_id: str, ctx: dict[str, Any]) -> dict[str, str]:
    return {'channel': channel, 'chat_id': chat_id, 'node_id': node_id,
            **{key: str(ctx.get(key) or '') for key in ('instance_id', 'tenant', 'sender_id', 'thread_id', 'source_message_id', 'binding_key', 'chat_type', 'bot_uuid', 'context_token', 'target_type', 'target_id')}}


def expire(db, uid: str) -> None:
    db.query(Pending).filter(Pending.user_id == uid, Pending.state == 'resolving', Pending.updated_at < _now() - 3600).update(
        {Pending.state: 'uncertain', Pending.revision: Pending.revision + 1}, synchronize_session=False)
    db.query(Pending).filter(Pending.user_id == uid, Pending.state == 'need_input', Pending.expires_at <= _now()).update(
        {Pending.state: 'expired', Pending.revision: Pending.revision + 1}, synchronize_session=False)


def create(tool_id: str, args: dict, target: dict, question: dict, origin: dict, *, state: str = 'need_input') -> dict:
    uid, now = require_user_id(), _now()
    lane = lane_for(uid, origin)
    with session_scope() as db:
        expire(db, uid)
        row = Pending(id='req-' + uuid.uuid4().hex, user_id=uid, lane=lane, tool_id=tool_id,
                      args={key: value for key, value in args.items() if key in ARG_KEYS},
                      target={key: value for key, value in target.items() if key in ARG_KEYS},
                      question=question, origin=origin, state=state, revision=1,
                      created_at=now, updated_at=now, expires_at=now + 1800)
        db.add(row)
        db.flush()
        if state == 'need_input' or tool_id in ('case.run', 'case.retry_failed', 'app.explore'):
            _set_focus(db, uid, lane, row.id)
        source_id = str(origin.get('source_message_id') or '')
        if source_id:
            ref_id = 'source-' + hashlib.sha256((lane + ':' + source_id).encode()).hexdigest()
            if db.get(Reference, ref_id) is None:
                db.add(Reference(id=ref_id, user_id=uid, lane=lane, request_id=row.id, revision=1,
                                 provider_message_id=source_id, state='source', created_at=now))
        return {**_view(row), '_origin': dict(row.origin or {})}


def _set_focus(db, uid: str, lane: str, request_id: str | None = None, task_id: str | None = None) -> None:
    from sqlalchemy.dialects.sqlite import insert
    values = {'generation': Focus.generation}
    if request_id is not None:
        values['request_id'] = request_id
    if task_id is not None:
        values['task_id'] = task_id
    db.execute(insert(Focus).values(user_id=uid, lane=lane, request_id=request_id or '', task_id=task_id or '', generation=1)
               .on_conflict_do_update(index_elements=['user_id', 'lane'], set_=values))


def focus(origin: dict, request_id: str | None = None, task_id: str | None = None) -> None:
    uid = require_user_id()
    with session_scope() as db:
        if request_id:
            row = db.get(Pending, request_id)
            if row is None or row.user_id != uid:
                raise ValueError('请求不存在')
        if task_id:
            from mino_nexus.services.run_store import get
            if not get(task_id):
                raise ValueError('任务不存在或无权访问')
        _set_focus(db, uid, lane_for(uid, origin), request_id, task_id)


def get_focus(origin: dict) -> dict:
    uid = require_user_id()
    with session_scope() as db:
        row = db.query(Focus).filter_by(user_id=uid, lane=lane_for(uid, origin)).first()
        return {'request_id': row.request_id, 'task_id': row.task_id, 'generation': row.generation} if row else {}


def list_requests(user_id: str) -> dict:
    from mino_nexus.services.assistant_store import mask_id
    with session_scope() as db:
        expire(db, user_id)
        rows = db.query(Pending).filter_by(user_id=user_id).order_by(Pending.updated_at.desc(), Pending.id.desc()).limit(100).all()
        focuses = db.query(Focus).filter_by(user_id=user_id).all()
        items = [{**_view(row), 'origin': {'channel': row.origin.get('channel'),
                  'chat_id_masked': mask_id(row.origin.get('chat_id') or '')}} for row in rows]
        from mino_nexus.core.execution_user import execution_user
        from mino_nexus.services.run_store import get
        with execution_user(user_id):
            for item in items:
                from mino_nexus.services.project_store import require_app, project_env
                try:
                    app = require_app(str(item['args'].get('app_id') or ''))
                    channels = project_env(app['project_id']).get('channels') or []
                    channel = next((row for row in channels if row['id'] == item['args'].get('env_surface')), {})
                    item['display'] = {'app': app.get('name') or app['id'], 'channel': channel.get('alias') or channel.get('label') or item['args'].get('env_surface') or ''}
                except (ValueError, KeyError):
                    item['display'] = {}
                if item['task_id']:
                    task = get(item['task_id']) or {}
                    item['task_status'] = task.get('status') or 'unknown'
        return {'items': items, 'focuses': [{'lane': row.lane, 'request_id': row.request_id, 'task_id': row.task_id} for row in focuses]}


def pending_for(origin: dict, request_id: str = '', reply_to: str = '') -> dict | None:
    uid, lane = require_user_id(), lane_for(require_user_id(), origin)
    with session_scope() as db:
        expire(db, uid)
        if reply_to:
            ref = db.query(Reference).filter(Reference.user_id == uid, Reference.lane == lane, or_(Reference.provider_message_id == reply_to, Reference.id == reply_to)).first()
            if ref is None:
                return None
            request_id = ref.request_id
        if request_id:
            row = db.get(Pending, request_id)
            if row is None or row.user_id != uid or row.lane != lane:
                return None
        else:
            current = db.query(Focus).filter_by(user_id=uid, lane=lane).first()
            row = db.get(Pending, current.request_id) if current and current.request_id else None
        return {**_view(row), '_origin': row.origin} if row is not None and row.lane == lane and row.state == 'need_input' and (not reply_to or ref.revision == row.revision) else None


def claim(request_id: str, revision: int, *, lane: str = '', patch: dict | None = None) -> dict:
    uid, now = require_user_id(), _now()
    with session_scope() as db:
        row = db.get(Pending, request_id)
        if row is None or row.user_id != uid or (lane and row.lane != lane):
            raise ValueError('请求不存在或不属于当前会话')
        if row.state != 'need_input' or row.revision != revision or row.expires_at <= now:
            raise ValueError('这个选择已使用、已过期或内容已变化，请查看最新请求')
        field = (row.question or {}).get('field')
        patch = patch or {}
        if set(patch) != {field}:
            raise ValueError('请只补充本次追问要求的参数')
        candidates = (row.question or {}).get('candidates') or []
        if candidates and patch[field] not in [item['value'] for item in candidates]:
            raise ValueError('该选择不在原候选列表中，请查看最新请求')
        if field == 'execution_target':
            if not candidates:
                raise ValueError('执行目标缺少有效候选，请重新选择')
            selection = json.loads(patch[field])
            if not isinstance(selection, dict) or set(selection) != {'env_surface', 'env_profile', 'sn'}:
                raise ValueError('执行目标格式已变化，请重新选择')
            patch = selection
        updated = db.query(Pending).filter(Pending.id == request_id, Pending.user_id == uid,
                    Pending.revision == revision, Pending.state == 'need_input', Pending.expires_at > now).update(
            {Pending.state: 'resolving', Pending.updated_at: now,
             Pending.args: {**(row.args or {}), **{key: value for key, value in patch.items() if key in ARG_KEYS}}}, synchronize_session=False)
        if updated != 1:
            raise ValueError('请求已被另一条回复处理')
        db.refresh(row)
        return {**_view(row), '_origin': dict(row.origin or {})}


def reuse_execution_args(origin: dict, args: dict) -> dict:
    from mino_nexus.services.assistant_tools import _resolve_one

    out = dict(args)
    app_id, _, _, _ = _resolve_one('app.by_name', str(out.get('app_id') or ''), user_id=require_user_id(), scope=out)
    if not app_id:
        return out
    out['app_id'] = app_id
    focus = get_focus(origin)
    with session_scope() as db:
        previous = db.get(Pending, focus.get('request_id') or '')
        if previous is None or previous.user_id != require_user_id() or previous.lane != lane_for(require_user_id(), origin):
            return out
        if previous.tool_id != 'case.run' or previous.expires_at <= _now() or previous.state not in {'failed', 'need_input'}:
            return out
        saved = previous.args or {}
        if saved.get('app_id') != app_id or any(out.get(key) and out[key] != saved.get(key) for key in ('env_surface', 'env_profile')):
            return out
        for key in ('env_surface', 'env_profile', 'sn'):
            if saved.get(key):
                out.setdefault(key, saved[key])
    return out


def finish(request_id: str, revision: int, outcome, *, task_id: str = '') -> dict:
    uid = require_user_id()
    with session_scope() as db:
        row = db.get(Pending, request_id)
        if row is None or row.user_id != uid or row.revision != revision or row.state != 'resolving':
            raise ValueError('请求状态已变化，旧结果不可继续执行')
        state = 'need_input' if outcome.ask else ('submitted' if task_id else 'done') if outcome.ok else 'uncertain' if outcome.status >= 500 else 'failed'
        row.state, row.updated_at, row.task_id = state, _now(), task_id
        row.revision += 1
        if outcome.args:
            row.args = {key: value for key, value in outcome.args.items() if key in ARG_KEYS}
        row.question = {'field': outcome.field, 'candidates': outcome.candidates} if outcome.ask else {}
        preflight = outcome.data.get('preflight') if isinstance(outcome.data, dict) else None
        if not outcome.ok and not outcome.ask:
            from mino_nexus.services.assistant_errors import explain
            issue, advice = explain(outcome.summary, outcome.data)
            row.result = {'problem': issue[:1800], 'next_steps': advice, **({'preflight': preflight} if preflight else {})}
        else:
            row.result = {'preflight': preflight} if preflight else {}
        if task_id:
            _set_focus(db, uid, row.lane, row.id, task_id)
        return _view(row)


def cancel(request_id: str, revision: int) -> bool:
    with session_scope() as db:
        changed = db.query(Pending).filter(Pending.id == request_id, Pending.user_id == require_user_id(),
            Pending.revision == revision, Pending.state == 'need_input').update(
                {Pending.state: 'cancelled', Pending.revision: Pending.revision + 1, Pending.updated_at: _now()}, synchronize_session=False)
        return changed == 1


def preferences(uid: str) -> dict[str, str]:
    with session_scope() as db:
        return {row.key: row.value for row in db.query(Preference).filter_by(user_id=uid).all()}


def set_preference(uid: str, key: str, value: str | None) -> dict:
    if key not in DISPLAY_PREFERENCES or (value is not None and value not in DISPLAY_PREFERENCES[key]):
        raise ValueError('只支持明确、可撤销的展示偏好')
    with session_scope() as db:
        row = db.query(Preference).filter_by(user_id=uid, key=key).first()
        if value is None:
            if row:
                db.delete(row)
        elif row:
            row.value, row.updated_at = value, _now()
        else:
            db.add(Preference(user_id=uid, key=key, value=value, updated_at=_now()))
    return preferences(uid)


def groups(uid: str) -> list[dict]:
    with session_scope() as db:
        return [{key: getattr(row, key) for key in ('id', 'channel', 'instance_id', 'tenant', 'chat_id', 'enabled',
                'allow_execute', 'disclose_results', 'project_ids')} for row in db.query(Group).filter_by(user_id=uid).all()]


def save_group(uid: str, policy: dict) -> dict:
    from mino_nexus.services.assistant_store import list_identities
    if not any(row['channel'] == policy['channel'] and row['instance_id'] == policy['instance_id']
               and row['tenant'] == policy.get('tenant', '') for row in list_identities(uid)):
        raise ValueError('群策略必须关联当前账号已经验证的机器人实例')
    from mino_nexus.services.project_store import require_project
    for project_id in policy.get('project_ids') or []:
        require_project(project_id)
    identity = {key: policy.get(key, '') for key in ('channel', 'instance_id', 'tenant', 'chat_id')}
    with session_scope() as db:
        row = db.query(Group).filter_by(user_id=uid, **identity).first()
        if row is None:
            row = Group(user_id=uid, **identity)
            db.add(row)
        for key in ('enabled', 'allow_execute', 'disclose_results', 'project_ids'):
            setattr(row, key, policy.get(key, [] if key == 'project_ids' else False))
        row.updated_at = _now()
        db.flush()
        return {'id': row.id}


def group_policy(origin: dict) -> dict:
    return next((row for row in groups(require_user_id()) if all(row[key] == origin.get(key, '') for key in
                ('channel', 'instance_id', 'tenant', 'chat_id'))), {})


def reference(origin: dict, request_id: str = '', revision: int = 0, *, message_id: str = '', provider_id: str = '') -> str:
    message_id = message_id or 'im-out-' + uuid.uuid4().hex
    with session_scope() as db:
        if db.get(Reference, message_id) is None:
            db.add(Reference(id=message_id, user_id=require_user_id(), lane=lane_for(require_user_id(), origin),
                         request_id=request_id, revision=revision, provider_message_id=provider_id, state='done', created_at=_now()))
    return message_id


def delivered(message_id: str, provider_id: str = '', state: str = 'done') -> None:
    with session_scope() as db:
        db.query(Reference).filter_by(id=message_id, user_id=require_user_id()).update(
            {Reference.provider_message_id: provider_id, Reference.state: state}, synchronize_session=False)


def begin(origin: dict) -> int:
    uid, lane = require_user_id(), lane_for(require_user_id(), origin)
    with session_scope() as db:
        row = db.query(Focus).filter_by(user_id=uid, lane=lane).first()
        if row:
            row.generation += 1
        else:
            row = Focus(user_id=uid, lane=lane, request_id='', task_id='', generation=1)
            db.add(row)
        db.flush()
        return row.generation


def require_generation(origin: dict, generation: int) -> None:
    if get_focus(origin).get('generation') != generation:
        raise PermissionError('会话已被新消息打断，旧解析结果不再执行')



def transfer(request_id: str, revision: int, origin: dict) -> dict:
    uid = require_user_id()
    lane = lane_for(uid, origin)
    with session_scope() as db:
        changed = db.query(Pending).filter(Pending.id == request_id, Pending.user_id == uid,
                    Pending.revision == revision, Pending.state == 'need_input', Pending.expires_at > _now()).update(
            {Pending.lane: lane, Pending.revision: Pending.revision + 1, Pending.updated_at: _now()}, synchronize_session=False)
        if changed != 1:
            raise ValueError('请求已变化、已使用或已过期')
        _set_focus(db, uid, lane, request_id)
        row = db.get(Pending, request_id)
        return _view(row)



def revise_scope(request_id: str, revision: int, modules: list[str]) -> dict:
    uid = require_user_id()
    with session_scope() as db:
        row = db.get(Pending, request_id)
        if row is None or row.user_id != uid or row.tool_id != 'case.run':
            raise ValueError('这不是可调整范围的测试请求')
        if row.state != 'need_input' or row.revision != revision or row.expires_at <= _now():
            raise ValueError('请求已提交、已处理或已过期，不能修改正在运行的任务')
        from mino_nexus.services.assistant_store import require_im_binding
        origin = row.origin or {}
        if origin.get('channel'):
            require_im_binding(uid, origin['channel'], origin.get('node_id', ''), origin)
        from mino_nexus.services.project_store import require_app
        from mino_nexus.services.case_store import case_modules
        app = require_app(str((row.args or {}).get('app_id') or ''))
        available = {item['name'] for item in case_modules(app['project_id'])['items']}
        if not modules or not set(modules).issubset(available):
            raise ValueError('请选择当前项目已有的测试模块')
        args = {key: value for key, value in (row.args or {}).items() if key not in ('case_ids', 'scope_revision', 'ordinal', 'module', 'modules', 'search', 'all_cases')}
        target = {key: value for key, value in (row.target or {}).items() if key not in ('case_ids', 'scope_revision', 'ordinal', 'module', 'modules', 'search', 'all_cases')}
        changed = db.query(Pending).filter(Pending.id == request_id, Pending.user_id == uid, Pending.revision == revision,
                    Pending.state == 'need_input').update({Pending.state: 'resolving', Pending.revision: revision + 1,
                    Pending.args: {**args, 'modules': list(dict.fromkeys(modules))}, Pending.target: target,
                    Pending.question: {}, Pending.updated_at: _now()}, synchronize_session=False)
        if changed != 1:
            raise ValueError('请求已被另一条消息处理')
        db.refresh(row)
        return {**_view(row), '_origin': row.origin}


def available_modules(request_id: str) -> dict:
    from mino_nexus.services.project_store import require_app
    from mino_nexus.services.case_store import case_modules
    with session_scope() as db:
        row = db.get(Pending, request_id)
        if row is None or row.user_id != require_user_id():
            raise ValueError('请求不存在')
        app = require_app(str((row.args or {}).get('app_id') or ''))
        result = case_modules(app['project_id'])
        args = row.args or {}
        selected = list(args.get('modules') or ([args['module']] if args.get('module') else []))
        if not selected and args.get('case_ids'):
            from mino_nexus.models.case import ProjectCase
            cases = db.query(ProjectCase).filter(ProjectCase.project_id == app['project_id'], ProjectCase.case_id.in_(args['case_ids'])).all()
            selected = sorted({str(case.module or '').strip() or '未分组' for case in cases})
        return {**result, 'selected': selected}


def cancel_source(origin: dict, source_id: str) -> bool:
    if not source_id:
        return False
    uid, lane = require_user_id(), lane_for(require_user_id(), origin)
    with session_scope() as db:
        rows = db.query(Pending).filter_by(user_id=uid, lane=lane, state='need_input').all()
        row = next((item for item in rows if (item.origin or {}).get('source_message_id') == source_id), None)
        if row is None:
            return False
        changed = db.query(Pending).filter(Pending.id == row.id, Pending.user_id == uid, Pending.revision == row.revision,
                                           Pending.state == 'need_input').update({Pending.state: 'cancelled', Pending.revision: row.revision + 1,
                                                                                Pending.updated_at: _now()}, synchronize_session=False)
        return changed == 1


def focus_request(request_id: str) -> dict:
    from mino_nexus.services.run_store import get
    uid = require_user_id()
    with session_scope() as db:
        row = db.get(Pending, request_id)
        if row is None or row.user_id != uid or not row.task_id or not get(row.task_id):
            raise ValueError('任务不存在或无权访问')
        _set_focus(db, uid, row.lane, task_id=row.task_id)
        return {'task_id': row.task_id}
