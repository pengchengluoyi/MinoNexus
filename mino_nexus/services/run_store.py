"""用例运行库。任务中心与 GET /case-runner/run 共用，落 app_regression_runs。"""
from __future__ import annotations

import copy
import json
import re
import threading
import uuid
from datetime import datetime
from typing import Any, Optional

_LOCK = threading.RLock()
_LIVE: dict[str, dict[str, Any]] = {}
_CANCEL = set()

_CASE_TERMINAL = {
    "pass", "fail", "blocked", "declined", "skipped", "skip", "cancelled",
    "untestable", "unverifiable", "unexecutable",
}

_TASK_LIVE = frozenset({"running", "queued"})


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


_KEEP_RUNS = 400
_THUMB_RE = re.compile(r'"thumb"\s*:\s*"(?:[^"\\]|\\.)*"')


def _parse_payload(raw: Any) -> dict[str, Any]:
    """读历史任务时先抹掉步骤截图再解析。截图在 session 的 observe/screen 里。"""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")
    if raw is None:
        return {}
    text_payload = raw if isinstance(raw, str) else str(raw)
    if not text_payload:
        return {}
    if len(text_payload) > 200_000 and '"thumb"' in text_payload:
        text_payload = _THUMB_RE.sub('"thumb":""', text_payload)
    try:
        data = json.loads(text_payload)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _slim_doc(doc: dict[str, Any]) -> dict[str, Any]:
    """落库前去掉 engine_steps 里的截图，避免单条任务涨到几十 MB。"""
    for case in doc.get("cases") or []:
        if not isinstance(case, dict):
            continue
        steps = case.get("engine_steps")
        if not isinstance(steps, list):
            continue
        for step in steps:
            if not isinstance(step, dict):
                continue
            thumb = str(step.get("thumb") or "")
            if thumb.startswith("/static/"):
                continue
            if thumb:
                step["thumb"] = ""
    return doc


def _fetch_payloads(
    *,
    limit: int,
    offset: int = 0,
    app_id: str = "",
    status: str = "",
    run_id: str = "",
) -> list[dict[str, Any]]:
    """先按索引列取出 run_id，再只读这一页的 payload。避免排序时把历史截图整表拉进内存。"""
    from sqlalchemy import text

    from mino_nexus.core.database import engine, ensure_db

    ensure_db()
    where = " WHERE 1=1"
    params: dict[str, Any] = {}
    if run_id:
        where += " AND run_id = :run_id"
        params["run_id"] = run_id
    if app_id:
        where += " AND app_id = :app_id"
        params["app_id"] = app_id
    if status:
        where += " AND status = :status"
        params["status"] = status
    with engine.connect() as conn:
        if run_id:
            id_rows = conn.execute(
                text(f"SELECT run_id FROM app_regression_runs{where}"),
                params,
            ).fetchall()
        else:
            params["limit"] = max(0, int(limit))
            params["offset"] = max(0, int(offset))
            id_rows = conn.execute(
                text(
                    "SELECT run_id FROM app_regression_runs"
                    f"{where} ORDER BY started_at DESC LIMIT :limit OFFSET :offset"
                ),
                params,
            ).fetchall()
        ids = [str(r[0]) for r in id_rows if r and r[0]]
        if not ids:
            return []
        binds = {f"id{i}": rid for i, rid in enumerate(ids)}
        placeholders = ", ".join(f":id{i}" for i in range(len(ids)))
        payload_rows = conn.execute(
            text(f"SELECT run_id, payload FROM app_regression_runs WHERE run_id IN ({placeholders})"),
            binds,
        ).fetchall()
    by_id: dict[str, dict[str, Any]] = {}
    for rid, raw in payload_rows:
        doc = _parse_payload(raw)
        if doc.get("run_id"):
            by_id[str(rid)] = doc
    return [by_id[rid] for rid in ids if rid in by_id]


def _count_runs(*, app_id: str = "", status: str = "") -> int:
    from sqlalchemy import text

    from mino_nexus.core.database import engine, ensure_db

    ensure_db()
    sql = "SELECT COUNT(*) FROM app_regression_runs WHERE 1=1"
    params: dict[str, Any] = {}
    if app_id:
        sql += " AND app_id = :app_id"
        params["app_id"] = app_id
    if status:
        sql += " AND status = :status"
        params["status"] = status
    with engine.connect() as conn:
        return int(conn.execute(text(sql), params).scalar() or 0)


def _distinct_app_ids() -> list[str]:
    from sqlalchemy import text

    from mino_nexus.core.database import engine, ensure_db

    ensure_db()
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT DISTINCT app_id FROM app_regression_runs WHERE app_id != ''")
        ).fetchall()
    return [str(r[0]) for r in rows if r and r[0]]


def _upsert_run(doc: dict[str, Any]) -> None:
    from mino_nexus.core.database import session_scope

    with session_scope() as db:
        db.merge(_run_row(doc))


def _trim_runs(keep: int = _KEEP_RUNS) -> None:
    from sqlalchemy import text

    from mino_nexus.core.database import engine, ensure_db

    ensure_db()
    with engine.connect() as conn:
        total = int(conn.execute(text("SELECT COUNT(*) FROM app_regression_runs")).scalar() or 0)
        extra = total - int(keep)
        if extra <= 0:
            return
        old = conn.execute(
            text("SELECT run_id FROM app_regression_runs ORDER BY started_at ASC LIMIT :n"),
            {"n": extra},
        ).fetchall()
        ids = [str(r[0]) for r in old if r and r[0]]
        if not ids:
            return
        binds = {f"id{i}": rid for i, rid in enumerate(ids)}
        placeholders = ", ".join(f":id{i}" for i in range(len(ids)))
        conn.execute(text(f"DELETE FROM app_regression_runs WHERE run_id IN ({placeholders})"), binds)
        conn.commit()
    for rid in ids:
        _LIVE.pop(rid, None)


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


def new_run_id() -> str:
    return f"cr-{uuid.uuid4().hex[:12]}"


def report_run_id(run_id: str, case_id: str, *, sn: str = "", coverage: str = "once") -> str:
    """Session / stream 主键：有机型绑定时带 sn，避免多机并行写同一条 log。"""
    rid = str(run_id or "").strip()
    cid = str(case_id or "").strip()
    device = str(sn or "").strip()
    if not cid:
        return rid
    if device:
        return f"{rid}::{cid}::{device}"
    return f"{rid}::{cid}"


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
    skipped = sum(1 for c in cases if c.get("status") == "skip")
    doc["total"] = len(cases)
    doc["completed"] = completed
    doc["passed"] = passed
    doc["failed"] = failed
    doc["blocked"] = blocked
    doc["declined"] = declined
    doc["cancelled"] = cancelled
    doc["skipped"] = skipped
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
        "env_surface": doc.get("env_surface") or "",
        "action_scheme": "dom" if str(doc.get("action_scheme") or "").strip().lower() == "dom" else "visual",
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
        "boot_ms": int(doc.get("boot_ms") or 0),
        "boot_phases": list(doc.get("boot_phases") or []),
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
    doc = _slim_doc(_recompute(doc))
    rid = str(doc.get("run_id") or "")
    with _LOCK:
        _LIVE[rid] = doc
        _upsert_run(doc)
        _trim_runs()
    return copy.deepcopy(doc)


def get(run_id: str) -> Optional[dict[str, Any]]:
    rid = str(run_id or "").strip()
    if not rid:
        return None
    with _LOCK:
        if rid in _LIVE:
            return copy.deepcopy(_LIVE[rid])
    rows = _fetch_payloads(limit=1, run_id=rid)
    return copy.deepcopy(rows[0]) if rows else None


def list_runs(
    *,
    limit: int = 30,
    app_id: str = "",
    status: str = "",
    offset: int = 0,
) -> list[dict[str, Any]]:
    cap = max(0, int(limit or 30))
    off = max(0, int(offset or 0))
    want_app = str(app_id or "").strip()
    want_status = str(status or "").strip().lower()
    rows = _fetch_payloads(limit=cap, offset=off, app_id=want_app, status=want_status)
    with _LOCK:
        live_docs = [copy.deepcopy(d) for d in _LIVE.values()]
    by_id: dict[str, dict[str, Any]] = {}
    for row in rows:
        rid = str(row.get("run_id") or "")
        if rid:
            by_id[rid] = row
    for live in live_docs:
        if want_app and str(live.get("app_id") or "") != want_app:
            continue
        if want_status and str(live.get("status") or "") != want_status:
            continue
        rid = str(live.get("run_id") or "")
        if not rid:
            continue
        if rid in by_id or off == 0:
            by_id[rid] = live
    out = list(by_id.values())
    out.sort(key=lambda r: str(r.get("started_at") or ""), reverse=True)
    return out[:cap] if off == 0 else out


def list_tasks(app_id: str = "", status: str = "", limit: int = 50, offset: int = 0) -> dict[str, Any]:
    want = str(status or "").strip().lower()
    offset = max(0, int(offset or 0))
    limit = max(0, int(limit or 50))
    total = _count_runs(app_id=app_id, status=want)
    rows = list_runs(limit=limit, offset=offset, app_id=app_id, status=want)
    tasks = [to_task_json(r, include_cases=False) for r in rows]
    return {
        "items": tasks,
        "total": total,
        "limit": limit,
        "offset": offset,
        "app_id": app_id,
        "status": want,
    }


def summary_for_apps(app_ids: list[str]) -> list[dict[str, Any]]:
    ids = [a for a in app_ids if a]
    if not ids:
        ids = _distinct_app_ids()
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


def task_is_live(doc: dict[str, Any] | None) -> bool:
    return str((doc or {}).get("status") or "") in _TASK_LIVE


def _run_matches_sn(row: dict[str, Any], sn: str) -> bool:
    want = str(sn or "").strip()
    if not want:
        return False
    sns = [str(x) for x in (row.get("sns") or [])]
    return want == str(row.get("sn") or "") or want in sns


def running_index() -> dict[str, list[str]]:
    """在途任务按设备聚合。只读 status=running，不扫历史 payload。"""
    docs = _fetch_payloads(limit=80, status="running")
    with _LOCK:
        live = [copy.deepcopy(d) for d in _LIVE.values()]
    by_id = {str(d.get("run_id") or ""): d for d in docs if d.get("run_id")}
    for doc in live:
        rid = str(doc.get("run_id") or "")
        if not rid:
            continue
        if str(doc.get("status") or "") == "running":
            by_id[rid] = doc
        else:
            by_id.pop(rid, None)
    idx: dict[str, list[str]] = {}
    for doc in by_id.values():
        rid = str(doc.get("run_id") or "")
        if not rid:
            continue
        sns = [str(x).strip() for x in (doc.get("sns") or []) if str(x).strip()]
        head = str(doc.get("sn") or "").strip()
        if head and head not in sns:
            sns.insert(0, head)
        for sn in sns:
            bucket = idx.setdefault(sn, [])
            if rid not in bucket:
                bucket.append(rid)
    return idx


def running_run_ids_for_sn(sn: str, *, limit: int = 400) -> list[str]:
    """Nexus 侧在途任务（不依赖 Scout active_runs 上报）。"""
    want = str(sn or "").strip()
    if not want:
        return []
    return running_index().get(want, [])[: max(0, int(limit or 0)) or 400]


def busy_task_for_sn(sn: str) -> str:
    ids = running_run_ids_for_sn(sn, limit=80)
    return ids[0] if ids else ""


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


def task_cancelled(run_id: str) -> bool:
    """用户取消或任务已非 running：agent / RouterProxy / DisplayGuard 应立刻停调度。"""
    rid = str(run_id or "").strip()
    if not rid:
        return False
    if cancel_requested(rid):
        return True
    doc = get(rid)
    if not doc:
        return False
    return not task_is_live(doc)


def clear_cancel(run_id: str) -> None:
    with _LOCK:
        _CANCEL.discard(run_id)


def cancel_run(run_id: str, *, reason: str = "已取消") -> dict[str, Any]:
    """用户取消：立刻落库为 cancelled，后台线程不得再覆盖终态。"""
    doc = get(run_id)
    if not doc:
        return {"ok": False, "code": 404, "reason": "任务不存在"}
    if not task_is_live(doc):
        clear_cancel(run_id)
        return {"ok": True, "already": True, "code": 200, "doc": doc}
    with _LOCK:
        _CANCEL.add(run_id)
    finished = finish(run_id, status="cancelled", error=reason)
    return {"ok": True, "code": 200, "doc": finished}


def reconcile_stale_running_runs(*, reason: str = "Nexus 重启，任务中断") -> list[str]:
    """启动时清扫僵尸 running（线程已死、仅 DB 残留）。"""
    done: list[str] = []
    for row in list_runs(limit=80, status="running"):
        rid = str(row.get("run_id") or "").strip()
        if not rid:
            continue
        try:
            finish(rid, status="failed", error=reason)
            done.append(rid)
        except KeyError:
            pass
    return done


def patch_case(run_id: str, case_id: str, **fields: Any) -> dict[str, Any]:
    """多设备并行跑批时可能并发 patch 不同 case，须在锁内读-改-写。"""
    with _LOCK:
        doc = get(run_id)
        if not doc:
            raise KeyError(run_id)
        for case in doc.get("cases") or []:
            if str(case.get("case_id")) == str(case_id):
                case.update({k: v for k, v in fields.items() if v is not None})
                break
        return put(doc)


def _session_status_for_close(case_status: str) -> str:
    st = str(case_status or "").strip().lower()
    if st in ("pass", "done"):
        return "pass"
    if st in ("cancelled",):
        return "cancelled"
    if st in ("blocked",):
        return "blocked"
    if st in ("unverifiable",):
        return "unverifiable"
    if st in ("skip", "skipped"):
        return "skip"
    if st in ("fail", "failed", "declined"):
        return "fail"
    return st or "unknown"


def _close_open_sessions(doc: dict[str, Any], *, summary: str = "") -> None:
    from mino_nexus.loop.observe.session_log import force_close_session

    rid = str(doc.get("run_id") or "")
    for case in doc.get("cases") or []:
        if not isinstance(case, dict):
            continue
        sid = str(case.get("report_run_id") or "").strip()
        if not sid:
            cid = str(case.get("case_id") or "")
            sid = report_run_id(rid, cid) if cid else ""
        if not sid:
            continue
        cst = str(case.get("status") or "")
        force_close_session(
            session_id=sid,
            status=_session_status_for_close(cst),
            summary=str(case.get("summary") or summary or "")[:2000],
            step_count=len(case.get("engine_steps") or []),
        )


def interrupt_runs(run_ids: list[str], *, reason: str) -> list[str]:
    """节点断开 / 设备丢失：在途 run 立刻失败，不重派。"""
    from mino_nexus.loop.observe.agent_stream import emit_testing_task
    from mino_nexus.loop.web.web_env import release_devices_for_run

    done: list[str] = []
    seen: set[str] = set()
    for raw in run_ids or []:
        rid = str(raw or "").strip()
        if not rid or rid in seen:
            continue
        seen.add(rid)
        request_cancel(rid)
        doc = get(rid)
        if not doc or not task_is_live(doc):
            continue
        finished = finish(rid, status="failed", error=reason)
        sns = [str(x) for x in (finished.get("sns") or []) if str(x).strip()]
        head = str(finished.get("sn") or "").strip()
        if head and head not in sns:
            sns = [head, *sns]
        release_devices_for_run(
            rid,
            sns=sns,
            platforms_by_sn=finished.get("platforms_by_sn")
            if isinstance(finished.get("platforms_by_sn"), dict)
            else {},
        )
        from mino_nexus.services.run_resource_release import release_run_resource_holdings

        release_run_resource_holdings(finished, run_id=rid)
        emit_testing_task({
            "event": "task_finished",
            "run_id": rid,
            "task_id": rid,
            "status": finished.get("status") or "failed",
            "app_id": str(finished.get("app_id") or ""),
        })
        done.append(rid)
    return done


def finish(run_id: str, *, status: str = "done", error: str = "") -> dict[str, Any]:
    doc = get(run_id)
    if not doc:
        raise KeyError(run_id)
    if not task_is_live(doc):
        clear_cancel(run_id)
        return doc
    if status in ("failed", "cancelled"):
        for case in doc.get("cases") or []:
            if str(case.get("status") or "") in ("pending", "running"):
                case["status"] = "cancelled"
                case["summary"] = case.get("summary") or error or "任务已结束"
    doc["status"] = status
    doc["error"] = error or doc.get("error") or ""
    doc["finished_at"] = _now()
    _close_open_sessions(doc, summary=error or "")
    clear_cancel(run_id)
    out = put(doc)
    from mino_nexus.services.run_resource_release import release_run_resource_holdings

    release_run_resource_holdings(out)
    return out
