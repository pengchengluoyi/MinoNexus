"""测试管家（V7 第三版）：圈定 → 检查 → 执行 → 诊断，不生成用例。"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any

from mino_nexus.core.log import SLog
from mino_nexus.models.mino_bots import CollabActivity, CollabTopic, CollabWorkItem

TAG = "CollabSteward"

LEGACY_KINDS = frozenset(
    {
        "qa_tick",
        "llm_job",
        "apply_drafts",
        "v5_publish",
        "openhands",
        "none",
        "compile",
        "publish",
        "defect",
        "resource_gap",
    }
)
STEWARD_KINDS = frozenset({"scope", "run", "diagnose", "issue", "recheck"})
RUN_KINDS = frozenset({"run", "case_run"})

_STATUS_RE = re.compile(r"(怎么样|进度|进展|状态如何|跑到哪|进行如何)", re.I)
_CREATE_RE = re.compile(r"(帮我测|测一下|开始测|提测|跑一下)", re.I)
_RERUN_RE = re.compile(r"(失败.{0,6}重跑|重跑|再跑)", re.I)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def log_activity(
    db,
    topic_id: str,
    *,
    kind: str,
    body: str,
    refs: dict[str, Any] | None = None,
) -> None:
    text = str(body or "").strip()
    if not text:
        return
    db.add(
        CollabActivity(
            activity_id=str(uuid.uuid4()),
            topic_id=topic_id,
            kind=str(kind or "info")[:32],
            body=text[:4000],
            refs=dict(refs or {}),
            created_at=_now(),
        )
    )


def cleanup_legacy_work_items(db, topic_id: str) -> int:
    rows = (
        db.query(CollabWorkItem)
        .filter(
            CollabWorkItem.topic_id == topic_id,
            CollabWorkItem.action_kind.in_(tuple(LEGACY_KINDS)),
            CollabWorkItem.status.notin_(("cancelled", "done")),
        )
        .all()
    )
    n = 0
    stamp = _now()
    for wi in rows:
        wi.status = "cancelled"
        wi.activity = "遗留项已清理，不再参与执行"
        wi.blocked_reason = ""
        wi.claimed_by = ""
        wi.claimed_at = ""
        wi.updated_at = stamp
        n += 1
    if n:
        log_activity(db, topic_id, kind="clean", body=f"已清理 {n} 条遗留工作项")
    return n


def _topic_scope_case_ids(topic: CollabTopic) -> list[str]:
    explicit = topic.scope_case_ids if isinstance(topic.scope_case_ids, list) else []
    ids = [str(x).strip() for x in explicit if str(x).strip()]
    if ids:
        return list(dict.fromkeys(ids))
    anchor = topic.anchor if isinstance(topic.anchor, dict) else {}
    if str(anchor.get("kind") or "") == "case":
        cid = str(anchor.get("case_id") or "").strip()
        if cid:
            return [cid]
    return []


def resolve_scope_case_ids(db, topic: CollabTopic) -> list[str]:
    from mino_nexus.services.collab_workspace import scope_ids
    return scope_ids(db, topic)










def build_scope_snapshot(project_id: str, app_id: str, case_ids: list[str]) -> dict[str, Any]:
    from mino_nexus.services import project_store as ps
    from mino_nexus.services.case_preflight import blocked_message, preflight
    from mino_nexus.services.case_store import get_case

    executable: list[str] = []
    blocked: list[dict[str, Any]] = []
    per_case: list[dict[str, Any]] = []
    if not case_ids:
        return {
            "total": 0,
            "executable": [],
            "blocked": [],
            "cases": [],
            "version": _now(),
        }
    app = ps.get_app(app_id) if app_id else None
    if app is None:
        first = get_case(project_id, case_ids[0]) or {}
        aid = str(first.get("app_id") or "").strip()
        if aid:
            app = ps.get_app(aid)
    summary: dict[str, Any] = {}
    if app:
        try:
            summary = preflight(app, case_ids)
        except ValueError as exc:
            return {"total": len(case_ids), "executable": [], "blocked": [{"message": str(exc)}], "cases": [], "version": _now()}
        for row in summary.get("blocked") or []:
            if isinstance(row, dict):
                blocked.append(row)
        ok_ids = [str(x) for x in (summary.get("runnable") or summary.get("executable") or []) if str(x).strip()]
        if not ok_ids and not blocked:
            ok_ids = [c for c in case_ids if c not in {str(b.get("case_id") or "") for b in blocked}]
        executable = list(dict.fromkeys(ok_ids))
    else:
        blocked.append({"message": "缺少 app_id，无法做执行前检查"})
    for cid in case_ids:
        case = get_case(project_id, cid) or {}
        per_case.append(
            {
                "case_id": cid,
                "title": case.get("title") or case.get("name") or cid,
                "module": case.get("module") or "",
                "executable": cid in executable,
            }
        )
    return {
        "total": len(case_ids),
        "executable": executable,
        "blocked": blocked,
        "blocked_message": blocked_message(blocked) if blocked else "",
        "cases": per_case,
        "version": _now(),
    }


def _set_work_activity(wi: CollabWorkItem, text: str, *, stage: str = "") -> None:
    wi.activity = str(text or "")[:500]
    if stage:
        wi.stage = stage[:32]
    if not wi.started_at:
        wi.started_at = _now()
    wi.heartbeat_at = _now()


def ensure_scope_work_item(db, topic: CollabTopic, holder_user_id: str) -> CollabWorkItem | None:
    existing = (
        db.query(CollabWorkItem)
        .filter(
            CollabWorkItem.topic_id == topic.topic_id,
            CollabWorkItem.action_kind == "scope",
            CollabWorkItem.status.in_(("pending", "running", "waiting")),
        )
        .first()
    )
    if existing is not None:
        return existing
    now = _now()
    wid = str(uuid.uuid4())
    wi = CollabWorkItem(
        work_item_id=wid,
        topic_id=topic.topic_id,
        task_id="",
        title="圈定用例并检查",
        goal="按任务范围统计可执行与阻塞",
        acceptance="生成范围快照",
        assignee_kind="bot",
        holder_user_id=holder_user_id,
        input_version=str(topic.input_version or "v1"),
        idempotency_key=f"scope:{topic.topic_id}:{topic.input_version}",
        status="pending",
        action_kind="scope",
        action_payload={"app_id": topic.app_id or ""},
        origin="steward",
        activity="等待圈定范围",
        stage="scope",
        created_at=now,
        updated_at=now,
    )
    db.add(wi)
    return wi


def ensure_run_work_item(
    db,
    topic: CollabTopic,
    holder_user_id: str,
    case_ids: list[str],
    *,
    depends_on: list[str] | None = None,
    origin: str = "steward",
) -> CollabWorkItem | None:
    if not case_ids:
        return None
    key = hashlib_key(case_ids)
    existing = (
        db.query(CollabWorkItem)
        .filter(
            CollabWorkItem.topic_id == topic.topic_id,
            CollabWorkItem.action_kind.in_(tuple(RUN_KINDS)),
            CollabWorkItem.status.in_(("pending", "running", "waiting")),
            CollabWorkItem.idempotency_key == f"run:{topic.topic_id}:{key}",
        )
        .first()
    )
    if existing is not None:
        return existing
    now = _now()
    wid = str(uuid.uuid4())
    wi = CollabWorkItem(
        work_item_id=wid,
        topic_id=topic.topic_id,
        task_id="",
        title=f"执行 {len(case_ids)} 条用例",
        goal="提交跑批并等待证据",
        acceptance="run_store 终态",
        assignee_kind="bot",
        holder_user_id=holder_user_id,
        input_version=str(topic.input_version or "v1"),
        idempotency_key=f"run:{topic.topic_id}:{key}",
        status="pending",
        action_kind="run",
        action_payload={"app_id": topic.app_id or "", "case_ids": case_ids},
        case_ids=case_ids,
        depends_on=list(depends_on or []),
        origin=origin,
        activity=f"排队执行 {len(case_ids)} 条用例",
        stage="run",
        created_at=now,
        updated_at=now,
    )
    db.add(wi)
    return wi


def hashlib_key(case_ids: list[str]) -> str:
    import hashlib

    raw = ",".join(sorted(case_ids))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def execute_scope(topic: CollabTopic, wi: CollabWorkItem, policy: dict[str, Any]) -> dict[str, Any]:
    from mino_nexus.core.database import session_scope

    app_id = str(topic.app_id or (wi.action_payload or {}).get("app_id") or "").strip()
    wid = wi.work_item_id
    run_enqueued = False
    case_ids: list[str] = []
    project_id = ""
    holder = ""
    with session_scope() as db:
        row = db.get(CollabTopic, topic.topic_id)
        wi_row = db.get(CollabWorkItem, wid)
        if row is None or wi_row is None:
            return {"status": "failed", "message": "topic_missing"}
        case_ids = resolve_scope_case_ids(db, row)
        if not case_ids:
            return {"status": "blocked", "message": "任务范围内没有用例；请指定需求、模块或用例集"}
        project_id = str(row.project_id or "")
        holder = str(wi_row.holder_user_id or row.lead_user_id or "")

    snap = build_scope_snapshot(project_id, app_id, case_ids)
    msg = (
        f"已圈定 {snap['total']} 条：可执行 {len(snap['executable'])}，阻塞 {len(snap['blocked'])}"
        if snap.get("total") is not None
        else ""
    )

    with session_scope() as db:
        row = db.get(CollabTopic, topic.topic_id)
        wi_row = db.get(CollabWorkItem, wid)
        if row is None or wi_row is None:
            return {"status": "failed", "message": "topic_missing"}
        row.scope_snapshot = snap
        row.scope_case_ids = case_ids
        row.updated_at = _now()
        wi_row.case_ids = case_ids
        wi_row.evidence = snap
        _set_work_activity(wi_row, msg, stage="scope")
        log_activity(
            db,
            row.topic_id,
            kind="scope",
            body=msg,
            refs={"executable": len(snap["executable"]), "blocked": len(snap["blocked"])},
        )
        from mino_nexus.services.mino_bots import _append_bot_message

        if msg:
            _append_bot_message(db, row.topic_id, f"【测试管家】{msg}", bot_id="", now=_now())
        auto = bool(policy.get("auto_execute", True))
        if auto and snap.get("executable"):
            if ensure_run_work_item(
                db,
                row,
                holder,
                list(snap["executable"]),
                depends_on=[wid],
            ):
                run_enqueued = True
        db.flush()
    if run_enqueued:
        from mino_nexus.services.collab_worker import wake

        wake()
    wi.case_ids = case_ids
    wi.evidence = snap
    return {"status": "ok", "evidence": snap}


def bootstrap_task_from_case(
    project_id: str,
    topic_id: str,
    sess: dict[str, Any],
    *,
    case_id: str,
    app_id: str,
) -> None:
    from mino_nexus.core.database import session_scope

    uid = str((sess or {}).get("user_id") or "").strip()
    with session_scope() as db:
        topic = db.get(CollabTopic, topic_id)
        if topic is None or topic.project_id != project_id:
            return
        cleanup_legacy_work_items(db, topic_id)
        topic.scope_case_ids = [case_id]
        topic.app_id = str(app_id or topic.app_id or "").strip()
        topic.title = topic.title or f"测试：{case_id}"
        topic.goal = topic.goal or f"执行用例 {case_id}"
        topic.updated_at = _now()
        ensure_scope_work_item(db, topic, uid)
        log_activity(db, topic_id, kind="bootstrap", body="已从用例页创建测试任务", refs={"case_id": case_id})
        db.flush()
    from mino_nexus.services.collab_worker import wake

    wake()


def start_steward_flow(project_id: str, topic_id: str, sess: dict[str, Any]) -> dict[str, Any]:
    from mino_nexus.core.database import session_scope

    uid = str((sess or {}).get("user_id") or "").strip()
    with session_scope() as db:
        topic = db.get(CollabTopic, topic_id)
        if topic is None or topic.project_id != project_id:
            raise KeyError("topic_not_found")
        cleanup_legacy_work_items(db, topic_id)
        if int(topic.paused or 0):
            return {"ok": False, "message": "任务已暂停，说「继续」后再派发。"}
        ensure_scope_work_item(db, topic, uid)
        db.flush()
    from mino_nexus.services.collab_worker import wake

    wake()
    return {"ok": True, "message": "测试管家已开始圈定与检查，完成后会自动派发可执行用例。"}


def format_status_report(project_id: str, topic_id: str, sess: dict[str, Any]) -> str:
    from mino_nexus.core.database import session_scope
    from mino_nexus.services import run_store
    from mino_nexus.services.mino_bots import _work_item_public
    from mino_nexus.services.project_members import require_maintainer

    require_maintainer(project_id, sess)
    with session_scope() as db:
        row = db.get(CollabTopic, topic_id)
        if row is None or row.project_id != project_id:
            return "未找到测试任务。"
        snap = dict(row.scope_snapshot or {}) if isinstance(row.scope_snapshot, dict) else {}
        wi_rows = db.query(CollabWorkItem).filter_by(topic_id=topic_id).order_by(CollabWorkItem.created_at).all()
        items = work_items_for_ui([_work_item_public(r) for r in wi_rows])
    total = int(snap.get("total") or 0)
    executable = len(snap.get("executable") or [])
    blocked_n = len(snap.get("blocked") or [])
    items = [i for i in items if str(i.get("status")) != "cancelled"]
    runs = [i for i in items if str(i.get("action_kind")) in RUN_KINDS]
    waiting = [r for r in runs if str(r.get("status")) == "waiting"]
    done_runs = [r for r in runs if str(r.get("status")) in ("done", "failed")]
    parts = [f"范围共 {total} 条用例，可执行 {executable}，阻塞 {blocked_n}。"]
    for r in waiting:
        rid = str(r.get("run_id") or "")
        doc = run_store.get(rid) if rid else None
        if doc:
            cases = doc.get("cases") or []
            done = sum(1 for c in cases if isinstance(c, dict) and str(c.get("status") or "") not in ("", "pending", "running"))
            parts.append(f"跑批进行中 {done}/{len(cases) or len(r.get('case_ids') or [])}（run {rid[:12]}…）。")
        else:
            parts.append(f"跑批等待回写（run {rid[:12] if rid else '…'}）。")
    if not waiting and done_runs:
        last = done_runs[-1]
        ev = last.get("evidence") if isinstance(last.get("evidence"), dict) else {}
        failed = len(ev.get("failed_case_ids") or [])
        parts.append(f"最近一批：{last.get('status')}，失败 {failed} 条。")
    elif not runs and total:
        parts.append("尚未派发跑批；说「跑吧」或等待自动执行。")
    return "".join(parts)


def detect_fast_intent(text: str) -> str:
    t = str(text or "").strip()
    if not t:
        return "empty"
    if _STATUS_RE.search(t):
        return "report_status"
    if _RERUN_RE.search(t):
        return "rerun"
    if _CREATE_RE.search(t):
        return "create_task"
    return "llm"


def list_activities(project_id: str, topic_id: str, sess: dict[str, Any], *, limit: int = 80) -> list[dict[str, Any]]:
    from mino_nexus.core.database import session_scope
    from mino_nexus.services.project_members import require_maintainer

    require_maintainer(project_id, sess)
    with session_scope() as db:
        topic = db.get(CollabTopic, topic_id)
        if topic is None or topic.project_id != project_id:
            raise KeyError("topic_not_found")
        rows = (
            db.query(CollabActivity)
            .filter_by(topic_id=topic_id)
            .order_by(CollabActivity.created_at.desc())
            .limit(limit)
            .all()
        )
        return [
            {
                "activity_id": r.activity_id,
                "topic_id": r.topic_id,
                "kind": r.kind or "",
                "body": r.body or "",
                "refs": dict(r.refs or {}),
                "created_at": r.created_at or "",
            }
            for r in reversed(rows)
        ]


def steward_overview(topic: dict[str, Any], work_items: list[dict[str, Any]]) -> dict[str, Any]:
    snap = topic.get("scope_snapshot") if isinstance(topic.get("scope_snapshot"), dict) else {}
    total = int(snap.get("total") or 0)
    executable = len(snap.get("executable") or [])
    blocked = len(snap.get("blocked") or [])
    runs = [w for w in work_items if str(w.get("action_kind")) in RUN_KINDS and w.get("status") != "cancelled"]
    last_pass = 0
    last_fail = 0
    for r in reversed(runs):
        if str(r.get("status")) not in ("done", "failed"):
            continue
        ev = r.get("evidence") if isinstance(r.get("evidence"), dict) else {}
        failed = ev.get("failed_case_ids") or []
        case_ids = r.get("case_ids") or []
        n = len(case_ids) if case_ids else 0
        last_fail = len(failed) if isinstance(failed, list) else 0
        last_pass = max(0, n - last_fail)
        break
    stages = [
        {"id": "scope", "label": "圈定", "status": "done" if total else "pending"},
        {"id": "check", "label": "检查", "status": "done" if total else "pending"},
        {
            "id": "run",
            "label": "执行",
            "status": "running" if any(str(r.get("status")) in ("waiting", "running") for r in runs) else ("done" if runs else "pending"),
        },
        {
            "id": "diagnose",
            "label": "诊断",
            "status": "done" if any(str(w.get("action_kind")) == "diagnose" and w.get("status") == "done" for w in work_items) else "pending",
        },
    ]
    progress = ""
    if total:
        progress = f"{last_pass + last_fail}/{total}" if (last_pass or last_fail) else f"0/{total}"
    return {
        "total": total,
        "executable": executable,
        "blocked": blocked,
        "last_pass": last_pass,
        "last_fail": last_fail,
        "progress_hint": progress,
        "stages": stages,
    }


_LEGACY_MSG_MARKERS = (
    "已拆解为",
    "同步",
    "工作项",
    "需求分析",
    "用例候选",
    "推进 QA",
    "多角色评审",
    "执行账号已固化",
    "从用例页进入",
    "编号：case-",
    "步骤摘要",
    "运行测试分析",
    "qa_cover",
    "确认写入",
    "议题里还没有",
    "协作助手",
)


def is_legacy_bot_message(body: str) -> bool:
    text = str(body or "").strip()
    if not text:
        return True
    if text.startswith("【测试管家】"):
        return False
    if text.startswith("【协作助手】") or text.startswith("【测试 Bot】"):
        return True
    return any(m in text for m in _LEGACY_MSG_MARKERS)


def filter_messages_for_ui(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in messages:
        if str(row.get("author_kind") or "") == "user":
            out.append(row)
            continue
        if is_legacy_bot_message(str(row.get("body") or "")):
            continue
        out.append(row)
    return out


def work_items_for_ui(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        st = str(row.get("status") or "")
        kind = str(row.get("action_kind") or "")
        if st == "cancelled":
            continue
        if kind in LEGACY_KINDS:
            continue
        activity = str(row.get("activity") or "").strip()
        if not activity and kind in STEWARD_KINDS | RUN_KINDS:
            activity = str(row.get("title") or kind)
        if not activity:
            continue
        row = dict(row)
        row["activity"] = activity
        out.append(row)
    return out


def try_match_requirement(project_id: str, phrase: str) -> tuple[str, str]:
    """返回 (requirement_id, title)；匹配不到则 ('', '')。"""
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.case import ProjectCase

    q = str(phrase or "").strip()
    if not q:
        return "", ""
    with session_scope() as db:
        rows = (
            db.query(ProjectCase.requirement_id)
            .filter(ProjectCase.project_id == project_id, ProjectCase.requirement_id != "")
            .distinct()
            .all()
        )
        ids = [str(r[0]) for r in rows if r and r[0]]
    for rid in ids:
        if rid in q or q in rid:
            return rid, rid
    return "", ""
