"""事件驱动协作 worker（§3.3）。"""
from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text

from mino_nexus.core.log import SLog
from mino_nexus.models.mino_bots import CollabTopic, CollabWorkItem
from mino_nexus.services.collab_steward import LEGACY_KINDS, RUN_KINDS

TAG = "CollabWorker"
LEASE_SECONDS = 300
_WAKE = threading.Event()
_LAST_EXPIRE_CLAIMS_AT = 0.0
_EXPIRE_CLAIMS_INTERVAL_SEC = 45.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def wake() -> None:
    _WAKE.set()


def bump_waiting_for_run(run_id: str) -> None:
    from mino_nexus.core.database import session_scope

    rid = str(run_id or "").strip()
    if not rid:
        return
    stamp = _now()
    with session_scope() as db:
        rows = (
            db.query(CollabWorkItem)
            .filter_by(run_id=rid, status="waiting")
            .all()
        )
        for wi in rows:
            wi.next_check_at = stamp
            wi.updated_at = stamp
        db.flush()
    wake()


def _deps_satisfied(db, wi: CollabWorkItem) -> bool:
    deps = wi.depends_on if isinstance(wi.depends_on, list) else []
    if not deps:
        return True
    for dep_id in deps:
        dep = db.get(CollabWorkItem, str(dep_id))
        if dep is None or str(dep.status or "") != "done":
            return False
    return True


def _running_case_runs(db, project_id: str) -> int:
    topic_ids = [r[0] for r in db.query(CollabTopic.topic_id).filter_by(project_id=project_id).all()]
    if not topic_ids:
        return 0
    return (
        db.query(CollabWorkItem)
        .filter(
            CollabWorkItem.topic_id.in_(topic_ids),
            CollabWorkItem.action_kind.in_(tuple(RUN_KINDS)),
            CollabWorkItem.status.in_(("running", "waiting")),
        )
        .count()
    )


def expire_claims() -> int:
    global _LAST_EXPIRE_CLAIMS_AT
    now_mono = time.time()
    if now_mono - _LAST_EXPIRE_CLAIMS_AT < _EXPIRE_CLAIMS_INTERVAL_SEC:
        return 0
    _LAST_EXPIRE_CLAIMS_AT = now_mono

    from mino_nexus.core.database import session_scope

    cutoff = time.time() - LEASE_SECONDS
    n = 0
    with session_scope() as db:
        rows = db.query(CollabWorkItem).filter_by(status="running").all()
        for wi in rows:
            claimed_at = str(wi.claimed_at or "")
            try:
                ts = datetime.fromisoformat(claimed_at.replace("Z", "+00:00")).timestamp() if claimed_at else 0
            except ValueError:
                ts = 0
            if not wi.claimed_by or ts < cutoff:
                wi.status = "pending"
                wi.claimed_by = ""
                wi.claimed_at = ""
                wi.updated_at = _now()
                n += 1
        db.flush()
    return n


def _topic_policy(db, topic: CollabTopic) -> dict[str, Any]:
    from mino_nexus.services.collab_bot_scope import resolve_topic_policy

    return resolve_topic_policy(db, topic)


def _filter_cases(topic: CollabTopic, case_ids: list[str], policy: dict[str, Any]) -> tuple[list[str], list[str]]:
    from mino_nexus.services.collab_bot_scope import filter_case_ids_by_platform

    platforms = policy.get("_bot_platforms") if isinstance(policy.get("_bot_platforms"), list) else []
    return filter_case_ids_by_platform(topic.project_id, case_ids, platforms)


def _run_terminal(doc: dict[str, Any] | None) -> bool:
    from mino_nexus.services import run_store

    if not doc:
        return False
    return not run_store.task_is_live(doc)


def _case_run_evidence(doc: dict[str, Any], case_ids: list[str]) -> dict[str, Any]:
    sessions: list[str] = []
    per_case: dict[str, Any] = {}
    for row in doc.get("cases") or []:
        if not isinstance(row, dict):
            continue
        cid = str(row.get("case_id") or row.get("id") or "").strip()
        if cid:
            per_case[cid] = str(row.get("status") or "")
            sid = str(row.get("session_id") or "").strip()
            if sid:
                sessions.append(sid)
    wanted = [c for c in case_ids if c]
    failed = [c for c in wanted if per_case.get(c) not in ("passed", "ok", "success")]
    if not wanted:
        wanted = list(per_case.keys())
        failed = [c for c, st in per_case.items() if st not in ("passed", "ok", "success")]
    run_st = str(doc.get("status") or "")
    if run_st in ("failed", "cancelled") and wanted:
        failed = wanted
    return {
        "run_id": doc.get("run_id"),
        "run_status": doc.get("status"),
        "session_id": sessions[0] if sessions else "",
        "session_ids": sessions,
        "case_status": per_case,
        "failed_case_ids": failed,
        "all_passed": bool(wanted) and not failed,
    }


def _spawn_followups(db, topic: CollabTopic, wi: CollabWorkItem, evidence: dict[str, Any], policy: dict[str, Any]) -> None:
    failed = list(evidence.get("failed_case_ids") or [])
    if not failed:
        return
    now = _now()
    diag_id = str(uuid.uuid4())
    db.add(
        CollabWorkItem(
            work_item_id=diag_id,
            topic_id=wi.topic_id,
            task_id="",
            title="失败诊断",
            goal=f"诊断 {len(failed)} 条失败用例",
            acceptance="分类并派生后续项",
            assignee_kind="bot",
            holder_user_id=wi.holder_user_id,
            input_version=wi.input_version,
            idempotency_key=f"diag:{wi.work_item_id}:{wi.attempt}",
            status="pending",
            action_kind="diagnose",
            action_payload={"parent_work_item_id": wi.work_item_id, "run_id": wi.run_id, "case_ids": failed},
            case_ids=failed,
            origin="run_failed",
            created_at=now,
            updated_at=now,
        )
    )


def settle_waiting() -> int:
    from mino_nexus.core.database import session_scope
    from mino_nexus.services import run_store
    from mino_nexus.services import case_compilation as cc

    stamp = _now()
    settled = 0
    with session_scope() as db:
        rows = (
            db.query(CollabWorkItem)
            .filter_by(status="waiting")
            .order_by(CollabWorkItem.next_check_at)
            .limit(20)
            .all()
        )
        for wi in rows:
            nca = str(wi.next_check_at or "")
            if nca and nca > stamp:
                continue
            topic = db.get(CollabTopic, wi.topic_id)
            if topic is None:
                continue
            if int(topic.paused or 0):
                continue
            kind = str(wi.action_kind or "")
            if kind in RUN_KINDS:
                doc = run_store.get(str(wi.run_id or ""))
                if doc is None:
                    wi.next_check_at = stamp
                    continue
                if not _run_terminal(doc):
                    wi.next_check_at = stamp
                    continue
                case_ids = [str(x) for x in (wi.case_ids or wi.action_payload.get("case_ids") or []) if str(x).strip()]
                ev = _case_run_evidence(doc, case_ids)
                wi.evidence = ev
                wi.updated_at = stamp
                policy = _topic_policy(db, topic)
                if ev.get("all_passed"):
                    wi.status = "done"
                    wi.blocked_reason = ""
                else:
                    wi.status = "failed"
                    wi.blocked_reason = "部分用例未通过"
                    _spawn_followups(db, topic, wi, ev, policy)
                    wake()
                from mino_nexus.services.collab_digest import on_work_item_terminal, refresh_topic_digest

                refresh_topic_digest(db, wi.topic_id)
                on_work_item_terminal(db, topic, wi, policy=policy)
                settled += 1
            elif kind == "compile":
                payload = wi.action_payload if isinstance(wi.action_payload, dict) else {}
                jobs = payload.get("compile_job_ids") if isinstance(payload.get("compile_job_ids"), list) else []
                project_id = topic.project_id
                all_ok = True
                any_fail = False
                job_status: dict[str, str] = {}
                for entry in jobs:
                    if not isinstance(entry, dict):
                        continue
                    cid = str(entry.get("case_id") or "")
                    jid = str(entry.get("job_id") or "")
                    if not cid or not jid:
                        continue
                    job = cc.get_job(project_id, cid, jid)
                    st = str((job or {}).get("status") or "")
                    job_status[jid] = st
                    if st in ("queued", "running"):
                        all_ok = False
                    elif st != "succeeded":
                        any_fail = True
                if not all_ok:
                    wi.next_check_at = stamp
                    continue
                wi.evidence = {"compile_jobs": job_status}
                policy = _topic_policy(db, topic)
                wi.status = "failed" if any_fail else "done"
                wi.updated_at = stamp
                if not any_fail and policy.get("auto_publish"):
                    has_publish = (
                        db.query(CollabWorkItem)
                        .filter(
                            CollabWorkItem.topic_id == topic.topic_id,
                            CollabWorkItem.action_kind.in_(("publish", "v5_publish")),
                            CollabWorkItem.status.in_(("pending", "waiting", "running")),
                        )
                        .first()
                    )
                    ok_ids = [
                        str(e.get("case_id") or "")
                        for e in jobs
                        if isinstance(e, dict) and job_status.get(str(e.get("job_id") or "")) == "succeeded"
                    ]
                    ok_ids = [x for x in ok_ids if x]
                    if ok_ids and has_publish is None:
                        from mino_nexus.services.collab_publish import enqueue_publish_for_compiled_cases

                        enqueue_publish_for_compiled_cases(
                            db,
                            topic,
                            wi,
                            ok_ids,
                            holder_user_id=str(wi.holder_user_id or topic.lead_user_id or ""),
                            input_version=str(wi.input_version or topic.input_version or ""),
                        )
                        wake()
                from mino_nexus.services.collab_digest import on_work_item_terminal, refresh_topic_digest

                refresh_topic_digest(db, wi.topic_id)
                on_work_item_terminal(db, topic, wi, policy=policy)
                settled += 1
        db.flush()
    return settled


def _execute_apply_drafts(topic: CollabTopic, wi: CollabWorkItem, policy: dict[str, Any]) -> dict[str, Any]:
    from mino_nexus.services.collab_apply import apply_case_drafts

    if not policy.get("auto_apply_drafts", True):
        return {"status": "blocked", "message": "策略关闭自动写入草稿"}
    blob = wi.action_payload.get("artifact") if isinstance(wi.action_payload, dict) else None
    if blob is None:
        blob = (wi.last_result or {}).get("artifact")
    if not isinstance(blob, dict):
        return {"status": "blocked", "message": "缺少草稿 artifact"}
    evidence = apply_case_drafts(
        topic.project_id,
        topic.topic_id,
        blob,
        input_version=str(wi.input_version or topic.input_version or ""),
    )
    case_ids = list(evidence.get("added") or []) + list(evidence.get("modified") or [])
    wi.case_ids = case_ids
    return {"status": "ok", "evidence": evidence}


def _execute_compile(topic: CollabTopic, wi: CollabWorkItem, policy: dict[str, Any]) -> dict[str, Any]:
    from mino_nexus.services import case_compilation as cc
    from mino_nexus.services.case_store import get_case

    if not policy.get("auto_compile", True):
        return {"status": "blocked", "message": "策略关闭自动编译"}
    case_ids = [str(x) for x in (wi.case_ids or wi.action_payload.get("case_ids") or []) if str(x).strip()]
    if not case_ids:
        deps = wi.depends_on if isinstance(wi.depends_on, list) else []
        from mino_nexus.core.database import session_scope

        with session_scope() as db:
            for dep_id in deps:
                dep = db.get(CollabWorkItem, str(dep_id))
                if dep and isinstance(dep.evidence, dict):
                    case_ids.extend(str(x) for x in (dep.evidence.get("added") or []) + (dep.evidence.get("modified") or []))
    case_ids = list(dict.fromkeys(case_ids))
    if not case_ids:
        return {"status": "blocked", "message": "无 case_ids 可编译"}
    kept, skipped = _filter_cases(topic, case_ids, policy)
    if skipped and not kept:
        plats = policy.get("_bot_platforms") or []
        return {
            "status": "blocked",
            "message": f"当前 Bot 平台作用域 {plats} 不包含这些用例的平台",
            "evidence": {"skipped_case_ids": skipped, "bot_platforms": plats},
        }
    case_ids = kept
    jobs: list[dict[str, str]] = []
    request_base = f"collab:{wi.work_item_id}:{int(wi.attempt or 0)}"
    for cid in case_ids:
        case = get_case(topic.project_id, cid)
        if not case:
            continue
        rev = int(case.get("v5_draft_revision") or case.get("expected_revision") or 1)
        job = cc.enqueue(topic.project_id, cid, rev, f"{request_base}:{cid}")
        jobs.append({"case_id": cid, "job_id": str(job.get("job_id") or "")})
    if not jobs:
        return {"status": "blocked", "message": "编译排队失败"}
    wi.action_payload = {**(wi.action_payload or {}), "compile_job_ids": jobs}
    return {"status": "waiting", "compile_job_ids": jobs}


def _execute_case_run(topic: CollabTopic, wi: CollabWorkItem, policy: dict[str, Any]) -> dict[str, Any]:
    from mino_nexus.services import project_store as ps
    from mino_nexus.services.case_preflight import blocked_message, preflight
    from mino_nexus.loop import case_runner as cr

    if not policy.get("auto_execute", True):
        return {"status": "blocked", "message": "策略关闭自动执行"}
    payload = dict(wi.action_payload or {})
    app_id = str(payload.get("app_id") or topic.app_id or "").strip()
    if not app_id:
        return {"status": "blocked", "message": "缺少 app_id"}
    case_ids = [str(x) for x in (wi.case_ids or payload.get("case_ids") or []) if str(x).strip()]
    if not case_ids:
        anchor = topic.anchor if isinstance(topic.anchor, dict) else {}
        cid = str(anchor.get("case_id") or "").strip()
        if cid:
            case_ids = [cid]
    if not case_ids:
        return {"status": "blocked", "message": "缺少 case_ids"}
    kept, skipped = _filter_cases(topic, case_ids, policy)
    if skipped and not kept:
        plats = policy.get("_bot_platforms") or []
        return {
            "status": "blocked",
            "message": f"当前 Bot 仅覆盖平台 {plats}，已跳过 {len(skipped)} 条用例",
            "evidence": {"skipped_case_ids": skipped, "bot_platforms": plats},
        }
    case_ids = kept
    env_profile = str(payload.get("env_profile") or "test").strip()
    surface = str(payload.get("env_surface") or payload.get("surface") or "").strip()
    from mino_nexus.services.collab_bot_scope import execution_env_allowed

    if not execution_env_allowed(policy, env_profile=env_profile, surface=surface):
        return {
            "status": "blocked",
            "message": f"环境 {env_profile} 不在策略允许列表 {policy.get('execute_env_profiles')}",
        }
    try:
        app = ps.require_app(app_id)
    except KeyError:
        return {"status": "blocked", "message": "应用不存在"}
    if str(app.get("project_id") or "") != topic.project_id:
        return {"status": "blocked", "message": "应用不属于项目"}
    try:
        summary = preflight(app, case_ids)
    except ValueError as exc:
        return {"status": "blocked", "message": str(exc)}
    blocked = list(summary.get("blocked") or [])
    if blocked:
        return {"status": "blocked", "message": blocked_message(blocked), "preflight": summary}
    from mino_nexus.services.case_store import get_case

    first = get_case(topic.project_id, case_ids[0]) or {}
    run_platform = str(payload.get("platform") or first.get("platform") or "android").strip()
    client_request_id = f"collab:{wi.work_item_id}:{int(wi.attempt or 0)}"
    holder = str(wi.holder_user_id or "").strip()
    out = cr.run_cases(
        app,
        sn=str(payload.get("sn") or ""),
        case_ids=case_ids,
        async_exec=True,
        platform=run_platform,
        plugin_user_id=holder,
        run_type="collab",
        client_request_id=client_request_id,
    )
    run_id = str((out or {}).get("run_id") or (out or {}).get("task_id") or "")
    wi.run_id = run_id
    wi.case_ids = case_ids
    return {"status": "waiting", "run_id": run_id, "run": out}


def _execute_diagnose(topic: CollabTopic, wi: CollabWorkItem, policy: dict[str, Any]) -> dict[str, Any]:
    from mino_nexus.services import case_review as creview
    from mino_nexus.services.case_store import get_case

    case_ids = [str(x) for x in (wi.case_ids or wi.action_payload.get("case_ids") or []) if str(x).strip()]
    derived: list[str] = []
    classifications: list[dict[str, Any]] = []
    toolchain = False
    for cid in case_ids:
        desc = creview.describe(get_case(topic.project_id, cid) or {})
        items = desc.get("requirements") if isinstance(desc.get("requirements"), list) else []
        cat = "business"
        for item in items:
            if not isinstance(item, dict):
                continue
            ctx = str(item.get("context") or item.get("category") or "")
            if "resource" in ctx or "compile" in ctx or "binding" in ctx or "tool" in ctx:
                cat = "toolchain"
                toolchain = True
        classifications.append({"case_id": cid, "category": cat})
    parent_id = str((wi.action_payload or {}).get("parent_work_item_id") or "")
    max_retry = int(policy.get("auto_retry_toolchain") or 0)
    if toolchain and parent_id and max_retry > 0:
        from mino_nexus.core.database import session_scope

        with session_scope() as db:
            parent = db.get(CollabWorkItem, parent_id)
            if parent and int(parent.attempt or 0) < max_retry:
                parent.status = "pending"
                parent.attempt = int(parent.attempt or 0) + 1
                parent.run_id = ""
                parent.updated_at = _now()
                derived.append(parent.work_item_id)
    else:
        from mino_nexus.core.database import session_scope

        now = _now()
        with session_scope() as db:
            for cid in case_ids:
                if any(c.get("category") == "toolchain" for c in classifications if c.get("case_id") == cid):
                    continue
                defect_id = str(uuid.uuid4())
                db.add(
                    CollabWorkItem(
                        work_item_id=defect_id,
                        topic_id=wi.topic_id,
                        task_id="",
                        title=f"缺陷：{cid}",
                        goal="业务类失败，需人工修复后复验",
                        acceptance="证据已附 session",
                        assignee_kind="human",
                        holder_user_id=wi.holder_user_id,
                        input_version=wi.input_version,
                        idempotency_key=f"defect:{wi.work_item_id}:{cid}",
                        status="blocked",
                        action_kind="defect",
                        action_payload={"case_id": cid, "run_id": wi.action_payload.get("run_id")},
                        case_ids=[cid],
                        origin="diagnose",
                        created_at=now,
                        updated_at=now,
                    )
                )
                derived.append(defect_id)
            db.flush()
    return {"status": "ok", "evidence": {"classifications": classifications, "derived": derived}}


def _execute_llm_job(topic: CollabTopic, wi: CollabWorkItem) -> dict[str, Any]:
    from mino_nexus.services.case_store import list_cases
    from mino_nexus.services.qa_cover import run_llm_job

    payload = dict(wi.action_payload or {})
    job = str(payload.get("job") or "analyze_req").strip()
    cases = list_cases(topic.project_id, topic.topic_id)
    requirement = {
        "id": topic.topic_id,
        "title": wi.title or topic.title,
        "source_text": wi.goal or topic.goal,
        "human_feedback": str(payload.get("human_feedback") or ""),
    }
    art = run_llm_job(job, requirement=requirement, cases=cases, qa_process=None)
    if art.get("status") != "ok":
        return {"status": "failed", "message": str(art.get("error") or "llm_job_failed"), "artifact": art}
    wi.last_result = {"status": "ok", "artifact": art}
    if job == "draft_cases" and isinstance(wi.action_payload, dict):
        wi.action_payload = {**wi.action_payload, "artifact": art}
    return {"status": "ok", "artifact": art}


def _execute_publish(topic: CollabTopic, wi: CollabWorkItem, policy: dict[str, Any]) -> dict[str, Any]:
    from mino_nexus.services.collab_publish import publish_payload_for_case, run_publish_case

    if not policy.get("auto_publish"):
        return {"status": "blocked", "message": "项目策略未开启 auto_publish"}
    payload = dict(wi.action_payload or {})
    case_ids = [str(x) for x in (wi.case_ids or []) if str(x).strip()]
    if not case_ids and payload.get("case_id"):
        case_ids = [str(payload["case_id"]).strip()]
    if not case_ids:
        return {"status": "blocked", "message": "缺少要发布的 case_id"}
    kept, skipped = _filter_cases(topic, case_ids, policy)
    if skipped and not kept:
        return {"status": "blocked", "message": "平台作用域内无可发布用例", "evidence": {"skipped": skipped}}
    evidence_cases: list[dict[str, Any]] = []
    for cid in kept:
        pl = {**publish_payload_for_case(topic.project_id, cid), **payload, "case_id": cid}
        res = run_publish_case(topic.project_id, cid, pl, auto=True, work_item_id=wi.work_item_id)
        if str(res.get("status") or "") != "ok":
            if isinstance(res.get("evidence"), dict):
                wi.evidence = res["evidence"]
            return res
        ev = res.get("evidence") if isinstance(res.get("evidence"), dict) else {}
        if ev:
            evidence_cases.append(ev)
    wi.case_ids = kept
    return {
        "status": "ok",
        "evidence": {"published": evidence_cases, "skipped_case_ids": skipped, "v5_plan_state": evidence_cases[-1].get("v5_plan_state") if evidence_cases else None},
        "message": f"已发布 {len(kept)} 条用例",
    }


def _execute_item(wi: CollabWorkItem, topic: CollabTopic, policy: dict[str, Any]) -> None:
    kind = str(wi.action_kind or "none")
    result: dict[str, Any]
    if kind == "scope":
        from mino_nexus.services.collab_steward import execute_scope

        result = execute_scope(topic, wi, policy)
    elif kind == "apply_drafts":
        result = _execute_apply_drafts(topic, wi, policy)
    elif kind == "compile":
        result = _execute_compile(topic, wi, policy)
    elif kind in RUN_KINDS:
        result = _execute_case_run(topic, wi, policy)
    elif kind == "diagnose":
        result = _execute_diagnose(topic, wi, policy)
    elif kind == "llm_job":
        result = _execute_llm_job(topic, wi)
    elif kind in ("publish", "v5_publish"):
        result = _execute_publish(topic, wi, policy)
    elif kind == "none":
        result = {"status": "ok"}
    else:
        result = {"status": "blocked", "message": f"worker 未实现动作 {kind}"}

    now = _now()
    st = str(result.get("status") or "error")
    if st == "ok":
        if isinstance(result.get("evidence"), dict):
            wi.evidence = result["evidence"]
        wi.status = "done"
        wi.blocked_reason = ""
    elif st == "waiting":
        wi.status = "waiting"
        wi.next_check_at = now
        wi.blocked_reason = ""
    elif st == "blocked":
        wi.status = "blocked"
        wi.blocked_reason = str(result.get("message") or "blocked")
        if isinstance(result.get("evidence"), dict):
            wi.evidence = result["evidence"]
    else:
        wi.status = "failed"
        wi.blocked_reason = str(result.get("message") or st)
    wi.updated_at = now
    wi.claimed_by = ""
    wi.claimed_at = ""


def run_one(worker_id: str) -> bool:
    from mino_nexus.core.database import session_scope
    from mino_nexus.core.execution_user import execution_user

    claimed: tuple[CollabWorkItem, CollabTopic, dict[str, Any]] | None = None
    with session_scope() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        topics = {t.topic_id: t for t in db.query(CollabTopic).filter_by(status="open").all()}
        paused = {tid for tid, t in topics.items() if int(t.paused or 0)}
        rows = (
            db.query(CollabWorkItem)
            .filter(CollabWorkItem.status == "pending")
            .order_by(CollabWorkItem.updated_at, CollabWorkItem.created_at)
            .all()
        )
        for wi in rows:
            # Preserve withdrawn workspace schedules without ever dispatching them.
            if wi.origin == "device_online":
                continue
            if wi.topic_id in paused:
                continue
            topic = topics.get(wi.topic_id) or db.get(CollabTopic, wi.topic_id)
            if topic is None or str(topic.status or "") != "open":
                continue
            if not _deps_satisfied(db, wi):
                continue
            kind = str(wi.action_kind or "")
            if kind in LEGACY_KINDS:
                continue
            if kind in RUN_KINDS:
                policy = _topic_policy(db, topic)
                max_par = int(policy.get("max_parallel_runs") or 1)
                if _running_case_runs(db, topic.project_id) >= max_par:
                    continue
            wi.status = "running"
            wi.claimed_by = worker_id
            wi.claimed_at = _now()
            wi.updated_at = _now()
            policy = _topic_policy(db, topic)
            claimed = (wi, topic, policy)
            break
        db.flush()
        if claimed is not None:
            # Keep a loaded snapshot for execution outside the transaction.
            # session_scope commits with expire_on_commit=True by default.
            db.expunge(claimed[0])
            db.expunge(claimed[1])

    if claimed is None:
        return False

    wi, topic, policy = claimed
    holder = str(wi.holder_user_id or topic.lead_user_id or "").strip()
    try:
        with execution_user(holder):
            _execute_item(wi, topic, policy)
    except Exception as exc:
        SLog.w(TAG, f"execute {wi.work_item_id}: {exc}")
        wi.status = "failed"
        wi.blocked_reason = str(exc)
        wi.updated_at = _now()

    with session_scope() as db:
        row = db.get(CollabWorkItem, wi.work_item_id)
        if row is None:
            return True
        row.status = wi.status
        row.blocked_reason = wi.blocked_reason
        row.evidence = wi.evidence
        row.run_id = wi.run_id
        row.case_ids = wi.case_ids
        row.last_result = wi.last_result
        row.action_payload = wi.action_payload
        row.next_check_at = wi.next_check_at
        row.claimed_by = ""
        row.claimed_at = ""
        row.updated_at = wi.updated_at
        topic_row = db.get(CollabTopic, row.topic_id)
        if topic_row is not None:
            from mino_nexus.services.collab_digest import on_work_item_terminal, refresh_topic_digest

            refresh_topic_digest(db, row.topic_id)
            if row.status in ("done", "failed", "blocked"):
                on_work_item_terminal(db, topic_row, row, policy=policy)
        db.flush()
    wake()
    return True


def tick(worker_id: str) -> bool:
    settle_waiting()
    return run_one(worker_id)
