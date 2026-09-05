"""用例运行库。任务中心与 GET /case-runner/run 共用，落 app_regression_runs。"""
from __future__ import annotations

import copy
import threading
import uuid
from datetime import datetime
from typing import Any, Optional

_LOCK = threading.RLock()
_LIVE: dict[str, dict[str, Any]] = {}
_CANCEL = set()

_CASE_TERMINAL = {
    "pass", "fail", "blocked", "declined", "skipped", "cancelled",
    "untestable", "unverifiable", "unexecutable",
}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _root() -> dict[str, Any]:
    from mino_nexus.core.database import SessionLocal, ensure_db
    from mino_nexus.models.run import AppRegressionRun

    ensure_db()
    db = SessionLocal()
    try:
        rows = []
        for row in db.query(AppRegressionRun).all():
            payload = dict(row.payload or {}) if isinstance(row.payload, dict) else {}
            if payload:
                rows.append(payload)
        return {"runs": rows}
    finally:
        db.close()


def _run_row(row: dict) -> Any:
    from mino_nexus.models.run import AppRegressionRun

    return AppRegressionRun(
        run_id=str(row["run_id"]),
        app_id=str(row.get("app_id") or ""),
        run_type=str(row.get("run_type") or "manual"),
        sn=str(row.get("sn") or ""),
        platform=str(row.get("platform") or "android"),
        status=str(row.get("status") or ""),
        total=float(row.get("total") or 0),
        passed=float(row.get("passed") or 0),
        failed=float(row.get("failed") or 0),
        error=str(row.get("error") or ""),
        payload=dict(row),
        started_at=str(row.get("started_at") or ""),
        finished_at=row.get("finished_at"),
    )


def _persist_runs(runs: list[dict[str, Any]]) -> None:
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.run import AppRegressionRun

    with session_scope() as db:
        keep = set()
        for row in runs:
            if isinstance(row, dict) and row.get("run_id"):
                db.merge(_run_row(row))
                keep.add(str(row["run_id"]))
        for row in db.query(AppRegressionRun).all():
            if row.run_id not in keep:
                db.delete(row)


def new_run_id() -> str:
    return f"cr-{uuid.uuid4().hex[:12]}"


def report_run_id(run_id: str, case_id: str, *, sn: str = "", coverage: str = "once") -> str:
    if coverage == "per_device" and sn:
        return f"{run_id}::{case_id}::{sn}"
    return f"{run_id}::{case_id}"


def line_text(item: Any) -> str:
    """用例步骤/预期必须是给人看的字符串。执行事件对象不能写进这里。"""
    if item is None:
        return ""
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, (int, float)):
        return str(item)
    if isinstance(item, dict):
        if item.get("capability_id") or item.get("executor_used"):
            return ""
        for key in ("text", "title", "step", "content", "name", "label"):
            val = item.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        return ""
    return str(item).strip()


def looks_like_engine_step(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    if item.get("capability_id") or item.get("executor_used"):
        return True
    return item.get("seq") is not None and bool(item.get("status")) and "summary" in item


def spec_lines(items: Any) -> list[str]:
    if not isinstance(items, list):
        return []
    engine = [x for x in items if looks_like_engine_step(x)]
    if items and (all(looks_like_engine_step(x) for x in items) or len(engine) >= max(2, int(len(items) * 0.6))):
        return []
    return [t for t in (line_text(x) for x in items) if t]


def seed_case(run_id: str, raw: dict[str, Any], index: int, *, sn: str = "", coverage: str = "once") -> dict[str, Any]:
    cid = str(raw.get("case_id") or "").strip() or f"case-{index}"
    steps = spec_lines(raw.get("steps"))
    expected = spec_lines(raw.get("expected"))
    steps_raw = str(raw.get("steps_raw") or "").strip() or "\n".join(
        f"{i + 1}. {t}" for i, t in enumerate(steps)
    )
    expected_raw = str(raw.get("expected_raw") or "").strip() or "\n".join(
        f"{i + 1}. {t}" for i, t in enumerate(expected)
    )
    return {
        "case_id": cid,
        "name": str(raw.get("name") or raw.get("title") or "").strip() or cid,
        "sn": sn,
        "status": "pending",
        "report_run_id": report_run_id(run_id, cid, sn=sn, coverage=coverage),
        "summary": "",
        "elapsed_ms": 0,
        "hitl": False,
        "module": str(raw.get("module") or "").strip(),
        "precondition": str(raw.get("precondition") or "").strip(),
        "steps": steps,
        "expected": expected,
        "steps_raw": steps_raw,
        "expected_raw": expected_raw,
        "engine_steps": [],
        "platform": str(raw.get("platform") or "").strip(),
        "point_ids": [str(x).strip() for x in (raw.get("point_ids") or []) if str(x).strip()],
        "error": "",
    }


def _recompute(doc: dict[str, Any]) -> dict[str, Any]:
    cases = [c for c in (doc.get("cases") or []) if isinstance(c, dict)]
    completed = sum(1 for c in cases if str(c.get("status") or "") in _CASE_TERMINAL)
    passed = sum(1 for c in cases if c.get("status") == "pass")
    failed = sum(1 for c in cases if c.get("status") == "fail")
    blocked = sum(1 for c in cases if c.get("status") == "blocked")
    declined = sum(1 for c in cases if c.get("status") == "declined")
    cancelled = sum(1 for c in cases if c.get("status") == "cancelled")
    doc["total"] = len(cases)
    doc["completed"] = completed
    doc["passed"] = passed
    doc["failed"] = failed
    doc["blocked"] = blocked
    doc["declined"] = declined
    doc["cancelled"] = cancelled
    current = ""
    for c in cases:
        if c.get("status") == "running":
            current = str(c.get("case_id") or "")
            break
    doc["current_case_id"] = current
    total = doc["total"]
    doc["progress"] = min(100, int(round(completed / total * 100))) if total else 0
    judged = passed + failed
    doc["pass_rate"] = int(round(passed / judged * 100)) if judged else 0
    return doc


def to_task_json(doc: dict[str, Any] | None, *, include_cases: bool = True) -> dict[str, Any]:
    doc = _recompute(copy.deepcopy(doc or {}))
    sns = [str(x).strip() for x in (doc.get("sns") or []) if str(x).strip()]
    sn = str(doc.get("sn") or (sns[0] if sns else ""))
    if sn and sn not in sns:
        sns.insert(0, sn)
    task = {
        "task_id": doc.get("run_id") or doc.get("task_id") or "",
        "run_id": doc.get("run_id") or "",
        "app_id": doc.get("app_id") or "",
        "app_name": doc.get("app_name") or "",
        "run_type": doc.get("run_type") or "manual",
        "sn": sn,
        "sns": sns,
        "coverage": doc.get("coverage") or "once",
        "platform": doc.get("platform") or "android",
        "platforms_by_sn": dict(doc.get("platforms_by_sn") or {}),
        "env_profile": doc.get("env_profile") or "",
        "package": doc.get("package") or "",
        "requirement_id": doc.get("requirement_id") or "",
        "release_id": doc.get("release_id") or "",
        "slot_id": doc.get("slot_id") or "",
        "status": doc.get("status") or "running",
        "total": doc.get("total") or 0,
        "completed": doc.get("completed") or 0,
        "passed": doc.get("passed") or 0,
        "failed": doc.get("failed") or 0,
        "blocked": doc.get("blocked") or 0,
        "declined": doc.get("declined") or 0,
        "untestable": doc.get("untestable") or 0,
        "progress": doc.get("progress") or 0,
        "pass_rate": doc.get("pass_rate") or 0,
        "error": doc.get("error") or "",
        "provider_name": doc.get("provider_name") or "",
        "model_name": doc.get("model_name") or "",
        "started_at": doc.get("started_at") or "",
        "finished_at": doc.get("finished_at") or None,
        "busy": doc.get("status") == "running",
        "current_case_id": doc.get("current_case_id") or "",
        "title": doc.get("title") or "",
        "engine": doc.get("engine") or "agent",
    }
    cases = [c for c in (doc.get("cases") or []) if isinstance(c, dict)]
    if include_cases:
        task["cases"] = [_public_case(c) for c in cases]
    else:
        # 列表也要带用例名/状态，否则执行批次只能显示任务号。
        task["cases"] = [_case_brief(c) for c in cases]
    if not task["title"] and cases:
        names = [str(c.get("name") or c.get("case_id") or "").strip() for c in cases]
        names = [n for n in names if n]
        if len(names) == 1:
            task["title"] = names[0]
        elif names:
            task["title"] = f"{names[0]} 等 {len(cases)} 条"
    return task


_CASE_BRIEF_KEYS = (
    "case_id", "name", "sn", "status", "summary", "report_run_id",
    "elapsed_ms", "module", "platform", "hitl",
)


def _case_brief(case: dict[str, Any]) -> dict[str, Any]:
    out = {k: case.get(k) for k in _CASE_BRIEF_KEYS}
    out["elapsed_ms"] = int(case.get("elapsed_ms") or 0)
    out["hitl"] = bool(case.get("hitl"))
    return out


def _public_case(case: dict[str, Any]) -> dict[str, Any]:
    """详情接口：保留用例原文，执行轨迹单独放 engine_steps。"""
    row = dict(case)
    leaked = [x for x in (row.get("steps") or []) if looks_like_engine_step(x)]
    if leaked and not row.get("engine_steps"):
        row["engine_steps"] = leaked
    if leaked:
        row["steps"] = spec_lines(row.get("steps"))
    row.setdefault("engine_steps", [])
    return row


def put(doc: dict[str, Any]) -> dict[str, Any]:
    doc = _recompute(doc)
    rid = str(doc.get("run_id") or "")
    with _LOCK:
        _LIVE[rid] = doc
        root = _root()
        found = False
        for i, row in enumerate(root["runs"]):
            if isinstance(row, dict) and row.get("run_id") == rid:
                root["runs"][i] = copy.deepcopy(doc)
                found = True
                break
        if not found:
            root["runs"].append(copy.deepcopy(doc))
        root["runs"] = root["runs"][-400:]
        _persist_runs(root["runs"])
    return copy.deepcopy(doc)


def get(run_id: str) -> Optional[dict[str, Any]]:
    rid = str(run_id or "").strip()
    with _LOCK:
        if rid in _LIVE:
            return copy.deepcopy(_LIVE[rid])
        for row in reversed(_root()["runs"]):
            if isinstance(row, dict) and row.get("run_id") == rid:
                return copy.deepcopy(row)
    return None


def list_runs(*, limit: int = 30, app_id: str = "") -> list[dict[str, Any]]:
    with _LOCK:
        rows = [copy.deepcopy(r) for r in _root()["runs"] if isinstance(r, dict)]
        for live in _LIVE.values():
            rid = live.get("run_id")
            for i, row in enumerate(rows):
                if row.get("run_id") == rid:
                    rows[i] = copy.deepcopy(live)
                    break
            else:
                rows.append(copy.deepcopy(live))
    if app_id:
        rows = [r for r in rows if r.get("app_id") == app_id]
    rows.sort(key=lambda r: str(r.get("started_at") or ""), reverse=True)
    return rows[: max(0, int(limit or 30))]


def list_tasks(app_id: str = "", status: str = "", limit: int = 50, offset: int = 0) -> dict[str, Any]:
    rows = list_runs(limit=400, app_id=app_id)
    want = str(status or "").strip().lower()
    tasks = [to_task_json(r, include_cases=False) for r in rows]
    if want:
        tasks = [t for t in tasks if t.get("status") == want]
    total = len(tasks)
    offset = max(0, int(offset or 0))
    limit = max(0, int(limit or 50))
    return {
        "items": tasks[offset:offset + limit],
        "total": total,
        "limit": limit,
        "offset": offset,
        "app_id": app_id,
        "status": want,
    }


def summary_for_apps(app_ids: list[str]) -> list[dict[str, Any]]:
    ids = [a for a in app_ids if a]
    if not ids:
        ids = list({str(r.get("app_id") or "") for r in list_runs(limit=400) if r.get("app_id")})
    out = []
    for aid in ids:
        rows = list_runs(limit=80, app_id=aid)
        running = [r for r in rows if r.get("status") == "running"]
        latest = rows[0] if rows else {}
        task = to_task_json(latest, include_cases=False) if latest else {}
        out.append({
            "app_id": aid,
            "running_count": len(running),
            "latest_task_id": task.get("task_id") or "",
            "status": task.get("status") or "",
            "completed": task.get("completed") or 0,
            "total": task.get("total") or 0,
            "started_at": task.get("started_at"),
            "pass_rate": task.get("pass_rate") or 0,
            "last_status": task.get("status") or "",
            "last_task_id": task.get("task_id") or "",
            "last_started_at": task.get("started_at"),
            "last_pass_rate": task.get("pass_rate") or 0,
            "total_count": len(rows),
        })
    return out


def busy_task_for_sn(sn: str) -> str:
    want = str(sn or "").strip()
    if not want:
        return ""
    for row in list_runs(limit=80):
        if row.get("status") != "running":
            continue
        sns = [str(x) for x in (row.get("sns") or [])]
        if want == row.get("sn") or want in sns:
            return str(row.get("run_id") or "")
    return ""


def request_cancel(run_id: str) -> dict[str, Any]:
    doc = get(run_id)
    if not doc:
        return {"ok": False, "code": 404, "reason": "任务不存在"}
    if doc.get("status") != "running":
        return {"ok": True, "already": True, "code": 200}
    with _LOCK:
        _CANCEL.add(run_id)
    return {"ok": True, "code": 200}


def cancel_requested(run_id: str) -> bool:
    with _LOCK:
        return run_id in _CANCEL


def clear_cancel(run_id: str) -> None:
    with _LOCK:
        _CANCEL.discard(run_id)


def patch_case(run_id: str, case_id: str, **fields: Any) -> dict[str, Any]:
    doc = get(run_id)
    if not doc:
        raise KeyError(run_id)
    for case in doc.get("cases") or []:
        if str(case.get("case_id")) == str(case_id):
            case.update({k: v for k, v in fields.items() if v is not None})
            break
    return put(doc)


def interrupt_runs(run_ids: list[str], *, reason: str) -> list[str]:
    """节点断开 / shutting_down：在途 run 立刻失败，不重派。"""
    done: list[str] = []
    for raw in run_ids or []:
        rid = str(raw or "").strip()
        if not rid:
            continue
        doc = get(rid)
        if not doc or doc.get("status") != "running":
            continue
        finish(rid, status="failed", error=reason)
        done.append(rid)
    return done


def finish(run_id: str, *, status: str = "done", error: str = "") -> dict[str, Any]:
    doc = get(run_id)
    if not doc:
        raise KeyError(run_id)
    if status in ("failed", "cancelled"):
        for case in doc.get("cases") or []:
            if str(case.get("status") or "") in ("pending", "running"):
                case["status"] = "cancelled"
                case["summary"] = case.get("summary") or error or "任务已结束"
    doc["status"] = status
    doc["error"] = error or doc.get("error") or ""
    doc["finished_at"] = _now()
    clear_cancel(run_id)
    return put(doc)
