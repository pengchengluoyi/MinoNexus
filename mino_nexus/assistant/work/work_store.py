"""工作层状态存储。

所有写操作走 `work_session()`：先 `BEGIN IMMEDIATE`，commit 后才发 WS 事件。
工作层不复制 case.run 的状态；run 的实时状态一律读 `run_store`。
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from sqlalchemy import and_, case, func, or_, text
from sqlalchemy.orm import Session

from mino_nexus.contract.events import publish_event
from mino_nexus.core.database import SessionLocal
from mino_nexus.models.work_agent_memory import WorkAgentMemory
from mino_nexus.models.work_agent_session import WorkAgentSession
from mino_nexus.models.work_approval import WorkApproval
from mino_nexus.models.work_artifact import WorkArtifact
from mino_nexus.models.work_event import WorkEvent
from mino_nexus.models.work_item import WorkItem
from mino_nexus.models.work_link import WorkLink
from mino_nexus.models.work_step import WorkStep

WORK_STATUSES = (
    "draft", "planning", "running", "waiting_human",
    "blocked", "paused", "done", "failed", "cancelled",
)
STEP_STATUSES = (
    "pending", "ready", "running", "waiting",
    "done", "failed", "skipped", "cancelled", "unknown",
)
TERMINAL_WORK = {"done", "failed", "cancelled"}
TERMINAL_STEP = {"done", "failed", "skipped", "cancelled", "unknown"}
RECOVERABLE = {"agent.plan", "agent.turn", "job.call", "run.wait", "work.review"}
DEFAULT_BUDGET = {
    "max_steps": 24,
    "max_turns": 12,
    "max_tool_calls": 12,
    "max_tokens": 60000,
    "max_wall_clock_sec": 1800,
}
LEASE_SECONDS = 90.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _id(prefix: str) -> str:
    return prefix + "_" + uuid.uuid4().hex[:20]


def _begin(db: Session) -> None:
    db.execute(text("BEGIN IMMEDIATE"))


@contextmanager
def work_session(*, write: bool = True) -> Iterator[Session]:
    db = SessionLocal()
    ok = False
    try:
        if write:
            _begin(db)
        yield db
        db.commit()
        ok = True
    except Exception:
        db.rollback()
        raise
    finally:
        events = list(db.info.pop("work_emit", []) or [])
        db.close()
        if ok:
            for event in events:
                try:
                    publish_event(event)
                except Exception:
                    pass
            changed = {
                str((e.get("data") or {}).get("work_id") or "")
                for e in events
                if str((e.get("data") or {}).get("type") or "") in {
                    "work.created", "plan.ready", "work.paused", "work.resumed",
                    "work.blocked", "work.cancelled", "work.completed", "work.instruction",
                    "approval.requested", "approval.approved", "approval.rejected",
                    "step.unknown_on_recovery", "step.resolved", "step.retry",
                    "step.skipped", "step.takeover", "work.handoff",
                }
            }
            if changed:
                try:
                    from mino_nexus.assistant.work.work_notify import sync_card
                    for work_id in changed:
                        if work_id:
                            sync_card(work_id)
                except Exception:
                    pass


def _payload(work: WorkItem, event: WorkEvent) -> dict[str, Any]:
    return {
        "type": "work_event",
        "data": {
            "event_id": event.event_id,
            "work_id": work.work_id,
            "project_id": work.project_id,
            "step_id": event.step_id,
            "seq": event.seq,
            "type": event.type,
            "actor": event.actor,
            "payload": event.payload or {},
            "created_at": event.created_at,
        },
    }


def append_event(db: Session, work: WorkItem, event_type: str, *,
                 step_id: str = "", actor: str = "system",
                 payload: dict[str, Any] | None = None) -> WorkEvent:
    work.last_event_seq = int(work.last_event_seq or 0) + 1
    event = WorkEvent(
        event_id=_id("we"),
        work_id=work.work_id,
        step_id=step_id or "",
        seq=work.last_event_seq,
        type=event_type,
        actor=actor or "system",
        payload=payload or {},
        created_at=_now(),
    )
    db.add(event)
    db.flush()
    db.info.setdefault("work_emit", []).append(_payload(work, event))
    return event


def normalize_acceptance(items: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if isinstance(items, str):
        items = [items]
    for idx, item in enumerate(items or [], 1):
        if isinstance(item, str):
            text = item.strip()
            if text:
                out.append({"id": f"a{idx}", "text": text, "required": True})
            continue
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or item.get("criterion") or item.get("title") or "").strip()
        if not text:
            continue
        out.append({
            "id": str(item.get("id") or f"a{idx}")[:32],
            "text": text[:500],
            "required": bool(item.get("required", True)),
        })
    return out[:20]


def _budget(raw: Any) -> dict[str, int]:
    merged = dict(DEFAULT_BUDGET)
    if isinstance(raw, dict):
        for key in DEFAULT_BUDGET:
            try:
                value = int(raw.get(key))
            except (TypeError, ValueError):
                continue
            if value > 0:
                merged[key] = value
    merged["max_steps"] = min(merged["max_steps"], 40)
    merged["max_tool_calls"] = min(merged["max_tool_calls"], 20)
    return merged


def _step_public(step: WorkStep) -> dict[str, Any]:
    return {
        "step_id": step.step_id,
        "work_id": step.work_id,
        "seq": step.seq,
        "step_key": step.step_key,
        "plan_step_id": step.plan_step_id or "",
        "kind": step.kind,
        "agent_id": step.agent_id or "",
        "job_id": step.job_id or "",
        "role_id": step.role_id or "",
        "title": step.title or "",
        "intent": step.intent or "",
        "status": step.status,
        "input": step.input_json or {},
        "output": step.output_json or {},
        "ref_type": step.ref_type or "",
        "ref_id": step.ref_id or "",
        "parent_step_id": step.parent_step_id or "",
        "attempt": int(step.attempt or 0),
        "max_attempts": int(step.max_attempts or 3),
        "error": step.error or "",
        "created_at": step.created_at or "",
        "updated_at": step.updated_at or "",
        "started_at": step.started_at,
        "finished_at": step.finished_at,
    }


def _approval_public(row: WorkApproval) -> dict[str, Any]:
    return {
        "approval_id": row.approval_id,
        "work_id": row.work_id,
        "step_id": row.step_id or "",
        "kind": row.kind,
        "status": row.status,
        "title": row.title or "",
        "prompt": row.prompt or "",
        "options": row.options_json or [],
        "decision": row.decision_json or {},
        "requested_by": row.requested_by or "",
        "decided_by": row.decided_by or "",
        "created_at": row.created_at or "",
        "decided_at": row.decided_at or "",
        "expires_at": row.expires_at or "",
    }


def _artifact_public(row: WorkArtifact) -> dict[str, Any]:
    return {
        "artifact_id": row.artifact_id,
        "work_id": row.work_id,
        "step_id": row.step_id or "",
        "kind": row.kind,
        "title": row.title or "",
        "ref_type": row.ref_type or "",
        "ref_id": row.ref_id or "",
        "uri": row.uri or "",
        "sha256": row.sha256 or "",
        "meta": row.meta_json or {},
        "created_at": row.created_at or "",
    }


def agent_session_public(row: WorkAgentSession) -> dict[str, Any]:
    return {
        "session_id": row.session_id,
        "work_id": row.work_id,
        "agent_id": row.agent_id or "",
        "job_id": row.job_id or "",
        "role_id": row.role_id or "",
        "status": row.status or "",
        "summary": row.summary or "",
        "last_event_seq": int(row.last_event_seq or 0),
        "created_at": row.created_at or "",
        "updated_at": row.updated_at or "",
    }


def memory_public(row: WorkAgentMemory) -> dict[str, Any]:
    return {
        "memory_id": row.memory_id,
        "scope": row.scope or "",
        "scope_id": row.scope_id or "",
        "agent_id": row.agent_id or "",
        "kind": row.kind or "",
        "content": row.content or "",
        "source_ref": row.source_ref or "",
        "confidence": int(row.confidence or 0),
        "expires_at": row.expires_at or "",
        "created_at": row.created_at or "",
    }


def upsert_agent_session(db: Session, work: WorkItem, *, agent_id: str,
                          role_id: str = "", job_id: str = "",
                          summary: str = "", last_event_seq: int = 0,
                          status: str = "active") -> WorkAgentSession:
    agent = (agent_id or "conductor")[:64]
    row = (
        db.query(WorkAgentSession)
        .filter_by(work_id=work.work_id, agent_id=agent)
        .one_or_none()
    )
    if not row:
        row = WorkAgentSession(
            session_id=_id("was"), work_id=work.work_id, agent_id=agent,
            created_at=_now(),
        )
        db.add(row)
    row.role_id = (role_id or agent)[:64]
    row.job_id = (job_id or "")[:64]
    row.status = (status or "active")[:32]
    row.summary = str(summary or "")[:4000]
    row.last_event_seq = int(last_event_seq or 0)
    row.updated_at = _now()
    db.flush()
    return row


def remember(db: Session, work: WorkItem, *, agent_id: str, kind: str,
             content: str, source_ref: str = "", confidence: int = 50,
             ttl_days: int = 90, scope: str = "work") -> WorkAgentMemory | None:
    text = str(content or "").strip()
    if not text:
        return None
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()
    scope_id = work.work_id
    existing = (
        db.query(WorkAgentMemory)
        .filter_by(scope=scope, scope_id=scope_id, agent_id=(agent_id or "")[:64],
                   content_hash=digest)
        .one_or_none()
    )
    if existing:
        return existing
    expires = ""
    if ttl_days > 0:
        expires = datetime.fromtimestamp(time.time() + ttl_days * 86400, timezone.utc).isoformat()
    row = WorkAgentMemory(
        memory_id=_id("wm"), scope=scope, scope_id=scope_id,
        agent_id=(agent_id or "")[:64], kind=(kind or "fact")[:32],
        content=text[:4000], content_hash=digest,
        source_ref=(source_ref or "")[:160], confidence=max(0, min(100, int(confidence))),
        expires_at=expires, created_at=_now(),
    )
    db.add(row)
    db.flush()
    return row


def list_agent_sessions(db: Session, work_id: str) -> list[dict[str, Any]]:
    rows = db.query(WorkAgentSession).filter_by(work_id=work_id).order_by(WorkAgentSession.created_at.asc()).all()
    return [agent_session_public(r) for r in rows]


def list_memories(db: Session, work_id: str, *, agent_id: str = "", limit: int = 40) -> list[dict[str, Any]]:
    now = _now()
    q = db.query(WorkAgentMemory).filter(
        WorkAgentMemory.scope == "work", WorkAgentMemory.scope_id == work_id,
        or_(WorkAgentMemory.expires_at == "", WorkAgentMemory.expires_at > now),
    )
    if agent_id:
        q = q.filter(or_(WorkAgentMemory.agent_id == agent_id, WorkAgentMemory.agent_id == ""))
    rows = q.order_by(WorkAgentMemory.created_at.desc()).limit(max(1, min(200, limit))).all()
    return [memory_public(r) for r in reversed(rows)]


def public_work(db: Session, work: WorkItem, *, include_counts: bool = True) -> dict[str, Any]:
    counts: dict[str, int] = {}
    if include_counts:
        for status in STEP_STATUSES:
            counts[status] = int(
                db.query(func.count(WorkStep.step_id))
                .filter_by(work_id=work.work_id, status=status)
                .scalar() or 0
            )
    done = int(counts.get("done", 0))
    total = sum(counts.values()) if counts else 0
    return {
        "work_id": work.work_id,
        "project_id": work.project_id,
        "topic_id": work.topic_id or "",
        "owner_user_id": work.owner_user_id,
        "source_message_id": work.source_message_id or "",
        "title": work.title or "",
        "goal": work.goal or "",
        "status": work.status,
        "plan": work.plan_json or {},
        "plan_revision": int(work.plan_revision or 0),
        "plan_index": int(work.plan_index or 0),
        "acceptance": work.acceptance_json or [],
        "review": work.review_json or {},
        "instructions": work.instructions_json or [],
        "policy": work.policy_json or {},
        "budget": work.budget_json or {},
        "spent": work.spent_json or {},
        "blocked_reason": work.blocked_reason or "",
        "stop_requested": int(work.stop_requested or 0),
        "created_at": work.created_at or "",
        "updated_at": work.updated_at or "",
        "finished_at": work.finished_at,
        "progress": {
            "done": done,
            "total": total,
            "percent": round(done * 100 / total) if total else 0,
            "counts": counts,
        },
    }


def get_work(work_id: str) -> dict[str, Any] | None:
    with work_session(write=False) as db:
        row = db.get(WorkItem, work_id)
        return public_work(db, row) if row else None


def owner_of(work_id: str) -> str:
    with work_session(write=False) as db:
        row = db.get(WorkItem, work_id)
        return row.owner_user_id if row else ""


def _create_key(owner: str, project_id: str, topic_id: str, goal: str, key: str) -> str:
    if key:
        return key[:160]
    raw = "\n".join((owner, project_id, topic_id, goal)).encode("utf-8")
    return "auto:" + hashlib.sha256(raw).hexdigest()[:48]


def create_work(*, project_id: str, owner_user_id: str, goal: str,
                title: str = "", topic_id: str = "", source_message_id: str = "",
                create_key: str = "", acceptance: Any = None,
                budget: Any = None, instructions: Any = None,
                policy: Any = None) -> tuple[dict[str, Any], bool]:
    goal = str(goal or "").strip()
    if not goal:
        raise ValueError("goal_required")
    key = _create_key(owner_user_id, project_id, topic_id, goal, create_key)
    if isinstance(instructions, str):
        instructions = [instructions]
    with work_session() as db:
        existing = (
            db.query(WorkItem)
            .filter_by(owner_user_id=owner_user_id, create_key=key)
            .one_or_none()
        )
        if existing:
            return public_work(db, existing), False
        now = _now()
        row = WorkItem(
            work_id=_id("wk"),
            project_id=project_id,
            topic_id=topic_id or "",
            owner_user_id=owner_user_id,
            create_key=key,
            source_message_id=source_message_id or "",
            title=(title or goal)[:200],
            goal=goal,
            status="planning",
            plan_json={},
            plan_revision=0,
            plan_index=0,
            acceptance_json=normalize_acceptance(acceptance),
            review_json={},
            instructions_json=[str(x)[:1000] for x in (instructions or []) if str(x).strip()][:30],
            budget_json=_budget(budget),
            spent_json={"steps": 0, "turns": 0, "tool_calls": 0, "tokens": 0,
                        "started_at": now, "elapsed_sec": 0},
            policy_json=policy or {},
            last_event_seq=0,
            stop_requested=0,
            created_at=now,
            updated_at=now,
        )
        db.add(row)
        db.flush()
        append_event(db, row, "work.created", actor=owner_user_id,
                     payload={"title": row.title, "goal": goal})
        _add_step(db, row, kind="agent.plan", step_key="plan:1",
                  agent_id="conductor", role_id="conductor",
                  title="制定执行计划", intent=goal, status="ready")
        return public_work(db, row), True


def _add_step(db: Session, work: WorkItem, *, kind: str, step_key: str,
              agent_id: str = "", job_id: str = "", role_id: str = "",
              title: str = "", intent: str = "", input_json: Any = None,
              parent_step_id: str = "", plan_step_id: str = "",
              status: str = "ready", max_attempts: int = 3,
              ref_type: str = "", ref_id: str = "") -> WorkStep:
    existing = db.query(WorkStep).filter_by(work_id=work.work_id, step_key=step_key).one_or_none()
    if existing:
        return existing
    seq = int(db.query(func.max(WorkStep.seq)).filter_by(work_id=work.work_id).scalar() or 0) + 1
    step = WorkStep(
        step_id=_id("ws"),
        work_id=work.work_id,
        seq=seq,
        step_key=step_key[:160],
        plan_step_id=plan_step_id or "",
        kind=kind,
        agent_id=agent_id or "",
        job_id=job_id or "",
        role_id=role_id or "",
        title=(title or kind)[:200],
        intent=intent or "",
        status=status,
        input_json=input_json or {},
        output_json={},
        ref_type=ref_type or "",
        ref_id=ref_id or "",
        parent_step_id=parent_step_id or "",
        attempt=0,
        max_attempts=max(1, int(max_attempts or 3)),
        lease_owner="",
        lease_until=None,
        next_run_at=None,
        created_at=_now(),
        updated_at=_now(),
    )
    db.add(step)
    db.flush()
    append_event(db, work, "step.created", step_id=step.step_id,
                 payload={"kind": kind, "title": step.title, "parent_step_id": step.parent_step_id})
    return step


def add_step(work_id: str, **kwargs: Any) -> dict[str, Any]:
    with work_session() as db:
        work = db.get(WorkItem, work_id)
        if not work:
            raise KeyError("work_not_found")
        return _step_public(_add_step(db, work, **kwargs))


def claim_step(worker_id: str) -> dict[str, Any] | None:
    now = time.time()
    with work_session() as db:
        due = or_(WorkStep.next_run_at.is_(None), WorkStep.next_run_at <= now)
        lease_free = or_(WorkStep.lease_until.is_(None), WorkStep.lease_until < now)
        claimable = or_(
            WorkStep.status == "ready",
            and_(WorkStep.status == "waiting", WorkStep.kind == "run.wait"),
        )
        # 同一工作项同时只允许一个在跑步骤；多 worker 时也不会两个步骤抢同一设备。
        busy_work = (
            db.query(WorkStep.work_id)
            .filter(WorkStep.status == "running",
                    or_(WorkStep.lease_until.is_(None), WorkStep.lease_until >= now))
            .subquery()
        )
        step = (
            db.query(WorkStep)
            .join(WorkItem, WorkItem.work_id == WorkStep.work_id)
            .filter(
                WorkItem.status.in_(("planning", "running")),
                func.coalesce(WorkItem.stop_requested, 0) == 0,
                WorkStep.status.in_(("ready", "waiting")),
                claimable,
                due,
                lease_free,
                ~WorkStep.work_id.in_(db.query(busy_work.c.work_id)),
            )
            .order_by(WorkStep.seq.asc())
            .first()
        )
        if not step:
            return None
        was_waiting = step.status == "waiting" and step.kind == "run.wait"
        step.status = "running"
        step.lease_owner = worker_id
        step.lease_until = now + LEASE_SECONDS
        step.last_heartbeat_at = now
        step.next_run_at = None
        if not step.started_at:
            step.started_at = _now()
        if not was_waiting:
            step.attempt = int(step.attempt or 0) + 1
        step.updated_at = _now()
        work = db.get(WorkItem, step.work_id)
        if not was_waiting:
            append_event(db, work, "step.started", step_id=step.step_id,
                         payload={"kind": step.kind, "attempt": int(step.attempt or 0)})
        db.flush()
        result = _step_public(step)
        result["owner_user_id"] = work.owner_user_id
        result["lease_owner"] = worker_id
        return result


def heartbeat(step_id: str, worker_id: str) -> bool:
    now = time.time()
    with work_session() as db:
        step = db.get(WorkStep, step_id)
        if not step or step.status != "running" or step.lease_owner != worker_id:
            return False
        step.lease_until = now + LEASE_SECONDS
        step.last_heartbeat_at = now
        step.updated_at = _now()
        return True


def finish_step_in_db(db: Session, step: WorkStep, *, status: str,
                      output: Any = None, error: str = "",
                      next_run_at: float | None = None) -> bool:
    """在调用方事务内完成步骤；handler 用它保证子步骤/父步骤原子提交。"""
    if status not in STEP_STATUSES:
        raise ValueError("step_status_invalid")
    if step.status == "cancelled":
        return False
    work_row = db.get(WorkItem, step.work_id)
    if work_row is not None and work_row.status == "cancelled":
        step.status = "cancelled"
        step.lease_owner = ""
        step.lease_until = None
        step.finished_at = _now()
        step.updated_at = _now()
        return False
    step.status = status
    step.output_json = output if isinstance(output, dict) else ({"value": output} if output is not None else {})
    step.error = str(error or "")[:2000]
    step.lease_owner = ""
    step.lease_until = None
    step.next_run_at = next_run_at
    step.updated_at = _now()
    if status in TERMINAL_STEP:
        step.finished_at = _now()
    work = db.get(WorkItem, step.work_id)
    append_event(db, work, f"step.{status}", step_id=step.step_id,
                 payload={"kind": step.kind, "error": step.error,
                          "output": step.output_json if status != "waiting" else {}})
    return True


def finish_step(step_id: str, *, worker_id: str = "", status: str,
                output: Any = None, error: str = "", next_run_at: float | None = None) -> bool:
    with work_session() as db:
        step = db.get(WorkStep, step_id)
        if not step:
            return False
        if worker_id and step.lease_owner and step.lease_owner != worker_id:
            return False
        return finish_step_in_db(db, step, status=status, output=output,
                                 error=error, next_run_at=next_run_at)


def park_wait(step_id: str, *, worker_id: str, next_run_at: float) -> bool:
    """run.wait 短轮询：只改时间，不写 waiting 事件，不增加 attempt。"""
    with work_session() as db:
        step = db.get(WorkStep, step_id)
        if not step or step.status != "running":
            return False
        if step.lease_owner and step.lease_owner != worker_id:
            return False
        inputs = dict(step.input_json or {})
        first = not bool(inputs.get("waiting_notified"))
        inputs["waiting_notified"] = True
        inputs["wait_polls"] = int(inputs.get("wait_polls") or 0) + 1
        if inputs["wait_polls"] > 1200:
            step.status = "failed"
            step.lease_owner = ""
            step.lease_until = None
            step.error = "run_wait_timeout"
            step.finished_at = _now()
            step.input_json = inputs
            work = db.get(WorkItem, step.work_id)
            work.status = "blocked"
            work.blocked_reason = "run_wait_timeout"
            work.updated_at = _now()
            append_event(db, work, "work.blocked", step_id=step.step_id,
                         payload={"reason": work.blocked_reason})
            return False
        step.input_json = inputs
        step.status = "waiting"
        step.next_run_at = next_run_at
        step.lease_owner = ""
        step.lease_until = None
        step.updated_at = _now()
        if first:
            work = db.get(WorkItem, step.work_id)
            append_event(db, work, "step.waiting", step_id=step.step_id,
                         payload={"kind": step.kind, "reason": "run_pending"})
        return True


def resume_parent_in_db(db: Session, parent_step_id: str, *, reason: str = "") -> bool:
    step = db.get(WorkStep, parent_step_id)
    if not step or step.status != "waiting":
        return False
    step.status = "ready"
    step.next_run_at = None
    step.updated_at = _now()
    work = db.get(WorkItem, step.work_id)
    append_event(db, work, "step.ready", step_id=step.step_id,
                 payload={"reason": reason or "child_done"})
    return True


def resume_parent(parent_step_id: str, *, reason: str = "") -> bool:
    with work_session() as db:
        return resume_parent_in_db(db, parent_step_id, reason=reason)


def add_artifact(db: Session, work: WorkItem, *, step_id: str, kind: str,
                 title: str = "", ref_type: str = "", ref_id: str = "",
                 uri: str = "", sha256: str = "", meta: Any = None) -> WorkArtifact:
    ref_id = str(ref_id or uri or "")[:128]
    existing = (
        db.query(WorkArtifact)
        .filter_by(work_id=work.work_id, step_id=step_id or "", kind=kind, ref_id=ref_id)
        .one_or_none()
    )
    if existing:
        return existing
    row = WorkArtifact(
        artifact_id=_id("wa"),
        work_id=work.work_id,
        step_id=step_id or "",
        kind=kind,
        title=(title or kind)[:200],
        ref_type=ref_type or "",
        ref_id=ref_id,
        uri=str(uri or "")[:1024],
        sha256=str(sha256 or "")[:64],
        meta_json=meta if isinstance(meta, dict) else {},
        created_at=_now(),
    )
    db.add(row)
    db.flush()
    append_event(db, work, "artifact.created", step_id=step_id,
                 payload={"artifact_id": row.artifact_id, "kind": kind, "ref_id": ref_id})
    return row


def add_link(db: Session, work: WorkItem, *, step_id: str, kind: str,
             ref_id: str, meta: Any = None) -> WorkLink:
    existing = (
        db.query(WorkLink)
        .filter_by(work_id=work.work_id, kind=kind, ref_id=ref_id)
        .one_or_none()
    )
    if existing:
        return existing
    row = WorkLink(
        link_id=_id("wl"),
        work_id=work.work_id,
        step_id=step_id or "",
        kind=kind,
        ref_id=str(ref_id)[:128],
        meta_json=meta if isinstance(meta, dict) else {},
        created_at=_now(),
    )
    db.add(row)
    db.flush()
    return row


def add_artifact_public(work_id: str, **kwargs: Any) -> dict[str, Any]:
    with work_session() as db:
        work = db.get(WorkItem, work_id)
        if not work:
            raise KeyError("work_not_found")
        return _artifact_public(add_artifact(db, work, **kwargs))


def create_approval(db: Session, work: WorkItem, *, kind: str, title: str,
                    prompt: str, step_id: str = "", options: Any = None,
                    requested_by: str = "system", expires_at: str = "") -> WorkApproval:
    existing = (
        db.query(WorkApproval)
        .filter_by(work_id=work.work_id, step_id=step_id or "", kind=kind, status="pending")
        .one_or_none()
    )
    if existing:
        return existing
    row = WorkApproval(
        approval_id=_id("ap"),
        work_id=work.work_id,
        step_id=step_id or "",
        kind=kind,
        status="pending",
        title=(title or kind)[:200],
        prompt=prompt or "",
        options_json=options or [
            {"value": "approve", "label": "批准"},
            {"value": "reject", "label": "驳回"},
        ],
        decision_json={},
        requested_by=requested_by or "system",
        created_at=_now(),
        expires_at=expires_at or "",
    )
    db.add(row)
    db.flush()
    append_event(db, work, "approval.requested", step_id=step_id,
                 payload={"approval_id": row.approval_id, "kind": kind, "title": row.title})
    return row


def decide_approval(*, work_id: str, approval_id: str, decision: str,
                    decided_by: str, comment: str = "", option: str = "") -> dict[str, Any]:
    with work_session() as db:
        approval = db.get(WorkApproval, approval_id)
        if not approval or approval.work_id != work_id:
            raise KeyError("approval_not_found")
        if approval.status != "pending":
            return _approval_public(approval)
        if decision not in ("approve", "reject"):
            raise ValueError("decision_invalid")
        work = db.get(WorkItem, work_id)
        if not work:
            raise KeyError("work_not_found")
        if work.status in TERMINAL_WORK:
            raise ValueError("work_terminal")
        approval.status = "approved" if decision == "approve" else "rejected"
        approval.decided_by = decided_by
        approval.decided_at = _now()
        approval.decision_json = {"decision": decision, "option": option, "comment": comment}
        append_event(db, work, f"approval.{approval.status}", step_id=approval.step_id,
                     actor=decided_by,
                     payload={"approval_id": approval_id, "comment": comment, "option": option})
        waiting = (
            db.query(WorkStep)
            .filter_by(work_id=work_id, kind="human.approval", status="waiting")
            .all()
        )
        matched = [s for s in waiting
                   if str((s.input_json or {}).get("approval_id") or "") == approval_id]
        if decision == "approve":
            # 关键：审批后 work 必须回到 running，否则 claim_step 永远不领审批步骤。
            if work.status == "waiting_human":
                work.status = "running"
                work.blocked_reason = ""
            work.updated_at = _now()
            for step in matched:
                step.status = "ready"
                step.next_run_at = None
                step.updated_at = _now()
                step.finished_at = None
                append_event(db, work, "step.ready", step_id=step.step_id,
                             actor=decided_by, payload={"approval_id": approval_id})
        else:
            work.status = "blocked"
            work.blocked_reason = "approval_rejected"
            work.updated_at = _now()
            for step in matched:
                step.status = "cancelled"
                step.finished_at = _now()
                step.lease_owner = ""
                step.lease_until = None
                step.updated_at = _now()
            append_event(db, work, "work.blocked", actor=decided_by,
                         payload={"reason": "approval_rejected", "approval_id": approval_id})
        return _approval_public(approval)


def pause_work(work_id: str, *, actor: str = "") -> dict[str, Any]:
    with work_session() as db:
        work = db.get(WorkItem, work_id)
        if not work:
            raise KeyError("work_not_found")
        if work.status in TERMINAL_WORK:
            return public_work(db, work)
        work.status = "paused"
        work.stop_requested = 1
        work.updated_at = _now()
        append_event(db, work, "work.paused", actor=actor,
                     payload={"running_step_may_finish": True})
        return public_work(db, work)


def resume_work(work_id: str, *, actor: str = "") -> dict[str, Any]:
    with work_session() as db:
        work = db.get(WorkItem, work_id)
        if not work:
            raise KeyError("work_not_found")
        if work.status in TERMINAL_WORK:
            raise ValueError("work_terminal")
        if work.blocked_reason == "write_result_unknown":
            raise ValueError("resolve_unknown_before_resume")
        if work.blocked_reason == "approval_rejected":
            rejected = (
                db.query(WorkApproval)
                .filter_by(work_id=work_id, kind="plan", status="rejected")
                .order_by(WorkApproval.created_at.desc())
                .first()
            )
            if not rejected:
                raise ValueError("plan_approval_missing")
            rejected.status = "pending"
            rejected.decided_by = ""
            rejected.decided_at = ""
            rejected.decision_json = {}
            step = db.get(WorkStep, rejected.step_id) if rejected.step_id else None
            if step and step.status in ("waiting", "done", "failed"):
                step.status = "waiting"
                step.lease_owner = ""
                step.lease_until = None
                step.finished_at = None
                step.updated_at = _now()
            work.status = "waiting_human"
            work.blocked_reason = ""
            work.stop_requested = 0
            work.updated_at = _now()
            append_event(db, work, "approval.requested", step_id=rejected.step_id,
                         actor=actor,
                         payload={"approval_id": rejected.approval_id, "kind": "plan",
                                  "title": rejected.title, "reopened": True})
            return public_work(db, work)
        if work.blocked_reason == "no_runnable_step":
            raise ValueError("patch_plan_before_resume")
        pending_approval = (
            db.query(WorkApproval)
            .filter_by(work_id=work_id, status="pending")
            .first()
        )
        work.status = "waiting_human" if pending_approval else "running"
        work.blocked_reason = ""
        work.stop_requested = 0
        work.updated_at = _now()
        # 暂停时可能有 waiting 的 turn；恢复时把它叫醒。
        waiting = (
            db.query(WorkStep)
            .filter_by(work_id=work_id, kind="agent.turn", status="waiting")
            .all()
        )
        for step in waiting:
            child = db.query(WorkStep).filter_by(parent_step_id=step.step_id).first()
            if child and child.status == "done":
                step.status = "ready"
                step.next_run_at = None
                step.updated_at = _now()
                append_event(db, work, "step.ready", step_id=step.step_id,
                             actor=actor, payload={"reason": "work_resumed"})
        append_event(db, work, "work.resumed", actor=actor)
        return public_work(db, work)


def cancel_work(work_id: str, *, actor: str = "") -> dict[str, Any]:
    with work_session() as db:
        work = db.get(WorkItem, work_id)
        if not work:
            raise KeyError("work_not_found")
        if work.status in TERMINAL_WORK:
            return public_work(db, work)
        work.status = "cancelled"
        work.stop_requested = 1
        work.finished_at = _now()
        work.updated_at = _now()
        steps = (
            db.query(WorkStep)
            .filter(WorkStep.work_id == work_id,
                    WorkStep.status.in_(("pending", "ready", "waiting")))
            .all()
        )
        for step in steps:
            step.status = "cancelled"
            step.finished_at = _now()
            step.lease_owner = ""
            step.lease_until = None
            step.updated_at = _now()
        approvals = (
            db.query(WorkApproval)
            .filter_by(work_id=work_id, status="pending")
            .all()
        )
        for row in approvals:
            row.status = "rejected"
            row.decided_by = actor or "system"
            row.decided_at = _now()
            row.decision_json = {"decision": "reject", "comment": "work_cancelled"}
        append_event(db, work, "work.cancelled", actor=actor,
                     payload={"cancelled_steps": len(steps), "cancelled_approvals": len(approvals)})
        return public_work(db, work)


def block_work(work_id: str, reason: str, *, actor: str = "") -> dict[str, Any]:
    with work_session() as db:
        work = db.get(WorkItem, work_id)
        if not work:
            raise KeyError("work_not_found")
        if work.status in TERMINAL_WORK:
            return public_work(db, work)
        work.status = "blocked"
        work.blocked_reason = str(reason or "")[:1000]
        work.updated_at = _now()
        append_event(db, work, "work.blocked", actor=actor,
                     payload={"reason": work.blocked_reason})
        return public_work(db, work)


def recover_stale_work() -> list[str]:
    """启动恢复：running 但 lease 过期的步骤；写结果未知不重放。"""
    now = time.time()
    recovered: list[str] = []
    with work_session() as db:
        stale = (
            db.query(WorkStep)
            .filter(WorkStep.status == "running",
                    or_(WorkStep.lease_until.is_(None), WorkStep.lease_until < now))
            .all()
        )
        for step in stale:
            work = db.get(WorkItem, step.work_id)
            if not work:
                continue
            if step.kind == "tool.call":
                child = (
                    db.query(WorkStep)
                    .filter(or_(
                        WorkStep.parent_step_id == step.step_id,
                        and_(WorkStep.ref_type == "tool_step", WorkStep.ref_id == step.step_id),
                    ))
                    .first()
                )
                if child:
                    # 子步骤已经落库：不重放工具，补父步骤完成，让子步骤继续。
                    step.status = "done"
                    step.lease_owner = ""
                    step.lease_until = None
                    step.updated_at = _now()
                    step.finished_at = _now()
                    append_event(db, work, "step.recovered", step_id=step.step_id,
                                 payload={"kind": step.kind, "action": "resume_child"})
                    if step.parent_step_id and child.status in TERMINAL_STEP:
                        resume_parent_in_db(db, step.parent_step_id, reason="tool_child_done")
                    recovered.append(step.step_id)
                    continue
                step.status = "unknown"
                step.lease_owner = ""
                step.lease_until = None
                step.updated_at = _now()
                step.finished_at = _now()
                work.status = "blocked"
                work.blocked_reason = "write_result_unknown"
                work.updated_at = _now()
                append_event(db, work, "step.unknown_on_recovery", step_id=step.step_id,
                             payload={"kind": step.kind})
                recovered.append(step.step_id)
            elif step.kind in RECOVERABLE:
                step.status = "ready"
                step.lease_owner = ""
                step.lease_until = None
                step.next_run_at = None
                step.updated_at = _now()
                append_event(db, work, "step.recovered", step_id=step.step_id,
                             payload={"kind": step.kind, "action": "retry"})
                recovered.append(step.step_id)
            elif step.kind == "human.approval":
                approval_id = str((step.input_json or {}).get("approval_id") or "")
                approval = db.get(WorkApproval, approval_id) if approval_id else None
                approved = bool(approval and approval.status == "approved")
                step.status = "ready" if approved else "waiting"
                step.lease_owner = ""
                step.lease_until = None
                step.next_run_at = None
                step.updated_at = _now()
                append_event(db, work, "step.recovered", step_id=step.step_id,
                             payload={"kind": step.kind, "action": "ready" if approved else "wait"})
                recovered.append(step.step_id)
        # 子步骤 done、父 turn 还 waiting：补一次唤醒。
        waiting_turns = (
            db.query(WorkStep)
            .filter_by(kind="agent.turn", status="waiting")
            .all()
        )
        for turn in waiting_turns:
            child = db.query(WorkStep).filter_by(parent_step_id=turn.step_id, status="done").first()
            if child:
                turn.status = "ready"
                turn.next_run_at = None
                turn.updated_at = _now()
                work = db.get(WorkItem, turn.work_id)
                append_event(db, work, "step.recovered", step_id=turn.step_id,
                             payload={"kind": turn.kind, "action": "resume_after_child"})
                recovered.append(turn.step_id)
        stuck = db.query(WorkItem).filter(WorkItem.status.in_(("planning", "running"))).all()
        for work in stuck:
            has = (
                db.query(WorkStep)
                .filter(WorkStep.work_id == work.work_id,
                        WorkStep.status.in_(("ready", "running", "waiting")))
                .first()
            )
            if not has:
                work.status = "blocked"
                work.blocked_reason = "no_runnable_step"
                work.updated_at = _now()
                append_event(db, work, "work.blocked", payload={"reason": work.blocked_reason})
    return recovered


def purge_expired_memories() -> int:
    """删除过期的 work_agent_memory；计划/验收读取时也会过滤，这里负责落库清理。"""
    now = _now()
    with work_session() as db:
        rows = (
            db.query(WorkAgentMemory)
            .filter(WorkAgentMemory.expires_at != "", WorkAgentMemory.expires_at <= now)
            .delete(synchronize_session=False)
        )
        return int(rows or 0)


def cleanup_terminal_work(retention_days: int = 90) -> int:
    """清理超过保留期的终态工作项及其子记录；不碰 running/waiting/blocked。"""
    days = max(1, min(3650, int(retention_days or 90)))
    cutoff = datetime.fromtimestamp(time.time() - days * 86400, timezone.utc).isoformat()
    with work_session() as db:
        rows = (
            db.query(WorkItem)
            .filter(WorkItem.status.in_(tuple(TERMINAL_WORK)),
                    WorkItem.updated_at < cutoff)
            .all()
        )
        ids = [row.work_id for row in rows]
        if not ids:
            return 0
        for model in (WorkArtifact, WorkLink, WorkApproval, WorkEvent, WorkStep,
                      WorkAgentSession):
            db.query(model).filter(model.work_id.in_(ids)).delete(synchronize_session=False)
        db.query(WorkAgentMemory).filter(
            WorkAgentMemory.scope == "work", WorkAgentMemory.scope_id.in_(ids),
        ).delete(synchronize_session=False)
        db.query(WorkItem).filter(WorkItem.work_id.in_(ids)).delete(synchronize_session=False)
        return len(ids)


def reserve_budget(step: dict[str, Any], work: dict[str, Any]) -> bool:
    with work_session() as db:
        row = db.get(WorkItem, work["work_id"])
        if not row:
            return False
        spent = dict(row.spent_json or {})
        for key in ("steps", "turns", "tool_calls", "tokens"):
            spent.setdefault(key, 0)
        # run.wait 是短轮询，不是新步骤；否则等待 1 分钟就会把 step 预算耗尽。
        if step.get("kind") != "run.wait":
            spent["steps"] = int(spent["steps"]) + 1
        if step.get("kind") == "agent.turn":
            spent["turns"] = int(spent["turns"]) + 1
        if step.get("kind") == "tool.call":
            spent["tool_calls"] = int(spent["tool_calls"]) + 1
        limits = row.budget_json or DEFAULT_BUDGET
        started = str(spent.get("started_at") or row.created_at or _now())
        try:
            spent["elapsed_sec"] = int((datetime.now(timezone.utc) - datetime.fromisoformat(started)).total_seconds())
        except ValueError:
            spent["elapsed_sec"] = 0
        exceeded = next(
            (key for key in ("steps", "turns", "tool_calls", "tokens")
             if int(limits.get("max_" + key) or 10**9) < int(spent[key])),
            "",
        )
        if int(limits.get("max_wall_clock_sec") or 10**9) < int(spent["elapsed_sec"]):
            exceeded = "wall_clock_sec"
        if exceeded:
            row.status = "blocked"
            row.blocked_reason = "budget_exhausted:" + exceeded
            row.updated_at = _now()
            step_row = db.get(WorkStep, step["step_id"])
            if step_row and step_row.status == "running":
                step_row.status = "cancelled"
                step_row.lease_owner = ""
                step_row.lease_until = None
                step_row.finished_at = _now()
            append_event(db, row, "work.blocked",
                         payload={"reason": row.blocked_reason, "spent": spent})
            return False
        row.spent_json = spent
        return True


def add_tokens(work_id: str, tokens: int) -> None:
    if int(tokens or 0) <= 0:
        return
    with work_session() as db:
        row = db.get(WorkItem, work_id)
        if not row:
            return
        spent = dict(row.spent_json or {})
        spent["tokens"] = int(spent.get("tokens") or 0) + int(tokens)
        row.spent_json = spent


def list_work(*, owner_user_id: str, project_id: str = "", topic_id: str = "",
              status: str = "", limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
    with work_session(write=False) as db:
        q = db.query(WorkItem).filter_by(owner_user_id=owner_user_id)
        if project_id:
            q = q.filter(WorkItem.project_id == project_id)
        if topic_id:
            q = q.filter(WorkItem.topic_id == topic_id)
        if status:
            q = q.filter(WorkItem.status == status)
        rows = q.order_by(WorkItem.updated_at.desc()).offset(max(0, offset)).limit(min(200, max(1, limit))).all()
        return [public_work(db, row) for row in rows]


def detail(work_id: str, *, event_limit: int = 200) -> dict[str, Any] | None:
    with work_session(write=False) as db:
        work = db.get(WorkItem, work_id)
        if not work:
            return None
        steps = db.query(WorkStep).filter_by(work_id=work_id).order_by(WorkStep.seq).all()
        approvals = (
            db.query(WorkApproval).filter_by(work_id=work_id)
            .order_by(WorkApproval.created_at.desc()).all()
        )
        artifacts = (
            db.query(WorkArtifact).filter_by(work_id=work_id)
            .order_by(WorkArtifact.created_at.desc()).all()
        )
        links = db.query(WorkLink).filter_by(work_id=work_id).order_by(WorkLink.created_at).all()
        events = []
        if event_limit:
            rows = (
                db.query(WorkEvent).filter_by(work_id=work_id)
                .order_by(WorkEvent.seq.desc()).limit(min(500, event_limit)).all()
            )
            for row in reversed(rows):
                events.append({
                    "event_id": row.event_id, "seq": row.seq, "type": row.type,
                    "step_id": row.step_id or "", "actor": row.actor,
                    "payload": row.payload or {}, "created_at": row.created_at,
                })
        out = public_work(db, work)
        out.update({
            "agent_sessions": list_agent_sessions(db, work_id),
            "memories": list_memories(db, work_id),
            "steps": [_step_public(s) for s in steps],
            "approvals": [_approval_public(a) for a in approvals],
            "artifacts": [_artifact_public(a) for a in artifacts],
            "links": [{"link_id": l.link_id, "kind": l.kind, "ref_id": l.ref_id,
                       "step_id": l.step_id, "meta": l.meta_json or {}} for l in links],
            "events": events,
        })
        return out


def set_plan(work_id: str, plan: dict[str, Any], acceptance: Any, *,
             actor: str = "") -> dict[str, Any]:
    with work_session() as db:
        work = db.get(WorkItem, work_id)
        if not work:
            raise KeyError("work_not_found")
        if work.status in TERMINAL_WORK:
            raise ValueError("work_terminal")
        work.plan_json = plan
        work.plan_revision = int(work.plan_revision or 0) + 1
        work.plan_index = 0
        for old in db.query(WorkStep).filter_by(
                work_id=work_id, kind="human.approval", status="waiting").all():
            old.status = "cancelled"
            old.finished_at = _now()
            old.updated_at = _now()
        work.acceptance_json = normalize_acceptance(acceptance)
        work.updated_at = _now()
        append_event(db, work, "plan.ready", actor=actor,
                     payload={"plan_revision": work.plan_revision,
                              "steps": len(plan.get("steps") or [])})
        return public_work(db, work)


def handoff(work_id: str, to_member: str, note: str = "", *, actor: str = "") -> dict[str, Any]:
    """Record a handoff without changing the plan or pretending execution changed hands.

    The chat mention created by the workspace endpoint is what asks the target member
    to pick the work up; this event keeps the decision visible on the work itself.
    """
    to_member = str(to_member or '').strip()[:120]
    if not to_member:
        raise ValueError('handoff_target_empty')
    note = str(note or '').strip()[:1000]
    with work_session() as db:
        work = db.get(WorkItem, work_id)
        if not work:
            raise KeyError("work_not_found")
        if work.status in TERMINAL_WORK:
            raise ValueError("work_terminal")
        rows = list(work.instructions_json or [])
        rows.append((f"交接给 {to_member}" + (f"：{note}" if note else ""))[:1000])
        work.instructions_json = rows[-30:]
        work.updated_at = _now()
        append_event(db, work, "work.handoff", actor=actor or "user",
                     payload={"to": to_member, "note": note})
        return public_work(db, work)


def add_instruction(work_id: str, text: str, *, actor: str = "") -> dict[str, Any]:
    text = str(text or "").strip()
    if not text:
        raise ValueError("instruction_empty")
    with work_session() as db:
        work = db.get(WorkItem, work_id)
        if not work:
            raise KeyError("work_not_found")
        if work.status in TERMINAL_WORK:
            raise ValueError("work_terminal")
        rows = list(work.instructions_json or [])
        rows.append(text[:1000])
        work.instructions_json = rows[-30:]
        work.updated_at = _now()
        append_event(db, work, "work.instruction", actor=actor, payload={"text": text[:1000]})
        return public_work(db, work)


def patch_plan(work_id: str, plan: dict[str, Any], acceptance: Any,
               *, expected_revision: int, actor: str = "") -> dict[str, Any]:
    with work_session() as db:
        work = db.get(WorkItem, work_id)
        if not work:
            raise KeyError("work_not_found")
        if work.status in TERMINAL_WORK:
            raise ValueError("work_terminal")
        if int(work.plan_revision or 0) != int(expected_revision):
            raise ValueError("plan_revision_conflict")
        pending = (
            db.query(WorkApproval)
            .filter_by(work_id=work_id, kind="plan", status="pending")
            .first()
        )
        if work.status == "waiting_human" and not pending:
            raise ValueError("approval_pending")
        if pending:
            # 计划审批前允许用户改计划；旧审批和旧审批步骤一起作废。
            pending.status = "cancelled"
            pending.decided_at = _now()
            pending.decision_json = {"decision": "cancelled", "comment": "plan_edited"}
            for old in db.query(WorkStep).filter_by(
                    work_id=work_id, kind="human.approval", status="waiting").all():
                old.status = "cancelled"
                old.finished_at = _now()
                old.updated_at = _now()
        steps = [s for s in (plan.get("steps") or []) if isinstance(s, dict)]
        if not (1 <= len(steps) <= 12):
            raise ValueError("plan_step_count_invalid")
        work.plan_json = plan
        work.plan_revision = int(work.plan_revision or 0) + 1
        work.plan_index = 0
        for old in db.query(WorkStep).filter_by(
                work_id=work_id, kind="human.approval", status="waiting").all():
            old.status = "cancelled"
            old.finished_at = _now()
            old.updated_at = _now()
        if acceptance:
            work.acceptance_json = normalize_acceptance(acceptance)
        work.status = "waiting_human"
        work.blocked_reason = ""
        work.updated_at = _now()
        approval = create_approval(
            db, work, kind="plan", title="批准修改后的计划",
            prompt=json.dumps({"title": plan.get("title"),
                               "steps": plan.get("steps"),
                               "acceptance": work.acceptance_json or [],
                               "risks": plan.get("risks") or []},
                              ensure_ascii=False),
            requested_by=actor or "user",
        )
        _add_step(db, work, kind="human.approval",
                  step_key=f"approval:{approval.approval_id}",
                  agent_id="conductor", role_id="conductor",
                  title="批准修改后的计划", input_json={"approval_id": approval.approval_id},
                  status="waiting")
        append_event(db, work, "plan.ready", actor=actor,
                     payload={"plan_revision": work.plan_revision,
                              "steps": len(steps), "approval_id": approval.approval_id})
        return public_work(db, work)


def retry_step(work_id: str, step_id: str, *, actor: str = "") -> dict[str, Any]:
    with work_session() as db:
        work = db.get(WorkItem, work_id)
        step = db.get(WorkStep, step_id)
        if not work or not step or step.work_id != work_id:
            raise KeyError("step_not_found")
        if work.status in TERMINAL_WORK:
            raise ValueError("work_terminal")
        if step.status not in ("failed", "unknown", "skipped"):
            raise ValueError("step_not_retryable")
        if step.status == "unknown":
            raise ValueError("resolve_unknown_before_retry")
        step.status = "ready"
        step.next_run_at = None
        step.error = ""
        step.updated_at = _now()
        work.status = "running"
        work.blocked_reason = ""
        work.updated_at = _now()
        append_event(db, work, "step.retry", step_id=step_id, actor=actor)
        return public_work(db, work)


def skip_step(work_id: str, step_id: str, *, actor: str = "") -> dict[str, Any]:
    with work_session() as db:
        work = db.get(WorkItem, work_id)
        step = db.get(WorkStep, step_id)
        if not work or not step or step.work_id != work_id:
            raise KeyError("step_not_found")
        if work.status in TERMINAL_WORK:
            raise ValueError("work_terminal")
        if step.status in ("running", "done", "cancelled"):
            raise ValueError("step_not_skippable")
        if step.kind == "human.approval":
            raise ValueError("approval_not_skippable")
        step.status = "skipped"
        step.finished_at = _now()
        step.lease_owner = ""
        step.lease_until = None
        step.updated_at = _now()
        work.status = "running"
        work.blocked_reason = ""
        work.updated_at = _now()
        if step.parent_step_id:
            resume_parent_in_db(db, step.parent_step_id, reason="step_skipped")
        append_event(db, work, "step.skipped", step_id=step_id, actor=actor)
        return public_work(db, work)


def resolve_unknown(work_id: str, step_id: str, *, decision: str,
                    actor: str = "", note: str = "") -> dict[str, Any]:
    if decision not in ("executed", "not_executed"):
        raise ValueError("decision_invalid")
    with work_session() as db:
        work = db.get(WorkItem, work_id)
        step = db.get(WorkStep, step_id)
        if not work or not step or step.work_id != work_id:
            raise KeyError("step_not_found")
        if step.status != "unknown":
            raise ValueError("step_not_unknown")
        step.output_json = {"resolution": decision, "note": note[:1000]}
        step.finished_at = _now()
        step.updated_at = _now()
        if decision == "executed":
            step.status = "done"
            work.status = "running"
            work.blocked_reason = ""
        else:
            step.status = "failed"
            work.status = "running"
            work.blocked_reason = ""
        work.updated_at = _now()
        if step.parent_step_id:
            resume_parent_in_db(db, step.parent_step_id, reason="unknown_resolved")
        append_event(db, work, "step.resolved", step_id=step_id, actor=actor,
                     payload={"decision": decision, "note": note[:1000]})
        return public_work(db, work)


def takeover_step(work_id: str, step_id: str, *, actor: str = "") -> dict[str, Any]:
    """L3 接管：暂停自动推进，把控制权交给用户；不伪造执行结果。"""
    with work_session() as db:
        work = db.get(WorkItem, work_id)
        step = db.get(WorkStep, step_id)
        if not work or not step or step.work_id != work_id:
            raise KeyError("step_not_found")
        if work.status in TERMINAL_WORK:
            raise ValueError("work_terminal")
        work.status = "paused"
        work.stop_requested = 1
        work.updated_at = _now()
        inputs = dict(step.input_json or {})
        inputs["takeover"] = {"actor": actor, "at": _now()}
        step.input_json = inputs
        step.updated_at = _now()
        append_event(db, work, "step.takeover", step_id=step_id, actor=actor)
        return public_work(db, work)
