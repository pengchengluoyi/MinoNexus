"""用例运行库。任务中心与 GET /case-runner/run 共用，落 app_regression_runs。"""
from __future__ import annotations

from mino_nexus.action_space.scheme import normalize_action_scheme

import copy
from mino_nexus.platform.identity import current_user_id
import json
import re

from mino_nexus.core.case_text import line_text, looks_like_engine_step, spec_lines  # noqa: F401,E402
import threading
import time
import uuid
from datetime import datetime
from typing import Any, Optional

_LOCK = threading.RLock()
_LIVE: dict[str, dict[str, Any]] = {}
_CANCEL = set()

_CASE_TERMINAL = {
    "pass", "fail", "blocked", "declined", "skipped", "skip", "cancelled",
    "untestable", "unverifiable", "unexecutable",
    "product_fail", "tool_blocked", "env_blocked", "unverified",
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
    if current_user_id():
        where += " AND owner_user_id = :owner"
        params["owner"] = current_user_id()
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
    if current_user_id():
        sql += " AND owner_user_id = :owner"
        params["owner"] = current_user_id()
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
            text("SELECT DISTINCT app_id FROM app_regression_runs WHERE app_id != '' AND (:owner = '' OR owner_user_id = :owner)"), {"owner": current_user_id()}
        ).fetchall()
    return [str(r[0]) for r in rows if r and r[0]]


def _upsert_run(doc: dict[str, Any]) -> None:
    from mino_nexus.core.database import session_scope

    from sqlalchemy import text
    from mino_nexus.resources.device_admission import admit_in_transaction
    with session_scope() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        from mino_nexus.models.run import AppRegressionRun
        from mino_nexus.casework.run_write_safety import merge
        previous = db.get(AppRegressionRun, str(doc.get("run_id") or ""))
        if previous is None and doc.get("client_request_id"):
            from mino_nexus.models.run_admission import RunAdmission
            receipt = db.query(RunAdmission).filter_by(owner_user_id=doc["owner_user_id"], request_id=doc["client_request_id"]).one_or_none()
            if receipt:
                if receipt.request_digest != doc.get("request_digest"):
                    raise ValueError("run_request_conflict")
                original = db.get(AppRegressionRun, receipt.run_id)
                if original is None:
                    raise ValueError("run_receipt_target_missing")
                doc.clear()
                doc.update(copy.deepcopy(original.payload))
                return
            from mino_nexus.core.ids import prefixed
            db.add(RunAdmission(id=prefixed("admission"), owner_user_id=doc["owner_user_id"],
                request_id=doc["client_request_id"], request_digest=doc["request_digest"], run_id=doc["run_id"]))
        if previous is None and doc.get("retry_contract"):
            from mino_nexus.casework.run_retry import validate_admission
            validate_admission(db, doc)
        guards = merge(previous.payload if previous else None, doc)
        from mino_nexus.casework.write_reconciliation import merge_receipts
        reconciliations = merge_receipts(previous.payload if previous else None, doc)
        if previous is not None:
            frozen = previous.payload
            for field in ("owner_user_id", "plugin_user_id", "client_request_id", "request_digest", "derived_from",
                          "requested_total", "skipped_cases", "coverage", "sns", "device_targets", "platforms_by_sn",
                          "environment_baseline_digest", "execution_policy", "retry_contract"):
                if field in frozen:
                    doc[field] = copy.deepcopy(frozen[field])
            before = {case.get("execution_unit_id"): case for case in frozen.get("cases") or []}
            incoming = {case.get("execution_unit_id"): case for case in doc.get("cases") or []}
            if before and all(before) and set(before) != set(incoming):
                raise ValueError("run_execution_selection_immutable")
            for case in doc.get("cases") or []:
                original_case = before.get(case.get("execution_unit_id")) or {}
                for field in ("case_id", "sn", "execution_unit_id", "frozen_identity", "source_revision_id", "v5_publication", "retry_count", "retry_of", "retry_of_execution_unit_id", "attempt_deadline_at"):
                    if field in original_case:
                        case[field] = copy.deepcopy(original_case[field])
        if previous is not None and previous.status not in _TASK_LIVE:
            frozen = copy.deepcopy(previous.payload)
            doc.clear()
            doc.update(frozen)
        if guards:
            doc["write_safety"] = guards
        if reconciliations:
            doc["write_reconciliation"] = reconciliations
        admit_in_transaction(db, doc)
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
            text("SELECT run_id, payload FROM app_regression_runs WHERE status NOT IN ('running', 'queued') ORDER BY started_at ASC LIMIT :n"),
            {"n": extra},
        ).fetchall()
        from mino_nexus.casework.run_write_safety import unresolved
        referenced = {str(row[0]) for row in conn.execute(text("SELECT run_id FROM run_admissions"))}
        referenced.update(str(row[0]) for row in conn.execute(text("SELECT json_extract(payload, '$.derived_from') FROM app_regression_runs WHERE json_extract(payload, '$.derived_from') IS NOT NULL")))
        ids = [str(r[0]) for r in old if r and r[0] and str(r[0]) not in referenced and not unresolved(_parse_payload(r[1]))]
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
        owner_user_id=str(row.get("owner_user_id") or row.get("plugin_user_id") or ""),
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


_RUN_ID_LOCK = threading.Lock()
_LAST_RUN_ID = ""


def new_run_id() -> str:
    global _LAST_RUN_ID
    from mino_nexus.core.ids import task_id

    with _RUN_ID_LOCK:
        for _ in range(20):
            rid = task_id()
            if rid != _LAST_RUN_ID and get(rid) is None:
                _LAST_RUN_ID = rid
                return rid
            time.sleep(0.001)
    raise RuntimeError("run_id_exhausted")


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





def _run_case_meta(raw: dict[str, Any]) -> dict[str, Any]:
    """任务快照只带跑起来要读的编译结果。操作阶段不再现场编译。"""
    meta = raw.get("meta") if isinstance(raw.get("meta"), dict) else {}
    kept: dict[str, Any] = {}
    plans = meta.get("step_program_keys")
    if isinstance(plans, dict):
        kept["step_program_keys"] = plans
    auto = meta.get("ui_automation")
    if isinstance(auto, dict):
        kept["ui_automation"] = auto
    return kept


def seed_case(run_id: str, raw: dict[str, Any], index: int, *, sn: str = "", coverage: str = "once") -> dict[str, Any]:
    from mino_nexus.casework.run_comparison import frozen_identity
    cid = str(raw.get("case_id") or "").strip()
    if not cid:
        raise ValueError("case_id_required")
    steps = spec_lines(raw.get("steps"))
    expected = spec_lines(raw.get("expected"))
    steps_raw = str(raw.get("steps_raw") or "").strip() or "\n".join(
        f"{i + 1}. {t}" for i, t in enumerate(steps)
    )
    expected_raw = str(raw.get("expected_raw") or "").strip() or "\n".join(
        f"{i + 1}. {t}" for i, t in enumerate(expected)
    )
    title = str(raw.get("title") or raw.get("name") or "").strip() or cid
    return {
        "case_id": cid,
        "execution_unit_id": "unit" + uuid.uuid5(uuid.NAMESPACE_URL, f"{run_id}:{cid}:{sn}:{index}").hex,
        "frozen_identity": frozen_identity(raw),
        "source_revision_id": raw.get("source_revision_id") or "",
        "name": title,
        "title": title,
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
        "steps_parsed": [str(x) for x in (raw.get("steps_parsed") or []) if str(x).strip()],
        "expected_parsed": [str(x) for x in (raw.get("expected_parsed") or []) if str(x).strip()],
        "meta": _run_case_meta(raw),
        "error": "",
        "project_id": raw.get("project_id", ""),
        "resource_key": copy.deepcopy(raw.get("resource_key")),
        "case_scene": copy.deepcopy(raw.get("case_scene")),
        "v5_publication": copy.deepcopy(raw.get("v5_publication")),
        "v5_plan_state": raw.get("v5_plan_state", "draft"),
        "wall_budget_sec": raw.get("wall_budget_sec", 0),
    }


def _recompute(doc: dict[str, Any]) -> dict[str, Any]:
    cases = [c for c in (doc.get("cases") or []) if isinstance(c, dict)]
    completed = sum(1 for c in cases if str(c.get("status") or "") in _CASE_TERMINAL)
    passed = sum(1 for c in cases if c.get("status") == "pass")
    failed = sum(1 for c in cases if c.get("status") in {"fail", "product_fail"})
    blocked = sum(1 for c in cases if c.get("status") in {"blocked", "tool_blocked", "env_blocked"})
    declined = sum(1 for c in cases if c.get("status") == "declined")
    cancelled = sum(1 for c in cases if c.get("status") == "cancelled")
    skipped = sum(1 for c in cases if c.get("status") == "skip")
    doc["total"] = len(cases)
    doc["completed"] = completed
    doc["passed"] = passed
    doc["failed"] = failed
    doc["blocked"] = blocked
    for terminal in ("product_fail", "tool_blocked", "env_blocked", "unverified"):
        doc[terminal] = sum(1 for c in cases if c.get("status") == terminal)
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
    from mino_nexus.casework.run_write_safety import unresolved

    doc = _recompute(copy.deepcopy(doc or {}))
    sns = [str(x).strip() for x in (doc.get("sns") or []) if str(x).strip()]
    sn = str(doc.get("sn") or (sns[0] if sns else ""))
    if sn and sn not in sns:
        sns.insert(0, sn)
    task = {
        "task_id": doc.get("run_id") or doc.get("task_id") or "",
        "client_request_id": doc.get("client_request_id") or "",
        "derived_from": doc.get("derived_from") or "",
        "execution_policy": copy.deepcopy(doc.get("execution_policy") or {}),
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
        "action_scheme": normalize_action_scheme(doc.get("action_scheme")),
        "resource_isolated": bool(unresolved(doc)),
        "package": doc.get("package") or "",
        "requirement_id": doc.get("requirement_id") or "",
        "release_id": doc.get("release_id") or "",
        "slot_id": doc.get("slot_id") or "",
        "status": doc.get("status") or "running",
        "total": doc.get("total") or 0,
        "requested_total": doc.get("requested_total", doc.get("total") or 0),
        "skipped_cases": copy.deepcopy(doc.get("skipped_cases") or []),
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
        "execution_mode": doc.get("execution_mode") or "",
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
    "case_id", "name", "title", "sn", "status", "summary", "report_run_id",
    "session_id", "duration_ms",
    "elapsed_ms", "module", "platform", "hitl",
)


def _case_display_title(case: dict[str, Any]) -> str:
    return str(case.get("title") or case.get("name") or case.get("case_id") or "").strip()


def _case_brief(case: dict[str, Any]) -> dict[str, Any]:
    out = {k: case.get(k) for k in _CASE_BRIEF_KEYS}
    out["title"] = _case_display_title(case)
    out["elapsed_ms"] = int(case.get("elapsed_ms") or 0)
    out["hitl"] = bool(case.get("hitl"))
    duration = case.get("duration_ms")
    out["duration_ms"] = int(duration) if isinstance(duration, int) else None
    out["session_id"] = str(case.get("session_id") or "") or None
    return out


def _public_case(case: dict[str, Any]) -> dict[str, Any]:
    """详情接口：保留用例原文，执行轨迹单独放 engine_steps。"""
    row = dict(case)
    row["title"] = _case_display_title(row)
    leaked = [x for x in (row.get("steps") or []) if looks_like_engine_step(x)]
    if leaked and not row.get("engine_steps"):
        row["engine_steps"] = leaked
    if leaked:
        row["steps"] = spec_lines(row.get("steps"))
    row.setdefault("engine_steps", [])
    return row


def put(doc: dict[str, Any]) -> dict[str, Any]:
    started = time.perf_counter()
    doc = _slim_doc(_recompute(doc))
    rid = str(doc.get("run_id") or "")
    prepared = time.perf_counter()
    with _LOCK:
        acquired = time.perf_counter()
        _upsert_run(doc)
        persisted = time.perf_counter()
        _LIVE[str(doc.get("run_id") or rid)] = doc
        _trim_runs()
        trimmed = time.perf_counter()
    result = copy.deepcopy(doc)
    finished = time.perf_counter()
    if finished - started >= 0.1:
        from mino_nexus.core.log import SLog
        SLog.i("RunStore", f"PERF run={rid} prepare_ms={(prepared-started)*1000:.2f} "
            f"lock_ms={(acquired-prepared)*1000:.2f} persist_ms={(persisted-acquired)*1000:.2f} "
            f"trim_ms={(trimmed-persisted)*1000:.2f} copy_ms={(finished-trimmed)*1000:.2f}")
    return result



def get(run_id: str) -> Optional[dict[str, Any]]:
    rid = str(run_id or "").strip()
    if not rid:
        return None
    with _LOCK:
        if rid in _LIVE:
            doc = _LIVE[rid]
            if current_user_id() and doc.get("owner_user_id") != current_user_id():
                return None
            return copy.deepcopy(doc)
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
        if current_user_id() and live.get("owner_user_id") != current_user_id():
            continue
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


def _task_summaries(app_id: str, status: str, limit: int, offset: int) -> list[dict[str, Any]]:
    from sqlalchemy import text
    from mino_nexus.core.database import engine, ensure_db

    ensure_db()
    where = "WHERE 1=1"
    params = {"limit": limit, "offset": offset}
    if current_user_id():
        where += " AND owner_user_id = :owner"
        params["owner"] = current_user_id()
    if app_id:
        where += " AND app_id = :app_id"
        params["app_id"] = app_id
    if status:
        where += " AND status = :status"
        params["status"] = status
    statement = text(f"""
        SELECT run_id AS task_id, run_id, app_id, status, total, passed, failed,
               sn, platform, started_at, finished_at,
               json_extract(payload, '$.title') AS title,
               json_extract(payload, '$.cases[0].name') AS first_case_name,
               json_extract(payload, '$.cases[0].case_id') AS first_case_id
        FROM app_regression_runs {where}
        ORDER BY started_at DESC, run_id DESC LIMIT :limit OFFSET :offset
    """)
    with engine.connect() as conn:
        rows = [dict(row) for row in conn.execute(statement, params).mappings()]
    for row in rows:
        first = row.pop("first_case_name") or row.pop("first_case_id", "") or row["task_id"]
        row.pop("first_case_id", None)
        row["title"] = row["title"] or (f"{first} 等 {int(row['total'])} 条" if row["total"] > 1 else first)
    return rows


def last_case_results(app_id: str, case_ids: list[str] | None = None) -> dict[str, dict[str, Any]]:
    from sqlalchemy import bindparam, text
    from mino_nexus.core.database import engine, ensure_db

    ensure_db()
    if case_ids == []:
        return {}
    case_filter = " AND json_extract(item.value, '$.case_id') IN :case_ids" if case_ids is not None else ""
    statement = text(f"""
        WITH history AS (
            SELECT json_extract(item.value, '$.case_id') AS case_id,
                   COALESCE(json_extract(item.value, '$.status'), json_extract(item.value, '$.overall_status')) AS status,
                   COALESCE(json_extract(item.value, '$.fail_reason'), json_extract(item.value, '$.failure'),
                            json_extract(item.value, '$.error'), json_extract(item.value, '$.summary'), '') AS reason,
                   COALESCE(json_extract(item.value, '$.finished_at'), run.finished_at, run.started_at) AS at,
                   ROW_NUMBER() OVER (PARTITION BY json_extract(item.value, '$.case_id')
                                      ORDER BY run.started_at DESC, run.run_id DESC) AS position
            FROM app_regression_runs AS run, json_each(run.payload, '$.cases') AS item
            WHERE run.app_id = :app_id AND (:owner = '' OR run.owner_user_id = :owner) {case_filter}
        )
        SELECT case_id, status, reason, at FROM history WHERE position = 1 AND case_id IS NOT NULL
    """)
    params: dict[str, Any] = {"app_id": app_id, "owner": current_user_id()}
    if case_ids is not None:
        statement = statement.bindparams(bindparam("case_ids", expanding=True))
        params["case_ids"] = list(dict.fromkeys(case_ids))
    with engine.connect() as conn:
        return {row["case_id"]: {"status": row["status"], "reason": str(row["reason"] or ""), "at": row["at"] or ""}
                for row in conn.execute(statement, params).mappings()}


def list_tasks(app_id: str = "", status: str = "", limit: int = 50, offset: int = 0, summary: bool = False) -> dict[str, Any]:
    want = str(status or "").strip().lower()
    offset = max(0, int(offset or 0))
    limit = max(0, int(limit or 50))
    total = _count_runs(app_id=app_id, status=want)
    if summary:
        tasks = _task_summaries(app_id, want, min(limit, 200), offset)
    else:
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


def request_receipt(request_id: str) -> dict[str, Any] | None:
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.run_admission import RunAdmission
    from mino_nexus.platform.identity import require_user_id
    with session_scope() as db:
        row = db.query(RunAdmission).filter_by(owner_user_id=require_user_id(), request_id=request_id).one_or_none()
        return {"request_id": row.request_id, "run_id": row.run_id} if row else None


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
    """Keep completed units; recover only units that never started."""
    done: list[str] = []
    from mino_nexus.platform.identity import execution_user
    from contextlib import nullcontext
    from mino_nexus.casework.execution_policy import remaining
    for state in ("running", "queued"):
        offset = 0
        snapshots = []
        while True:
            rows = _fetch_payloads(limit=80, offset=offset, status=state)
            if not rows:
                break
            snapshots.extend(rows)
            offset += len(rows)
        for row in snapshots:
            with execution_user(row["owner_user_id"]) if row.get("owner_user_id") else nullcontext():
                rid = str(row["run_id"])
                resumable = False
                for case in row.get("cases") or []:
                    prior = str(case.get("status") or "pending")
                    if prior == "pending" and row.get("owner_user_id") and remaining(row) not in {None, 0}:
                        resumable = True
                        continue
                    if prior in {"pending", "running"}:
                        case.update(status="blocked", reason_code="nexus_restarted",
                                    execution_started="no" if prior == "pending" else "unknown",
                                    summary=reason)
                    if prior == "running":
                        guards = row.setdefault("write_safety", {})
                        if not any(g.get("sn") == case.get("sn") and g.get("state") in {"pending", "isolated"} for g in guards.values()):
                            guards["restart:" + str(case.get("execution_unit_id") or case.get("report_run_id"))] = {
                                "state": "isolated", "sn": case.get("sn") or row.get("sn"),
                                "reason_code": "restart_stop_and_reset_evidence_required", "stop_confirmed": False}
                row["interruption"] = {"reason_code": "nexus_restarted", "at": _now(),
                                       "automatic_replay": False, "resume_unstarted": resumable}
                row["status"] = "running"
                put(row)
                if not resumable:
                    finish(rid, status="failed", error=reason)
                done.append(rid)
    return done


def patch_case(run_id: str, case_id: str, *, target_sn: str | None = None,
               execution_unit_id: str = "", **fields: Any) -> dict[str, Any]:
    """Select the exact execution unit; never update the first duplicate case."""
    with _LOCK:
        doc = get(run_id)
        if not doc:
            raise KeyError(run_id)
        if not task_is_live(doc):
            return doc
        matches = [case for case in doc.get("cases") or []
                   if str(case.get("case_id")) == str(case_id)
                   and (target_sn is None or str(case.get("sn") or "") == target_sn)
                   and (not execution_unit_id or case.get("execution_unit_id") == execution_unit_id)]
        if len(matches) != 1:
            raise ValueError("execution_unit_ambiguous" if matches else "execution_unit_not_found")
        matches[0].update({k: v for k, v in fields.items() if v is not None})
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
    from mino_nexus.records.eventlog.writer import force_close_session

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



def finish(run_id: str, *, status: str = "done", error: str = "") -> dict[str, Any]:
    doc = get(run_id)
    if not doc:
        raise KeyError(run_id)
    if not task_is_live(doc):
        clear_cancel(run_id)
        return doc
    if status == "cancelled":
        for case in doc.get("cases") or []:
            if str(case.get("status") or "") in ("pending", "running"):
                case["status"] = "cancelled"
                case["summary"] = case.get("summary") or error or "任务已取消"
    elif status == "failed":
        # 单条失败不应把队列里未跑条目标成 cancelled；仅收口异常退出时仍挂起的行。
        for case in doc.get("cases") or []:
            if str(case.get("status") or "") in ("pending", "running"):
                case["status"] = "fail"
                case["summary"] = case.get("summary") or error or "任务异常结束，本条未执行"
                case.setdefault("reason_code", "batch_aborted")
    doc["status"] = status
    doc["error"] = error or doc.get("error") or ""
    doc["finished_at"] = _now()
    _close_open_sessions(doc, summary=error or "")
    clear_cancel(run_id)
    out = put(doc)
    from mino_nexus.casework.run_resource_release import release_run_resource_holdings

    release_run_resource_holdings(out)
    return out


from mino_nexus.contract.runs import register_runs  # noqa: E402

register_runs(get=get, task_is_live=task_is_live, running_run_ids_for_sn=running_run_ids_for_sn)
