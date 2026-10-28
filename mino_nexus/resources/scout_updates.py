"""Fixed-build Scout updates; hold only new admissions, never existing steps."""
from __future__ import annotations

import asyncio
import inspect
import time

from fastapi import HTTPException
from sqlalchemy import text

from mino_nexus.core.database import session_scope
from mino_nexus.core import protocol as P
from mino_nexus.models.scout_update import ScoutUpdate


TERMINAL = {"succeeded", "failed", "rolled_back", "cancelled", "expired"}


def public(row):
    return {"operation_id": row.operation_id, "node_id": row.node_id,
            "state": row.state, "status": row.state, "params": row.params,
            "result": row.result, "created_at": row.created_at,
            "deadline": row.deadline, "update_hold": row.slot is not None,
            "running_version_verified": row.state == "succeeded"}


def hold(db, node_id):
    row = db.query(ScoutUpdate).filter_by(slot=node_id).first()
    # The caller holds BEGIN IMMEDIATE: timeout, cancellation and install compete
    # for the same durable row. Never expire an already dispatched operation.
    if row and row.state == "waiting_idle" and row.deadline <= time.time():
        row.state, row.slot = "expired", None
        row.result = {"reason": "等待空闲超过截止时间，未派发更新"}
        db.flush()
        return None
    return row


def check_admission(db, node_ids, *, task_id=""):
    for nid in sorted(set(node_ids)):
        row = hold(db, nid)
        if row:
            raise HTTPException(409, {"reason_code": "blocked_by_update",
                "operation_id": row.operation_id, "node_id": nid,
                "task_id": task_id, "message": "节点更新门禁生效，请查询原更新操作"})


def _supports(node):
    return bool(node and node.alive and any(c.get("id") == "node.update" and
        c.get("contract_version") == 1 for c in (node.runtime_capabilities or [])))


def request(node_id, owner, opid, params):
    # A Nexus hold blocks new work, not lookup of an existing idempotent request.
    with session_scope() as db:
        previous = db.get(ScoutUpdate, opid)
        if previous:
            if (previous.node_id, previous.owner_user_id, previous.params) != (node_id, owner, params):
                raise HTTPException(409, "operation_id 已用于不同目标或参数")
            return public(previous)
    return _request_new(node_id, owner, opid, params)


def _request_new(node_id, owner, opid, params):
    from mino_nexus.resources.node_registry import get_registry
    # Registry registration persists to SQLite while holding its own lock. Read
    # the node before acquiring the DB writer, never invert those lock orders.
    live = get_registry().get_node(node_id)
    with session_scope() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        previous = db.get(ScoutUpdate, opid)
        if previous:
            if (previous.node_id, previous.owner_user_id, previous.params) != (node_id, owner, params):
                raise HTTPException(409, "operation_id 已用于不同目标或参数")
            return public(previous)
        if not live or live.owner_user_id != owner:
            raise HTTPException(403, "节点不属于当前执行账号")
        if not _supports(live):
            raise HTTPException(409, {"reason_code": "unsupported", "message": "当前 Scout 未声明固定版本更新合同 v1"})
        check_admission(db, [node_id])
        row = ScoutUpdate(operation_id=opid, node_id=node_id, owner_user_id=owner,
            slot=node_id, state="waiting_idle", params=params, result={},
            created_at=time.time(), deadline=time.time() + params["wait_seconds"])
        db.add(row)
        db.flush()
        return public(row)


def get(node_id, opid=""):
    with session_scope() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        hold(db, node_id)
        query = db.query(ScoutUpdate).filter_by(node_id=node_id)
        row = query.filter_by(operation_id=opid).first() if opid else query.order_by(ScoutUpdate.created_at.desc()).first()
        if opid and not row:
            raise HTTPException(404, "没有这个节点的更新操作")
        return public(row) if row else None


def cancel(node_id, opid):
    with session_scope() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        hold(db, node_id)
        row = db.get(ScoutUpdate, opid)
        if not row or row.node_id != node_id:
            raise HTTPException(404, "没有这个更新操作")
        if row.state == "cancelled":
            return public(row)
        if row.state != "waiting_idle":
            raise HTTPException(409, "仅能取消尚未派发的等待操作；安装不能强行终止")
        row.state, row.slot = "cancelled", None
        return public(row)


def busy(db, node_id):
    from mino_nexus.models.run import AppRegressionRun
    from mino_nexus.models.device_reservation import DeviceReservation
    from mino_nexus.resources.device_admission import _recorded_target
    # Include unresolved reservations even if their batch is already terminal.
    reserved = {r.run_id for r in db.query(DeviceReservation)}
    rows = db.query(AppRegressionRun).filter(
        (AppRegressionRun.status == "running") | AppRegressionRun.run_id.in_(reserved))
    for run in rows:
        payload = run.payload or {}
        if any(_recorded_target(db, payload, sn).get("node_id") == node_id
               for sn in payload.get("sns") or [payload.get("sn") or run.sn]):
            return True
    # Unknown IDE effects remain blocking until the existing review path resolves them.
    from mino_nexus.models.node_tool_task import NodeToolTask
    return db.query(NodeToolTask).filter_by(node_id=node_id).filter(NodeToolTask.active_slot.is_not(None)).first() is not None


async def _send(live, action, opid, params=None):
    payload = P.Execute(run_id="", step_idx=-1, sn="", capability_id="node.update",
        params={"action": action, "operation_id": opid, **(params or {})},
        timeout_sec=20, executor_order=["core"], low_level={},
        selected_impl={"executor": "core", "runs_on": "scout",
                       "params_contract": {"version": 1}, "result_contract": {"version": 1}})
    result = live.send(P.MsgType.EXECUTE, payload, timeout=25)
    if inspect.isawaitable(result):
        result = await result
    return (getattr(result, "extra", {}) or {}).get("update")


async def tick(opid):
    from mino_nexus.resources.node_registry import get_registry
    with session_scope() as db:
        snapshot = db.get(ScoutUpdate, opid)
        node_id = snapshot.node_id if snapshot else ""
    live = get_registry().get_node(node_id) if node_id else None
    with session_scope() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        row = db.get(ScoutUpdate, opid)
        if not row or row.state in TERMINAL:
            return
        if row.state == "waiting_idle" and hold(db, row.node_id) is None:
            return
        if not _supports(live) or live.owner_user_id != row.owner_user_id:
            return
        op_params = dict(row.params)
        if row.state == "waiting_idle":
            if live.draining or busy(db, row.node_id):
                return
            # This persisted handoff is written BEFORE sending. After a crash only
            # query the original op, never submit an uncertain installation again.
            row.state, row.dispatched_at = "installing", time.time()
            action = "submit"
        else:
            action = "status"
    try:
        receipt = await _send(live, action, opid, op_params if action == "submit" else None)
    except Exception:
        return  # last confirmed state survives disconnect; hold remains
    if not isinstance(receipt, dict) or receipt.get("operation_id") != opid:
        return
    with session_scope() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        row = db.get(ScoutUpdate, opid)
        if not row or row.state in TERMINAL:
            return
        state = receipt.get("state")
        if state == "not_found":
            row.state = "needs_operator"
            row.result = {"reason": "执行端没有此操作回执；派发结果未知，不自动重发"}
        elif state == "busy" and action == "submit":
            row.state, row.dispatched_at = "waiting_idle", None
            row.result = receipt
        elif state in {"installing", "restarting", "verifying", "needs_operator", "failed", "rolled_back", "succeeded"}:
            # Executor validates loaded build; a file version on disk is insufficient.
            if state == "succeeded" and not (receipt.get("running_build_id") == row.params["build_id"]
                    and receipt.get("running_version_verified") is True):
                row.state, row.result = "needs_operator", {"reason": "运行 build 核验不匹配", "receipt": receipt}
                return
            row.state, row.result = state, receipt
            if state in TERMINAL:
                row.slot = None


async def worker():
    while True:
        try:
            with session_scope() as db:
                ids = [r.operation_id for r in db.query(ScoutUpdate).filter(ScoutUpdate.slot.is_not(None))]
            # Independent nodes must not be serialized behind an offline node.
            for offset in range(0, len(ids), 8):
                await asyncio.gather(*(tick(opid) for opid in ids[offset:offset + 8]))
        except Exception as exc:
            from mino_nexus.core.log import SLog
            SLog.w("ScoutUpdate", type(exc).__name__)
        await asyncio.sleep(2)
