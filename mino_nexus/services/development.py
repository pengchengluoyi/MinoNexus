"""Application development preparation; no IDE/device I/O on Nexus."""
from __future__ import annotations

import re
import hashlib
import json
import uuid
import inspect
import asyncio
from urllib.parse import urlsplit
import time
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from mino_nexus.core.database import session_scope
from mino_nexus.core import protocol as P
from mino_nexus.models.development import DevelopmentBinding, DevelopmentOperation
from mino_nexus.routers.deps import require_project_write
from mino_nexus.routers.rRuntime import _node_row_for_session, _require_live_node
from mino_nexus.services.project_store import require_app


class Binding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    node_id: str = Field(default="", max_length=100)
    connection_id: str = Field(default="", max_length=80)
    connection_revision: int = Field(default=0, ge=0)
    project_path: str = Field(default="", max_length=2048)
    source_dir: str = Field(default="", max_length=2048)
    run_configuration: str = Field(default="", max_length=200)
    build_configuration: str = Field(default="", max_length=200)
    target_url: str = Field(default="", max_length=2048)
    expected_build: str = Field(default="", max_length=200)
    expected_instance_id: str = Field(default="", max_length=100)
    build_field: str = Field(default="build_id", max_length=200)
    service_log_path: str = Field(default="", max_length=2048)
    apk_path: str = Field(default="", max_length=2048)
    android_serial: str = Field(default="", max_length=200)
    apk_package: str = Field(default="", max_length=200)
    apk_version_code: str = Field(default="", max_length=50)
    execution_allowed: bool = False
    test_parameters_confirmed: bool = False


def authorize(app_id, sess):
    try:
        app = require_app(app_id)
    except KeyError:
        raise HTTPException(404, "应用不存在")
    require_project_write(app["project_id"], sess)
    return str(sess["user_id"])


def is_android_app(app_id):
    platforms = require_app(app_id).get("platforms") or []
    return "android" in (platforms.split(",") if isinstance(platforms, str) else platforms)


def binding_get(app_id, sess):
    authorize(app_id, sess)
    with session_scope() as db:
        row = db.get(DevelopmentBinding, app_id)
        if not row:
            return {"revision": 0, "config": Binding().model_dump(), "check": {}, "checked_revision": 0}
        _node_row_for_session(row.config["node_id"], sess=sess, studio_id="") if row.config.get("node_id") else None
        return {"revision": row.revision, "config": row.config, "check": row.check,
                "checked_revision": row.checked_revision}


def binding_save(app_id, sess, revision, binding):
    uid = authorize(app_id, sess)
    cfg = binding.model_dump()
    if binding.enabled:
        if not binding.project_path.startswith(("/", "\\")) or not binding.source_dir.startswith(("/", "\\")):
            # Windows execute-only support may use drive-qualified paths.
            import ntpath
            if not ntpath.isabs(binding.project_path) or not ntpath.isabs(binding.source_dir):
                raise HTTPException(422, "项目与源码目录必须为绝对路径")
        if not all([binding.node_id, binding.connection_id, binding.connection_revision, binding.project_path, binding.source_dir]):
            raise HTTPException(422, "请填写节点、已保存的连接、IDE 项目路径与应用源码目录")
        _node_row_for_session(binding.node_id, sess=sess, studio_id="")
    with session_scope() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        row = db.get(DevelopmentBinding, app_id)
        if not is_android_app(app_id):
            previous = row.config if row else {}
            fields = ("apk_path", "android_serial", "apk_package", "apk_version_code")
            if any(cfg.get(key) and cfg.get(key) != previous.get(key, "") for key in fields):
                raise HTTPException(422, "APK 安装参数仅适用于 Android 应用")
        if (row.revision if row else 0) != revision:
            raise HTTPException(409, "配置已被修改，请刷新后保存")
        active = db.query(DevelopmentOperation).filter_by(app_id=app_id).filter(DevelopmentOperation.slot.is_not(None)).first()
        if active:
            raise HTTPException(409, {"message": "准备操作尚未结束，请先处理原操作", "operation_id": active.operation_id})
        if not row:
            row = DevelopmentBinding(app_id=app_id, owner_user_id=uid, revision=0)
            db.add(row)
        row.owner_user_id = uid
        row.config, row.revision, row.check, row.checked_revision = cfg, revision + 1, {}, 0
    return binding_get(app_id, sess)


def serialize(row):
    return {"operation_id": row.operation_id, "app_id": row.app_id, "node_id": row.node_id,
            "action": row.action, "revision": row.revision, "state": row.state,
            "result": row.result, "created_at": row.created_at, "deadline": row.deadline}


def get_operation(app_id, opid, sess):
    uid = authorize(app_id, sess)
    with session_scope() as db:
        row = db.get(DevelopmentOperation, opid)
        if not row or row.app_id != app_id or row.owner_user_id != uid:
            raise HTTPException(404, "没有这个开发操作")
        if row.state in {"queued", "running"} and row.deadline < time.time():
            if row.state == "queued":
                row.state, row.slot = "failed", None
                row.result = {"reason": "排队期限已到，未派发"}
            else:
                row.state = "unknown"
                row.result = {"reason": "等待期限已到，未收到确定回执；不会重新派发"}
        return serialize(row)


def list_operations(app_id, sess):
    uid = authorize(app_id, sess)
    with session_scope() as db:
        query = db.query(DevelopmentOperation).filter_by(app_id=app_id, owner_user_id=uid)
        ids = list(dict.fromkeys([r.operation_id for r in query.filter(DevelopmentOperation.slot.is_not(None))] +
                   [r.operation_id for r in query.order_by(DevelopmentOperation.created_at.desc()).limit(20)]))
    return [get_operation(app_id, opid, sess) for opid in ids]


def create_operation(app_id, sess, opid, action, revision, connection=None):
    uid = authorize(app_id, sess)
    if action == "install" and not is_android_app(app_id):
        raise HTTPException(422, "只有 Android 应用可以安装 APK")
    saved = binding_get(app_id, sess)
    cfg = saved["config"]
    # Connection operations specify their node; binding is only saved after Scout acknowledges.
    if connection is not None:
        if action == "save_connection":
            spec = connection.get("connection") or {}
            if set(spec) - {"url", "transport", "type", "headers", "header_env"}:
                raise HTTPException(422, "不支持的 IDE 配置；不接受 command/args")
            headers = spec.get("headers") or {}
            if not isinstance(headers, dict) or any(k.lower() != "x-idea-project" for k in headers):
                raise HTTPException(422, "不接收明文认证 header；仅支持本机环境变量引用")
            env = spec.get("header_env") or {}
            if not isinstance(env, dict) or any(k.lower() not in {"authorization", "x-api-key"} or not isinstance(v, str) or not re.fullmatch(r"MINO_IDE_[A-Z0-9_]{1,100}", v) for k, v in env.items()):
                raise HTTPException(422, "认证仅允许引用 MINO_IDE_ 开头的本机环境变量名")
            if spec.get("transport", spec.get("type", "streamable-http")) not in {"http", "streamable-http", "sse"}:
                raise HTTPException(422, "不支持的 IDE 传输")
            u = urlsplit(str(spec.get("url", "")))
            if u.scheme not in {"http", "https"} or u.hostname not in {"localhost", "127.0.0.1", "::1"} or u.username or u.password or u.query or u.fragment:
                raise HTTPException(422, "IDE 地址必须是无凭据的本机回环 URL")
        node_id = connection.pop("node_id")
        params = {"action": action, "operation_id": opid, **connection}
    else:
        if revision != saved["revision"] or not cfg["enabled"]:
            raise HTTPException(409, "开发配置未启用或 revision 已变化")
        node_id = cfg["node_id"]
        params = {"action": action, "operation_id": opid, "binding": cfg}
    node_row = _node_row_for_session(node_id, sess=sess, studio_id="")
    if node_row.get("owner_user_id") != uid:
        raise HTTPException(403, "必须先将节点认领到当前账号")
    with session_scope() as db:
        previous = db.get(DevelopmentOperation, opid)
        if previous:
            if (previous.owner_user_id, previous.app_id, previous.params, previous.revision) != (uid, app_id, params, revision):
                raise HTTPException(409, "operation_id 已用于不同参数")
            return serialize(previous), False
    live = _require_live_node(node_id, sess, "")
    caps = getattr(live, "runtime_capabilities", None) or []
    if not any(c.get("id") == "node.ide_prepare" and c.get("contract_version") == 1 for c in caps):
        raise HTTPException(409, {"reason_code": "unsupported", "message": "当前运行的 Scout 不支持开发准备 v1，请先更新节点"})
    mutation = action in {"execute", "restart", "build", "install"}
    if mutation:
        if action == "install":
            device = live.devices.get(cfg.get("android_serial", ""))
            if not device or device.platform != "android" or device.channels.get("adb") not in {"connected", "online", "available"}:
                raise HTTPException(409, "APK 安装只支持当前 Scout 已登记且 ADB 在线的同节点设备")
        if not cfg["execution_allowed"] or not cfg["test_parameters_confirmed"]:
            raise HTTPException(403, "需要应用执行授权和测试参数确认")
        if saved["checked_revision"] != revision or not saved["check"].get("project_matched"):
            raise HTTPException(409, "请先检查当前 revision 的连接和项目")
    try:
        with session_scope() as db:
            db.execute(text("BEGIN IMMEDIATE"))
            from mino_nexus.services.scout_updates import check_admission
            check_admission(db, [node_id], task_id=opid)
            if action == "save_connection" and db.query(DevelopmentOperation).filter_by(slot=node_id).first():
                raise HTTPException(409, "节点正在准备或结果未确认，暂不能更改连接")
            if connection is None:
                current = db.get(DevelopmentBinding, app_id)
                if not current or current.revision != revision:
                    raise HTTPException(409, "配置已变化，请刷新")
            row = DevelopmentOperation(operation_id=opid, app_id=app_id, owner_user_id=uid,
                node_id=node_id, action=action, revision=revision, slot=node_id if mutation else None,
                state="queued", params=params, result={}, created_at=time.time(), deadline=time.time() + 120)
            db.add(row)
            db.flush()
            result = serialize(row)
    except IntegrityError:
        with session_scope() as db:
            previous = db.get(DevelopmentOperation, opid)
            if previous and (previous.owner_user_id, previous.app_id, previous.params, previous.revision) == (uid, app_id, params, revision):
                return serialize(previous), False
            active = db.query(DevelopmentOperation).filter_by(slot=node_id, owner_user_id=uid).first()
            detail = {"message": "节点已有活动或结果未确认的开发操作，请读取原操作"}
            if active:
                detail["operation_id"] = active.operation_id
        raise HTTPException(409, detail)
    return result, True


async def dispatch(opid, sess):
    pending = _claim_dispatch(opid, sess)
    if pending is not None:
        return await _execute_dispatch(opid, sess, pending)


def _claim_dispatch(opid, sess):
    with session_scope() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        row = db.get(DevelopmentOperation, opid)
        if not row or row.state != "queued":
            return
        node_id, params, app_id, revision = row.node_id, row.params, row.app_id, row.revision
        if row.deadline < time.time():
            row.state, row.slot, row.result = "failed", None, {"reason": "排队期限已到，未派发"}
            return
        if row.action in {"execute", "restart", "build", "install"}:
            from mino_nexus.services.node_workload import workload_for_node_id
            workload = workload_for_node_id(node_id) or {}
            if workload.get("busy") or workload.get("active_runs") or any(d.get("busy") for d in workload.get("devices", [])):
                row.result = {"reason": "等待当前节点的测试和设备操作结束"}
                return
        from mino_nexus.services.scout_updates import hold
        blocker = hold(db, node_id)
        if blocker:
            row.result = {"reason_code": "blocked_by_update", "operation_id": blocker.operation_id}
            return
        updated = db.query(DevelopmentOperation).filter_by(operation_id=opid, state="queued").update({"state": "running"})
        if not updated:
            return
    return node_id, params, app_id, revision


async def _execute_dispatch(opid, sess, pending):
    node_id, params, app_id, revision = pending
    sent = False
    try:
        uid = authorize(app_id, sess)
        owner = _node_row_for_session(node_id, sess=sess, studio_id="")
        if owner.get("owner_user_id") != uid:
            raise HTTPException(403, "节点归属已变更，未派发")
        live = _require_live_node(node_id, sess, "")
        if params["action"] == "install":
            device = live.devices.get(params["binding"].get("android_serial", ""))
            if not device or device.platform != "android" or device.channels.get("adb") not in {"connected", "online", "available"}:
                raise HTTPException(409, "安装前设备已离线或不属于该 Scout")
        payload = P.Execute(run_id="", step_idx=-1, sn="", capability_id="node.ide_prepare", params=params,
            timeout_sec=100, executor_order=["ide"], low_level={},
            selected_impl={"id": "ide.prepare.v1", "executor": "ide", "runs_on": "scout", "params_contract": {"version": 1}, "result_contract": {"version": 1}})
        sent = True
        result = live.send(P.MsgType.EXECUTE, payload, timeout=110)
        if inspect.isawaitable(result):
            result = await result
        raw = getattr(result, "extra", {}) or {}
        data = raw.get("development") or (getattr(result, "data", {}) or {}).get("development")
        if not isinstance(data, dict) or data.get("state") not in {"ready", "failed", "awaiting_developer", "unknown"}:
            data = {"state": "unknown", "reason": "节点未返回可确认的开发操作回执；不会自动重试"}
    except asyncio.CancelledError:
        if asyncio.current_task().cancelling():
            raise  # Shutdown leaves the persisted running record for expiry/recovery.
        data = {"state": "unknown", "reason": "节点连接断开，执行结果尚未确认；请读取原回执，不会重新执行"}
    except Exception:
        data = {"state": "unknown" if sent else "failed", "reason": "节点离线或传输未确认；请读取原操作并核对目标" if sent else "派发前权限或节点检查失败；未执行"}
    with session_scope() as db:
        row = db.get(DevelopmentOperation, opid)
        # A late receipt is evidence, not a license to replace a frozen unknown result.
        if row.state == "unknown":
            row.result = {**(row.result or {}), "late_receipt": data}
            return
        row.state, row.result = data["state"], data
        if row.state != "unknown":
            row.slot = None
        binding = db.get(DevelopmentBinding, app_id)
        if row.action == "check" and binding and binding.revision == revision:
            binding.check = data
            binding.checked_revision = revision if data.get("project_matched") else 0


async def recover_operation(app_id, opid, sess):
    uid = authorize(app_id, sess)
    with session_scope() as db:
        row = db.get(DevelopmentOperation, opid)
        if not row or row.app_id != app_id or row.owner_user_id != uid:
            raise HTTPException(404, "操作不存在")
        if row.state not in {"unknown", "running"}:
            return serialize(row)
        node_id, original = row.node_id, row.params
    node = _node_row_for_session(node_id, sess=sess, studio_id="")
    if node.get("owner_user_id") != uid:
        raise HTTPException(403, "节点归属已变更，不能读取原回执")
    live = _require_live_node(node_id, sess, "")
    if not any(c.get("id") == "node.ide_prepare" and "receipt" in c.get("features", [])
               for c in (getattr(live, "runtime_capabilities", None) or [])):
        raise HTTPException(409, "此 Scout 尚不支持原回执读取，请更新或人工核对")
    payload = P.Execute(run_id="", step_idx=-1, sn="", capability_id="node.ide_prepare",
        params={"operation_id": uuid.uuid4().hex, "action": "receipt", "target_operation_id": opid,
                "target_digest": hashlib.sha256(json.dumps(original, sort_keys=True).encode()).hexdigest()},
        timeout_sec=10, executor_order=["ide"], low_level={},
        selected_impl={"id": "ide.prepare.v1", "executor": "ide", "runs_on": "scout",
                       "params_contract": {"version": 1}, "result_contract": {"version": 1}})
    try:
        response = live.send(P.MsgType.EXECUTE, payload, timeout=12)
        if inspect.isawaitable(response):
            response = await response
        data = (getattr(response, "extra", {}) or {}).get("development")
    except Exception:
        raise HTTPException(503, "原回执暂不可读取；没有重新执行")
    if not isinstance(data, dict):
        raise HTTPException(502, "节点未返回原回执")
    with session_scope() as db:
        row = db.get(DevelopmentOperation, opid)
        if row.state in {"unknown", "running"}:
            history = {"previous_state": row.state, "at": time.time(), "source": "scout_durable_receipt"}
            if data.get("receipt_verified") is True and data.get("receipt_operation_id") == opid and data.get("state") in {"ready", "failed", "awaiting_developer"}:
                row.result = {**data, "recovery": history}
                row.state, row.slot = data["state"], None
            else:
                row.result = {**(row.result or {}), "recovery": history, "receipt_lookup": data, "stage": data.get("stage"), "stages": data.get("stages", [])}
        return serialize(row)


def acknowledge(app_id, opid, sess):
    # Explicit manual acknowledgement records uncertainty; it never changes historical success.
    uid = authorize(app_id, sess)
    with session_scope() as db:
        row = db.get(DevelopmentOperation, opid)
        if not row or row.app_id != app_id or row.owner_user_id != uid:
            raise HTTPException(404, "操作不存在")
        if row.state != "unknown" or row.deadline > time.time():
            raise HTTPException(409, "仅能处置已超过等待期限的未知操作")
        row.result = {**(row.result or {}), "manual_review": {"user_id": uid, "at": time.time(), "confirmed_idle": True}}
        row.slot = None
        return serialize(row)


async def worker():
    from mino_nexus.services.auth_store import api_session_for
    from mino_nexus.core.execution_user import execution_user
    active = {}
    try:
        while True:
            try:
                with session_scope() as db:
                    expired = db.query(DevelopmentOperation).filter(DevelopmentOperation.state.in_(["queued", "running"]), DevelopmentOperation.deadline < time.time()).all()
                    for row in expired:
                        if row.state == "queued":
                            row.state, row.slot, row.result = "failed", None, {"reason": "排队期限已到，未派发"}
                        else:
                            row.state, row.result = "unknown", {**(row.result or {}), "reason": "操作等待已到期，请读取原回执；不会重新执行"}
                    rows = [(r.operation_id, r.owner_user_id) for r in db.query(DevelopmentOperation)
                            .filter_by(state="queued").order_by(DevelopmentOperation.created_at).limit(16)]
                for opid, uid in rows:
                    sess = api_session_for(uid)
                    if not sess:
                        with session_scope() as db:
                            row = db.get(DevelopmentOperation, opid)
                            if row and row.state == "queued":
                                row.state, row.slot, row.result = "failed", None, {"reason": "执行账号已不可用"}
                        continue
                    if opid in active:
                        continue
                    if len(active) >= 16:
                        break
                    with execution_user(uid):
                        active[opid] = asyncio.create_task(dispatch(opid, sess))
                for opid, task in list(active.items()):
                    if task.done():
                        active.pop(opid)
                        if not task.cancelled():
                            task.result()
            except Exception as exc:
                from mino_nexus.core.log import SLog
                SLog.w("Development", "prepare worker: " + type(exc).__name__)
            await asyncio.sleep(1)
    finally:
        for task in active.values():
            task.cancel()
        await asyncio.gather(*active.values(), return_exceptions=True)
