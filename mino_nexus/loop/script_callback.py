"""Scout 执行脚本时，账号、步骤、失败和证据回调本仓。验证码和模型在 Scout 本机完成。"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from mino_nexus.core import protocol as P
from mino_nexus.core.log import SLog

TAG = "ScriptCallback"
CALLBACKS = frozenset({
    "case.account", "case.note", "case.step", "case.fail",
    "account", "note", "step", "fail",
})
STEP_CAP = "script.step"
_ALIASES = {
    "account": "case.account",
    "note": "case.note",
    "step": "case.step",
    "fail": "case.fail",
}

_GUARD = threading.Lock()
_ADMISSIONS: dict[str, "ScriptAdmission"] = {}


@dataclass
class ScriptAdmission:
    run_id: str
    session_id: str
    case_id: str
    sn: str
    node_id: str
    project_id: str
    app_id: str
    provider_id: str
    owner_user_id: str
    platform: str
    case: dict[str, Any]
    ctx: Any
    writer: Any
    steps: list[dict[str, Any]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)


def register(admission: ScriptAdmission) -> None:
    with _GUARD:
        _ADMISSIONS[admission.run_id] = admission


def unregister(run_id: str) -> None:
    with _GUARD:
        _ADMISSIONS.pop(str(run_id or ""), None)


def handle(node_id: str, req: P.Execute) -> P.Result:
    cap = str(req.capability_id or "")
    params = dict(req.params or {})
    if cap == "script.callback":
        cap = str(params.get("op") or "")
    cap = _ALIASES.get(cap, cap)
    try:
        admission = _require(node_id, params)
        with admission.lock:
            data = _call(admission, cap, params)
        summary = str(data.pop("_summary", "") or "ok")[:300]
        return P.Result(
            run_id=str(req.run_id or admission.run_id),
            step_idx=int(req.step_idx),
            status=P.EventStatus.PASS,
            summary=summary,
            executor_used="nexus",
            data=data,
        )
    except Exception as exc:
        code = str(getattr(exc, "code", "") or getattr(exc, "reason_code", "") or "")
        message = str(exc)[:300] or code or "callback_rejected"
        SLog.w(TAG, f"callback {cap} run={params.get('run_id')} rejected: {code or type(exc).__name__}")
        return P.Result(
            run_id=str(req.run_id or params.get("run_id") or ""),
            step_idx=int(req.step_idx),
            status=P.EventStatus.FAIL,
            summary=message,
            error=code or type(exc).__name__,
            executor_used="nexus",
        )


def _require(node_id: str, params: dict[str, Any]) -> ScriptAdmission:
    run_id = str(params.get("run_id") or "").strip()
    session_id = str(params.get("session_id") or "").strip()
    with _GUARD:
        admission = _ADMISSIONS.get(run_id)
    if admission is None or not run_id or not session_id:
        raise PermissionError("这次运行没有准入")
    if session_id != admission.session_id:
        raise PermissionError("session_id 与这次运行不一致")
    if str(node_id or "") != admission.node_id:
        raise PermissionError("调用方不是这台设备所在节点")
    from mino_nexus.casework import run_store
    from mino_nexus.resources.node_registry import get_registry

    if not run_store.task_is_live(run_store.get(run_id)):
        raise PermissionError("这次运行已经结束")
    node, reason = get_registry().resolve(admission.sn)
    if node is None or str(node.node_id) != str(node_id or ""):
        raise PermissionError(reason or "设备所在节点已变化")
    return admission


def _call(admission: ScriptAdmission, cap: str, params: dict[str, Any]) -> dict[str, Any]:
    from mino_nexus.contract.ownership import execution_user

    owner = admission.owner_user_id
    if cap == STEP_CAP:
        _write_step(admission, params)
        return {"_summary": "已写入步骤"}
    if not owner:
        raise PermissionError("这次运行没有执行账号")
    with execution_user(owner):
        if cap == "case.account":
            return _account(admission)
        if cap == "case.note":
            return _note(admission, params)
        if cap == "case.step":
            return _step(admission, params)
        if cap == "case.fail":
            return _fail(admission, params)
    raise PermissionError("Nexus 不处理这个回调")


def _account(admission: ScriptAdmission) -> dict[str, Any]:
    from mino_nexus.loop.script_runtime import ScriptAbort
    from mino_nexus.resources.accounts.lease import lease_script_account

    row = dict(getattr(admission.ctx, "picked_account", None) or {})
    if not str(row.get("id") or row.get("account_id") or "").strip():
        leased, err = lease_script_account(admission.ctx, admission.case)
        row = dict(leased or getattr(admission.ctx, "picked_account", None) or {})
        if not str(row.get("id") or row.get("account_id") or "").strip():
            raise ScriptAbort(err or "这条用例没有租到账号", reason_code="resource_account_missing")
    return {
        "_summary": "已返回本次租到的账号",
        "id": str(row.get("id") or row.get("account_id") or ""),
        "email": str(row.get("email") or ""),
        "phone": str(row.get("phone") or ""),
        "username": str(row.get("username") or ""),
        "password": str(row.get("password") or ""),
        "otp": str(row.get("otp") or ""),
        "ident": str(row.get("ident") or ""),
    }


def _step_text(case: dict[str, Any], role: str, number: int) -> str:
    from mino_nexus.contract.step_pointer import build_seq_nodes
    from mino_nexus.loop.script_runtime import _prep_lines

    if role == "prep":
        return str(_prep_lines(case).get(number) or "")
    for node in build_seq_nodes(case):
        if node.n == number:
            return str(node.instruction if role == "do" else node.expected or "")
    return ""


def _step(admission: ScriptAdmission, params: dict[str, Any]) -> dict[str, Any]:
    from mino_nexus.loop.script_runtime import ScriptAbort

    action = str(params.get("action") or "")
    role = str(params.get("role") or "")
    try:
        number = int(params.get("number"))
    except (TypeError, ValueError) as exc:
        raise ScriptAbort("步骤编号无效", reason_code="script_step_missing") from exc
    text = _step_text(admission.case, role, number)
    writer = admission.writer
    if action == "enter" and writer is not None:
        if hasattr(writer, "set_turn"):
            writer.set_turn(number)
        writer.append("node/enter", {"n": number, "role": role, "text": text, "case_step": number})
    elif action == "leave" and writer is not None:
        status = "fail" if params.get("failed") else "pass"
        writer.append("node/leave", {"n": number, "role": role, "status": status, "case_step": number, "text": text})
    elif action not in {"enter", "leave"}:
        raise ScriptAbort("步骤动作无效", reason_code="script_step_missing")
    return {"_summary": text or str(number)}


def _fail(admission: ScriptAdmission, params: dict[str, Any]) -> dict[str, Any]:
    message = str(params.get("message") or "脚本判定失败")[:300]
    if admission.writer is not None:
        admission.writer.append("tool/result", {
            "capability_id": "fail",
            "status": "fail",
            "summary": message,
            "error": message,
            "executor_used": "scout",
            "source": "script_run",
        })
    return {"_summary": message}


def _note(admission: ScriptAdmission, params: dict[str, Any]) -> dict[str, Any]:
    from mino_nexus.loop.script_runtime import _note_fields

    body = str(params.get("text") or "")[:500]
    fields = {key: value for key, value in params.items() if key not in {"run_id", "session_id", "text"}}
    payload = {
        "name": "note",
        "summary": body[:300],
        "status": "pass",
        **_note_fields(fields),
        "text": body,
    }
    if admission.writer is not None:
        admission.writer.append("script/note", payload)
    return {"_summary": body[:300]}


def _write_step(admission: ScriptAdmission, params: dict[str, Any]) -> None:
    event = params.get("event") if isinstance(params.get("event"), dict) else params
    body = {key: value for key, value in dict(event).items() if key not in {"run_id", "session_id", "image_base64", "event"}}
    image = str(params.get("image_base64") or event.get("image_base64") or "")
    if image and admission.writer is not None:
        _store_image(admission, image, str(params.get("image_mime") or "image/png"))
    cap = str(body.get("capability_id") or "script")[:80]
    status = str(body.get("status") or "pass")[:40]
    record = {
        "capability_id": cap,
        "status": status,
        "summary": str(body.get("summary") or "")[:800],
        "error": str(body.get("error") or "")[:800],
        "executor_used": "scout",
        "source": "script_run",
    }
    if admission.writer is not None:
        admission.writer.append("tool/call", {"capability_id": cap, "params": {}, "source": "script_run"})
        admission.writer.append("tool/result", record)
    admission.steps.append(record)


def _store_image(admission: ScriptAdmission, encoded: str, mime: str) -> None:
    if len(encoded) > 12_000_000 or admission.writer is None:
        return
    from mino_nexus.loop.observe.agent_stream import make_thumb
    from mino_nexus.loop.observe.screen_store import publish_screen

    thumb = make_thumb(encoded)
    if not thumb:
        return
    session_id = str(getattr(admission.writer, "session_id", "") or admission.session_id)
    ref, _shot_hash = publish_screen(session_id, f"script-{len(admission.steps) + 1}", thumb)
    if not ref:
        return
    admission.writer.append("observe/screen", {
        "mime": "image/jpeg",
        "thumb": ref,
        "source": "script",
        "original_mime": mime,
    })
