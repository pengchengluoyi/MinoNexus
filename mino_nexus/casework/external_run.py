"""外部 agent 自己执行用例，Nexus 只记账：把它按阶段上报的事件写进该用例的会话。

不起 agent_loop、不占设备。事件写入与内置执行同一份 session_events，类型统一 `ext/*`，
payload 带 source=external_agent，详情页读到的就是同一条时间线。
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from mino_nexus.casework import run_store
from mino_nexus.platform.identity import require_user_id
from mino_nexus.records.eventlog import store as session_store
from mino_nexus.records.eventlog.writer import SessionWriter, open_session

SOURCE = "external_agent"
PHASES = ("prep", "do", "check")
KINDS = ("phase_start", "phase_end", "action", "observe", "note")
MAX_BATCH = 100
_RESULT_STATUS = {"pass", "fail", "blocked", "skip"}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def begin(app: dict[str, Any], case_id: str, *, agent: str = "", client_request_id: str = "",
          sn: str = "", lease_minutes: int = 30) -> dict[str, Any]:
    from mino_nexus.resources import app_automation as aas

    uid = require_user_id()
    case = next((c for c in aas.list_app_cases(app) if str(c.get("case_id")) == str(case_id)), None)
    if case is None:
        raise ValueError("所选用例已删除或不属于当前项目")
    run_id = run_store.new_run_id()
    seeded = run_store.seed_case(run_id, case, 0)
    seeded["status"] = "running"
    doc: dict[str, Any] = {
        "run_id": run_id, "task_id": run_id, "client_request_id": client_request_id,
        "engine": "external", "execution_mode": "external", "run_type": "external",
        "app_id": str(app.get("id") or ""), "app_name": str(app.get("name") or ""),
        "owner_user_id": uid, "plugin_user_id": uid,
        "sn": "", "sns": [], "coverage": "once", "platform": str(case.get("platform") or "web"),
        "external_agent": str(agent or "")[:80],
        "status": "running", "started_at": _now(), "finished_at": None,
        "cases": [seeded], "error": "",
    }
    if client_request_id:
        from mino_nexus.core.stable_digest import digest
        doc["request_digest"] = digest({"app_id": doc["app_id"], "case_id": case_id, "executor": "external"})
    persisted = run_store.put(doc)
    session_id = seeded["report_run_id"]
    writer = open_session(session_id=session_id, run_id=run_id, case_id=str(case_id),
                          app_id=doc["app_id"], sop_id="external",
                          extra={"source": SOURCE, "agent": doc["external_agent"],
                                 "goal": seeded["title"], "execution_mode": "external"})
    from mino_nexus.casework import external_lease
    held = external_lease.acquire(run_id, sn, agent=agent, minutes=lease_minutes)
    writer.append("ext/begin", {"source": SOURCE, "agent": doc["external_agent"], "sn": str(sn or ""),
                                "lease": held["lease"], "conflicts": held["conflicts"]})
    return {"lease": held["lease"], "conflicts": held["conflicts"], "run_id": persisted["run_id"], "case_id": str(case_id), "session_id": session_id,
            "phases": list(PHASES), "kinds": list(KINDS),
            "case": {"title": seeded["title"], "steps": seeded["steps"], "expected": seeded["expected"]}}


def _session_of(run_id: str, case_id: str) -> tuple[dict[str, Any], dict[str, Any], str]:
    doc = run_store.get(run_id)
    if not doc or doc.get("execution_mode") != "external":
        raise KeyError("external_run_not_found")
    case = next((c for c in doc.get("cases") or [] if str(c.get("case_id")) == str(case_id)), None)
    if case is None:
        raise KeyError("external_case_not_found")
    return doc, case, str(case["report_run_id"])


MAX_IMAGE_BYTES = 5 * 1024 * 1024


def save_screenshot(run_id: str, case_id: str, data_b64: str) -> dict[str, Any]:
    """图片落成文件，事件里只放 /static 引用，不把 base64 写进任务或会话。"""
    import base64
    import binascii
    import uuid
    from mino_nexus.core.paths import data_dir

    _session_of(run_id, case_id)
    raw = str(data_b64 or "").split(",", 1)[-1]
    try:
        blob = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("image_base64_invalid") from exc
    if not blob or len(blob) > MAX_IMAGE_BYTES:
        raise ValueError("image_size_invalid")
    ext = "png" if blob[:8] == b"\x89PNG\r\n\x1a\n" else "jpg" if blob[:3] == b"\xff\xd8\xff" else ""
    if not ext:
        raise ValueError("image_type_unsupported")
    folder = data_dir() / "uploads" / "external" / run_id
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{uuid.uuid4().hex[:16]}.{ext}"
    (folder / name).write_bytes(blob)
    return {"screenshot_ref": f"/static/external/{run_id}/{name}"}


def _clean(event: Any) -> dict[str, Any]:
    if not isinstance(event, dict):
        raise ValueError("event_invalid")
    kind = str(event.get("kind") or "").strip()
    phase = str(event.get("phase") or "").strip()
    if kind not in KINDS:
        raise ValueError(f"kind 必须是 {'/'.join(KINDS)}")
    if phase not in PHASES:
        raise ValueError(f"phase 必须是 {'/'.join(PHASES)}")
    body: dict[str, Any] = {"source": SOURCE, "kind": kind, "phase": phase}
    for key in ("seq", "case_step", "action", "target", "value", "result", "status", "note",
                "page", "duration_ms", "started_at", "ended_at", "error"):
        if event.get(key) is not None:
            body[key] = event[key]
    ref = event.get("screenshot_ref")
    if ref:
        ref = str(ref).strip()
        if ref.startswith("data:") or len(ref) > 500:
            raise ValueError("screenshot_ref 只接受文件路径或 URL，不接受 base64")
        body["screenshot_ref"] = ref
    if "status" in body and kind == "phase_end":
        body["status"] = str(body["status"]).lower()
    return body


def report(run_id: str, case_id: str, events: list[Any]) -> dict[str, Any]:
    if not isinstance(events, list) or not events:
        raise ValueError("events 不能为空")
    if len(events) > MAX_BATCH:
        raise ValueError(f"单次最多 {MAX_BATCH} 条")
    doc, _case, session_id = _session_of(run_id, case_id)
    if not session_store.is_open(session_id):
        raise ValueError("session_already_finalized")
    seen = {int(r["payload"]["seq"]) for r in session_store.read_events(session_id)
            if str(r.get("type", "")).startswith("ext/")
            and isinstance((r.get("payload") or {}).get("seq"), int)}
    writer = SessionWriter(session_id=session_id, run_id=run_id, case_id=str(case_id), app_id=str(doc.get("app_id") or ""))
    accepted = skipped = 0
    for raw in events:
        body = _clean(raw)
        seq = body.get("seq")
        if isinstance(seq, int) and seq in seen:
            skipped += 1
            continue
        body.setdefault("reported_at", _now())
        if writer.append(f"ext/{body['kind']}", body, phase=body["phase"]):
            accepted += 1
            if isinstance(seq, int):
                seen.add(seq)
        else:
            raise RuntimeError("event_persist_failed")
    return {"accepted": accepted, "duplicate": skipped, "session_id": session_id}


def finish(run_id: str, case_id: str, *, status: str, summary: str = "") -> dict[str, Any]:
    status = str(status or "").lower()
    if status not in _RESULT_STATUS:
        raise ValueError("status 必须是 pass/fail/blocked/skip")
    doc, case, session_id = _session_of(run_id, case_id)
    writer = SessionWriter(session_id=session_id, run_id=run_id, case_id=str(case_id), app_id=str(doc.get("app_id") or ""))
    writer.append("ext/finish", {"source": SOURCE, "status": status, "summary": str(summary or "")[:2000],
                                 "trust": "agent_claimed"})
    started = time.time()
    run_store.patch_case(run_id, str(case_id), status=status, summary=str(summary or "")[:2000],
                         trust="agent_claimed")
    from mino_nexus.casework import external_lease
    external_lease.release(run_id)
    out = run_store.finish(run_id, status="done")
    return {"run_id": run_id, "status": out.get("status"), "case_status": status,
            "session_id": session_id, "finalized_ms": int((time.time() - started) * 1000)}
