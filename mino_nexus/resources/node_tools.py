"""Account-scoped asynchronous CLI installation, with durable uncertain dispatch."""
from __future__ import annotations

import inspect
import time
import uuid

from fastapi import HTTPException
from sqlalchemy import text

from mino_nexus.core import protocol as P
from mino_nexus.core.database import session_scope
from mino_nexus.models.node import Node
from mino_nexus.models.node_tool_task import NodeToolTask
from mino_nexus.resources.node_registry import get_registry

ACTIVE = {'pending', 'running', 'unknown'}
TERMINAL = {'succeeded', 'failed', 'interrupted'}
TOOLS = [('scout.core', 'Scout 核心', '节点运行'), ('chromium', 'Chromium', 'Web 自动化'),
         ('adb.keyboard', 'ADB Keyboard', 'Android 文本输入'), ('cli.feishu', '飞书 CLI', '工作项接入'),
         ('cli.meego', 'Meego CLI', '工作项接入'), ('langbot', 'LangBot', '飞书、钉钉、企业微信共用对话宿主')]


def _owner(node_id, user_id):
    with session_scope() as db:
        node = db.get(Node, node_id)
        if not user_id or node is None or node.owner_user_id != user_id:
            raise HTTPException(404, '没有这个节点')
    live = get_registry().get_node(node_id)
    if live and live.owner_user_id != user_id:
        raise HTTPException(404, '没有这个节点')
    return live


def _live(node_id, user_id):
    live = _owner(node_id, user_id)
    if not live or not live.alive or live.draining or live.send is None:
        raise HTTPException(409, '节点离线，不能安装')
    if live.tool_tasks_version not in (1, 2) or live.plugins_ready is not True:
        raise HTTPException(409, '节点尚未上报下载中心合同，请手动升级 Scout 或等待账号确认')
    return live


def _public(row):
    return {**dict(row.snapshot or {}), 'task_id': row.task_id, 'node_id': row.node_id,
            'operation_id': row.operation_id, 'tool_id': row.tool_id, 'action': row.action,
            'state': row.state, 'active': row.state in ACTIVE, 'created_at': row.created_at, 'updated_at': row.updated_at}


def read(node_id, user_id, task_id):
    _owner(node_id, user_id)
    with session_scope() as db:
        row = db.get(NodeToolTask, task_id)
        if not row or row.owner_user_id != user_id or row.node_id != node_id:
            raise HTTPException(404, '没有这个安装任务')
        return _public(row)


async def _rpc(live, capability, params):
    req = P.Execute(run_id='', step_idx=-1, sn='', capability_id=capability, params=params, timeout_sec=6)
    result = live.send(P.MsgType.EXECUTE, req, timeout=8)
    if inspect.isawaitable(result):
        result = await result
    if result is None:
        raise RuntimeError('node_no_response')
    status = getattr(getattr(result, 'status', None), 'value', None) or getattr(result, 'status', '')
    if str(status).lower() not in ('pass', 'passed', 'ok', 'success'):
        error = str(getattr(result, 'error', '') or '')
        raise RuntimeError(error if error in ('tool_busy', 'task_parameters_conflict', 'plugin_owner_unverified', 'unsupported_tool_action') else 'node_rejected')
    return {**dict(getattr(result, 'extra', None) or {}), **dict(getattr(result, 'data', None) or {})}


def _save(task, raw):
    if not isinstance(raw, dict) or raw.get('task_id') != task['task_id'] or raw.get('tool_id') != task['tool_id'] or raw.get('action') != task['action']:
        raise RuntimeError('task_receipt_mismatch')
    state = raw.get('state')
    if state not in ACTIVE | TERMINAL:
        raise RuntimeError('task_state_invalid')
    with session_scope() as db:
        db.execute(text('BEGIN IMMEDIATE'))
        row = db.get(NodeToolTask, task['task_id'])
        node = db.get(Node, task['node_id'])
        if node is None or node.owner_user_id != task['owner_user_id']:
            raise HTTPException(404, '没有这个节点')
        if row.owner_user_id != task['owner_user_id'] or row.node_id != task['node_id']:
            raise HTTPException(404, '没有这个任务')
        if row.state not in TERMINAL:
            row.state = state
            row.snapshot = {k: raw[k] for k in ('stage', 'label', 'percent', 'bytes_received', 'bytes_total', 'error') if k in raw}
            row.active_slot = row.active_slot if state in ACTIVE else None
            row.updated_at = time.time()
        return _public(row)


def _uncertain(task, error='节点应答未确认，只查询原任务，不自动重装'):
    with session_scope() as db:
        db.execute(text('BEGIN IMMEDIATE'))
        row = db.get(NodeToolTask, task['task_id'])
        if row.state in ACTIVE:
            row.state = 'unknown'
            row.snapshot = {**dict(row.snapshot or {}), 'error': error}
            row.updated_at = time.time()
        return _public(row)


async def refresh_task(node_id, user_id, task_id):
    task = read(node_id, user_id, task_id)
    if task['state'] not in ACTIVE:
        return task
    live = _owner(node_id, user_id)
    if not live or not live.alive or live.send is None or live.tool_tasks_version not in (1, 2):
        return task
    try:
        raw = await _rpc(live, 'node.tool_task_get', {'task_id': task_id})
        _owner(node_id, user_id)
        if raw.get('task') is None:
            return _uncertain(task, 'Scout 未找到原任务；只有显式重试同一请求才可能重新派发')
        return _save({**task, 'owner_user_id': user_id}, raw['task'])
    except HTTPException:
        raise
    except Exception:
        return _uncertain(task)


async def submit(node_id, user_id, operation_id, tool_id, action):
    live = _owner(node_id, user_id)
    if action not in {'install', 'update'} or tool_id not in {'cli.feishu', 'cli.meego', 'langbot', 'chromium', 'adb.keyboard'}:
        raise HTTPException(422, 'unsupported_tool_action')
    with session_scope() as db:
        db.execute(text('BEGIN IMMEDIATE'))
        row = db.query(NodeToolTask).filter_by(owner_user_id=user_id, operation_id=operation_id).first()
        created = row is None
        if row:
            if (row.node_id, row.tool_id, row.action) != (node_id, tool_id, action):
                raise HTTPException(409, 'operation_parameters_conflict')
            if row.state in TERMINAL or not live or not live.alive or live.draining or live.send is None or live.tool_tasks_version not in (1, 2) or live.plugins_ready is not True:
                return _public(row)
        else:
            live = _live(node_id, user_id)
            if live.tool_tasks_version < 2 and (action == 'update' or tool_id in {'chromium', 'adb.keyboard'}):
                raise HTTPException(409, '当前 Scout 仅支持旧版插件安装，请先升级到下载中心合同 v2')
            from mino_nexus.resources.scout_updates import check_admission
            check_admission(db, [node_id])
            slot = f'{user_id}:{node_id}:{tool_id}'
            if db.query(NodeToolTask).filter_by(active_slot=slot).first():
                raise HTTPException(409, 'tool_busy')
            now = time.time()
            row = NodeToolTask(task_id=uuid.uuid4().hex, owner_user_id=user_id, operation_id=operation_id,
                               node_id=node_id, tool_id=tool_id, action=action, active_slot=slot,
                               process_generation=live.scout_process_generation, state='pending', snapshot={}, created_at=now, updated_at=now)
            db.add(row)
            db.flush()
        task = {**_public(row), 'owner_user_id': user_id}
        generation = row.process_generation
    if task['state'] in TERMINAL:
        return read(node_id, user_id, task['task_id'])
    try:
        if not created:
            raw = await _rpc(live, 'node.tool_task_get', {'task_id': task['task_id']})
            if raw.get('task') is not None:
                return _save(task, raw['task'])
            # Only an explicit repeat POST, a definite not-found receipt, and the
            # same authenticated Scout process may dispatch the SAME task again.
            if not generation or generation != live.scout_process_generation:
                return _uncertain(task, '节点进程身份已变化；原安装结果仍需核对')
        current = _live(node_id, user_id)
        if current is not live:
            return _uncertain(task)
        raw = await _rpc(live, 'node.tool_task_submit', {'task_id': task['task_id'], 'tool_id': tool_id, 'action': action})
        _owner(node_id, user_id)
        return _save(task, raw.get('task'))
    except HTTPException:
        raise
    except RuntimeError as exc:
        if str(exc) in ('tool_busy', 'task_parameters_conflict', 'plugin_owner_unverified', 'unsupported_tool_action'):
            _save(task, {**task, 'state': 'failed', 'error': str(exc)})
            raise HTTPException(409, str(exc)) from exc
        return _uncertain(task)
    except Exception:
        return _uncertain(task)


def note_progress(node_id, user_id, progress):
    task_id = str(progress.get('task_id') or '')
    if not task_id:
        return
    with session_scope() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        row = db.get(NodeToolTask, task_id)
        if not row or row.owner_user_id != user_id or row.node_id != node_id or row.state not in ACTIVE:
            return
        progress_tool = progress.get('id') if progress.get('class') == 'tool' else 'langbot' if (progress.get('class'), progress.get('id')) == ('im', 'langbot') else f"{progress.get('class')}.{progress.get('id')}"
        if row.tool_id != progress_tool:
            return
        # Progress is not an authoritative final receipt, even at 100%.
        row.snapshot = {k: progress[k] for k in ('stage', 'label', 'percent', 'bytes_received', 'bytes_total') if k in progress}
        row.updated_at = time.time()


async def tools(node_id, user_id):
    live = _owner(node_id, user_id)
    supported = bool(live and live.tool_tasks_version in (1, 2))
    online = bool(live and live.alive and not live.draining and live.send)
    stale = True
    if online and supported:
        try:
            raw = await _rpc(live, 'node.tools', {})
            snapshot = raw.get('tools')
            if isinstance(snapshot, dict) and snapshot.get('version') in (1, 2) and isinstance(snapshot.get('items'), list):
                with session_scope() as db:
                    db.execute(text("BEGIN IMMEDIATE"))
                    node = db.get(Node, node_id)
                    if node.owner_user_id != user_id:
                        raise HTTPException(404, '没有这个节点')
                    node.extra = {**dict(node.extra or {}), 'tool_snapshot': {'owner_user_id': user_id, 'reported_at': time.time(), 'items': snapshot['items']}}
                stale = False
        except HTTPException:
            raise
        except Exception:
            pass
    with session_scope() as db:
        node = db.get(Node, node_id)
        snapshot = dict((node.extra or {}).get('tool_snapshot') or {})
        if snapshot.get('owner_user_id') != user_id:
            snapshot = {}
        tasks = [_public(r) for r in db.query(NodeToolTask).filter_by(owner_user_id=user_id, node_id=node_id).order_by(NodeToolTask.created_at.desc()).limit(20)]
        active = [_public(r) for r in db.query(NodeToolTask).filter_by(owner_user_id=user_id, node_id=node_id).filter(NodeToolTask.state.in_(ACTIVE))]
    for task in active:
        updated = await refresh_task(node_id, user_id, task['task_id'])
        tasks = [t for t in tasks if t['task_id'] != updated['task_id']]
        tasks.append(updated)
    items = snapshot.get('items') or [dict(tool_id=k, label=label, purpose=purpose, state='unknown', detail='节点尚未上报此状态，需要支持下载中心的新版本 Scout') for k, label, purpose in TOOLS]
    if online and not supported:
        items = [dict(tool_id=k, label=label, purpose=purpose, state='unknown', detail='当前 Scout 未上报下载中心合同，请手动升级') for k, label, purpose in TOOLS]
    return dict(node_id=node_id, online=online, supported=supported, contract_version=live.tool_tasks_version if live else 0, stale=stale, reported_at=snapshot.get('reported_at'), items=items, tasks=tasks)
