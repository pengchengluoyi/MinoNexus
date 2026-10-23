"""工作层步骤 handler。

每个 handler 的“子步骤创建 + 父步骤状态 + artifact/link + resume”必须在同一
`work_session()` 事务里；工具/LLM 调用放在事务外，避免长事务锁库。
"""
from __future__ import annotations

import json
import re
from typing import Any

from mino_nexus.assistant.work import work_agent
from mino_nexus.assistant.work.work_agent import _bounded
from mino_nexus.assistant.work.work_store import (
    add_artifact, add_link, append_event, create_approval,
    finish_step_in_db, public_work, resume_parent_in_db,
    remember, upsert_agent_session, work_session, _add_step,
)
from mino_nexus.models.work_approval import WorkApproval
from mino_nexus.models.work_item import WorkItem
from mino_nexus.models.work_step import WorkStep

TERMINAL_RUN = {"done", "passed", "failed", "cancelled", "error", "timeout", "skipped", "aborted"}


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _bool(value: Any) -> bool:
    return bool(value) if value is not None else False


def _plan_steps(work: dict[str, Any]) -> list[dict[str, Any]]:
    plan = _as_dict(work.get("plan"))
    return [s for s in (plan.get("steps") or []) if isinstance(s, dict)]


def _plan_step(work: dict[str, Any], step: dict[str, Any]) -> dict[str, Any]:
    pid = str((step.get("input") or {}).get("plan_step_id") or "")
    for row in _plan_steps(work):
        if str(row.get("id") or "") == pid:
            return row
    idx = int(work.get("plan_index") or 0)
    rows = _plan_steps(work)
    return rows[idx] if 0 <= idx < len(rows) else {}


def _step_key_turn(work: dict[str, Any], plan_step_id: str, suffix: str = "") -> str:
    base = f"turn:{int(work.get('plan_revision') or 0)}:{plan_step_id}"
    return (base + ":" + suffix)[:160] if suffix else base[:160]


def _create_turn(db, work: WorkItem, plan_step: dict[str, Any], *,
                 parent_step_id: str = "", delegated: bool = False) -> WorkStep:
    pid = str(plan_step.get("id") or "p1")
    agent_id = str(plan_step.get("agent_id") or "conductor")
    key = _step_key_turn(public_work(db, work), pid, "delegate" if delegated else "")
    if delegated:
        key = (key + ":" + str(parent_step_id or "root"))[:160]
    return _add_step(
        db, work, kind="agent.turn", step_key=key,
        agent_id=agent_id, role_id=agent_id,
        plan_step_id=pid,
        title=str(plan_step.get("title") or "执行计划步骤"),
        intent=str(plan_step.get("intent") or ""),
        input_json={"plan_step_id": pid, "delegated": delegated},
        parent_step_id=parent_step_id,
    )


def _create_review(db, work: WorkItem) -> WorkStep:
    key = f"review:{int(work.plan_revision or 0)}"
    existing = db.query(WorkStep).filter_by(work_id=work.work_id, step_key=key).one_or_none()
    if existing:
        return existing
    return _add_step(
        db, work, kind="work.review", step_key=key,
        agent_id="conductor", role_id="conductor",
        title="验收工作结果", intent=work.goal or "",
        input_json={"acceptance": work.acceptance_json or []},
    )


def _advance_plan(db, work: WorkItem, step: WorkStep, *, reason: str = "step_done") -> None:
    index = int(work.plan_index or 0)
    steps = [s for s in ((work.plan_json or {}).get("steps") or []) if isinstance(s, dict)]
    if index + 1 < len(steps):
        work.plan_index = index + 1
        _create_turn(db, work, steps[index + 1])
    else:
        work.plan_index = len(steps)
        _create_review(db, work)
    work.status = "running" if work.status != "paused" else "paused"
    work.updated_at = _now()
    append_event(db, work, "plan.advanced", step_id=step.step_id,
                 payload={"plan_index": work.plan_index, "reason": reason})


def _now() -> str:
    from mino_nexus.assistant.work.work_store import _now as now

    return now()


def _approval_step(db, work: WorkItem, approval, *, parent_step_id: str,
                   title: str) -> WorkStep:
    step = _add_step(
        db, work, kind="human.approval", step_key=f"approval:{approval.approval_id}",
        agent_id="conductor", role_id="conductor", title=title,
        input_json={"approval_id": approval.approval_id, "kind": approval.kind},
        parent_step_id=parent_step_id, status="waiting",
    )
    approval.step_id = step.step_id
    return step


def _compact_tool_output(output: Any) -> dict[str, Any]:
    """给下一轮模型看的结果摘要；不要把 case.list 全量元数据反复塞进 prompt。"""
    out = _as_dict(output)
    data = _as_dict(out.get("data"))
    compact: dict[str, Any] = {
        "ok": out.get("ok"),
        "summary": str(out.get("summary") or "")[:800],
    }
    for key in ("run_id", "task_id", "status", "passed", "failed", "blocked",
                "total", "count", "runnable"):
        if key in data:
            compact[key] = data.get(key)
    rows = data.get("cases")
    if isinstance(rows, list) and rows:
        compact["case_ids"] = [
            str((row or {}).get("case_id") or (row or {}).get("id") or "")
            for row in rows[:30] if isinstance(row, dict)
        ]
        compact["case_titles"] = [
            str((row or {}).get("title") or (row or {}).get("name") or "")[:120]
            for row in rows[:12] if isinstance(row, dict)
        ]
    items = data.get("items")
    if isinstance(items, list) and items:
        compact["items"] = [
            {k: (row or {}).get(k) for k in ("session_id", "run_id", "case_id", "status", "summary")
             if isinstance(row, dict) and (row or {}).get(k) not in (None, "")}
            for row in items[:20]
        ]
    if not compact.get("summary") and data:
        compact["data"] = _bounded(data, 1200)
    return compact


def _recent_results(work: dict[str, Any], limit: int = 8) -> list[dict[str, Any]]:
    rows = []
    seen: set[str] = set()
    steps = list(work.get("steps") or [])
    for step in reversed(steps):
        if str(step.get("status") or "") not in ("done", "failed", "unknown"):
            continue
        kind = str(step.get("kind") or "")
        if kind not in ("tool.call", "job.call", "agent.turn"):
            continue
        payload = step.get("input") or {}
        key = kind + ":" + str(step.get("step_key") or step.get("step_id") or "")
        if kind == "tool.call":
            key = kind + ":" + str(payload.get("tool_id") or "") + ":" + json.dumps(
                payload.get("args") or {}, sort_keys=True, ensure_ascii=False, default=str)
        if key in seen:
            continue
        seen.add(key)
        output = step.get("output") or {}
        if kind == "tool.call":
            output = _compact_tool_output(output)
        rows.append({
            "step_id": step.get("step_id"),
            "kind": kind,
            "status": step.get("status"),
            "title": step.get("title"),
            "output": output,
            "error": str(step.get("error") or "")[:800],
        })
    arts = []
    for art in (work.get("artifacts") or []):
        kind = str(art.get("kind") or "")
        # tool_result 的全文已经在 tool.call.output 的摘要里，重复塞会淹没模型。
        if kind == "tool_result":
            continue
        meta = _as_dict(art.get("meta"))
        brief = {key: meta.get(key) for key in ("status", "verdict", "summary", "text", "reason")
                 if meta.get(key) not in (None, "")}
        if "review" in meta:
            brief["verdict"] = (meta.get("review") or {}).get("verdict") if isinstance(meta.get("review"), dict) else ""
        arts.append({
            "artifact_id": art.get("artifact_id"),
            "kind": kind,
            "title": art.get("title"),
            "ref_id": art.get("ref_id"),
            "meta": _bounded(brief, 1500),
        })
    rows.reverse()
    return (rows + arts)[-limit:]


def _same_tool_input(prior: WorkStep, tool_id: str, args: dict[str, Any]) -> bool:
    payload = dict(prior.input_json or {})
    if str(payload.get("tool_id") or "") != tool_id:
        return False
    old_args = payload.get("args") if isinstance(payload.get("args"), dict) else {}
    return json.dumps(old_args, sort_keys=True, ensure_ascii=False, default=str) == \
        json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)


def _case_run_key(args: dict[str, Any]) -> tuple[Any, ...]:
    """case.run 的幂等键：同一项目/应用/设备/用例集合只允许起一个 run。"""
    ids = args.get("case_ids") if isinstance(args.get("case_ids"), list) else []
    if not ids and args.get("case_id"):
        ids = [args.get("case_id")]
    return (
        str(args.get("project_id") or ""),
        str(args.get("app_id") or ""),
        str(args.get("env_surface") or ""),
        str(args.get("env_profile") or ""),
        str(args.get("sn") or ""),
        tuple(sorted(str(x) for x in ids)),
        str(args.get("module") or ""),
        tuple(sorted(str(x) for x in (args.get("modules") or []))),
        str(args.get("ordinal") or ""),
        bool(args.get("all_cases")),
    )


def _same_tool_call(prior: WorkStep, tool_id: str, args: dict[str, Any]) -> bool:
    if _same_tool_input(prior, tool_id, args):
        return True
    if tool_id != "case.run":
        return False
    payload = dict(prior.input_json or {})
    old_args = payload.get("args") if isinstance(payload.get("args"), dict) else {}
    return _case_run_key(old_args) == _case_run_key(args)


def _record_turn(db, row: WorkItem, role_id: str, decision: dict[str, Any]) -> None:
    action = str(decision.get("action") or "reply")
    text = str(decision.get("text") or decision.get("intent") or decision.get("prompt") or "")
    summary = f"{action}: {text}".strip()[:4000]
    upsert_agent_session(db, row, agent_id=role_id, role_id=role_id,
                         summary=summary, last_event_seq=int(row.last_event_seq or 0))
    if action in ("approval", "delegate", "complete"):
        remember(db, row, agent_id=role_id, kind="decision", content=summary,
                 source_ref="turn", confidence=60)


def handle_agent_plan(step: dict[str, Any], work: dict[str, Any]) -> dict[str, Any]:
    with work_session() as db:
        row = db.get(WorkItem, work["work_id"])
        if not row:
            return {"status": "failed", "error": "work_not_found"}
        current_row = db.get(WorkStep, step["step_id"])
        if row.status in ("cancelled",):
            finish_step_in_db(db, current_row, status="cancelled")
            return {"status": "cancelled"}
        # claim 返回的 public step 可能没有 input；以数据库里的 input_json 为准。
        replan = bool((step.get("input") or {}).get("replan")
                      or (current_row.input_json or {}).get("replan"))
        if int(row.plan_revision or 0) > 0 and not replan:
            current = db.get(WorkStep, step["step_id"])
            finish_step_in_db(db, current, status="done",
                              output={"plan_revision": row.plan_revision, "reused": True})
            return {"status": "done", "reused": True}
    plan = work_agent.plan_work(work)
    with work_session() as db:
        row = db.get(WorkItem, work["work_id"])
        current = db.get(WorkStep, step["step_id"])
        if not row or not current:
            return {"status": "failed", "error": "work_step_missing"}
        if row.status in ("cancelled",):
            finish_step_in_db(db, current, status="cancelled")
            return {"status": "cancelled"}
        if int(row.plan_revision or 0) > 0 and not replan:
            finish_step_in_db(db, current, status="done",
                              output={"plan_revision": row.plan_revision, "reused": True})
            return {"status": "done", "reused": True}
        was_paused = row.status == "paused" or int(row.stop_requested or 0) == 1
        row.plan_json = plan
        row.plan_revision = int(row.plan_revision or 0) + 1
        row.plan_index = 0
        row.blocked_reason = ""
        if replan:
            row.review_json = {}
            append_event(db, row, "work.replanned", step_id=current.step_id,
                         payload={"plan_revision": row.plan_revision,
                                  "reason": str((current.input_json or {}).get("reason") or "")[:500]})
        # 用户明确给过的验收标准优先；plan_work 已经做了这一步。
        row.acceptance_json = plan.get("acceptance") or row.acceptance_json or []
        approval = create_approval(
            db, row, kind="plan", title="批准执行计划",
            prompt=json.dumps({"title": plan.get("title"), "steps": plan.get("steps"),
                               "acceptance": plan.get("acceptance"), "risks": plan.get("risks")},
                              ensure_ascii=False),
            requested_by="conductor",
        )
        _approval_step(db, row, approval, parent_step_id="", title="批准计划")
        row.status = "paused" if was_paused else "waiting_human"
        row.updated_at = _now()
        append_event(db, row, "plan.ready", step_id=current.step_id,
                     payload={"plan_revision": row.plan_revision, "approval_id": approval.approval_id})
        summary = f"计划：{plan.get('title') or row.goal}；{len(plan.get('steps') or [])} 步"
        upsert_agent_session(db, row, agent_id="conductor", role_id="conductor",
                             job_id="work-plan", summary=summary,
                             last_event_seq=int(row.last_event_seq or 0))
        remember(db, row, agent_id="conductor", kind="decision", content=summary,
                 source_ref=approval.approval_id, confidence=70)
        finish_step_in_db(db, current, status="done",
                          output={"plan_revision": row.plan_revision,
                                  "steps": len(plan.get("steps") or [])})
        return {"status": "done", "plan_revision": row.plan_revision}


def handle_human_approval(step: dict[str, Any], work: dict[str, Any]) -> dict[str, Any]:
    approval_id = str((step.get("input") or {}).get("approval_id") or "")
    if not approval_id:
        return {"status": "failed", "error": "approval_id_missing"}
    with work_session() as db:
        approval = db.get(WorkApproval, approval_id)
        row = db.get(WorkItem, work["work_id"])
        current = db.get(WorkStep, step["step_id"])
        if not approval or not row or not current:
            return {"status": "failed", "error": "approval_not_found"}
        if row.status == "cancelled" or current.status == "cancelled":
            finish_step_in_db(db, current, status="cancelled")
            return {"status": "cancelled"}
        if approval.status == "pending":
            current.status = "waiting"
            current.lease_owner = ""
            current.lease_until = None
            row.status = "waiting_human"
            row.updated_at = _now()
            return {"status": "waiting"}
        if approval.status == "rejected":
            row.status = "blocked"
            row.blocked_reason = "approval_rejected"
            row.updated_at = _now()
            finish_step_in_db(db, current, status="done", output={"decision": "reject"})
            return {"status": "done", "decision": "reject"}
        # approved
        if row.status != "paused":
            row.status = "running"
        row.blocked_reason = ""
        row.updated_at = _now()
        if approval.kind == "plan":
            steps = _plan_steps(public_work(db, row))
            if steps and not db.query(WorkStep).filter_by(
                    work_id=row.work_id, step_key=_step_key_turn(public_work(db, row),
                                                                 str(steps[0].get("id") or "p1"))).first():
                _create_turn(db, row, steps[0])
                remember(db, row, agent_id="conductor", kind="decision",
                         content="用户批准计划 revision " + str(row.plan_revision),
                         source_ref=approval.approval_id, confidence=80)
        elif approval.kind == "tool" and (current.input_json or {}).get("execute"):
            # 批准的是“这次具体工具参数”，不是让模型重新决策一次。
            payload = current.input_json or {}
            child = _add_step(
                db, row, kind="tool.call",
                step_key=f"approved:{approval.approval_id}"[:160],
                agent_id=str(payload.get("agent_id") or current.agent_id or "conductor"),
                role_id=str(current.role_id or ""),
                title="执行已批准的工具 " + str(payload.get("tool_id") or ""),
                intent=str(current.intent or ""),
                input_json={"tool_id": payload.get("tool_id") or "",
                            "args": payload.get("args") or {},
                            "approved": True},
                parent_step_id=current.parent_step_id or "",
            )
            _ = child
        else:
            parent_id = current.parent_step_id or ""
            if parent_id:
                resume_parent_in_db(db, parent_id, reason="approval_approved")
        finish_step_in_db(db, current, status="done", output={"decision": "approve"})
        return {"status": "done", "decision": "approve"}


def handle_agent_turn(step: dict[str, Any], work: dict[str, Any], *,
                      use_plan: bool = True) -> dict[str, Any]:
    plan_step = _plan_step(work, step) if use_plan else {}
    role_id = str(step.get("agent_id") or plan_step.get("agent_id") or "conductor")
    step_for_turn = dict(step)
    step_for_turn["agent_id"] = role_id
    if plan_step:
        step_for_turn["intent"] = plan_step.get("intent") or step.get("intent") or ""
        step_for_turn["title"] = plan_step.get("title") or step.get("title") or ""
    approvals = list(work.get("approvals") or [])
    decision = work_agent.turn_work(work, step_for_turn,
                                    _recent_results(work), approvals)
    action = decision["action"]
    with work_session() as db:
        row = db.get(WorkItem, work["work_id"])
        current = db.get(WorkStep, step["step_id"])
        if not row or not current:
            return {"status": "failed", "error": "work_step_missing"}
        if row.status == "cancelled" or current.status == "cancelled":
            finish_step_in_db(db, current, status="cancelled")
            return {"status": "cancelled"}
        _record_turn(db, row, role_id, decision)
        delegated = bool((current.input_json or {}).get("delegated")) or current.kind == "job.call"
        if action == "tool":
            # 同一工作项内，工具结果可跨回合复用；尤其 case.run 只能起一个 run。
            prior = (
                db.query(WorkStep)
                .filter_by(work_id=row.work_id, kind="tool.call", status="done")
                .order_by(WorkStep.seq.desc()).all()
            )
            from mino_nexus.assistant.assistant_tools import tool_by_function_name
            spec = tool_by_function_name(str(decision.get("tool_id") or ""))
            tool_id = str(decision.get("tool_id") or "")
            same = None
            if spec:
                risk = str(spec.get("risk") or "read")
                # 读工具可复用；case.run 是“启动一次运行”的幂等边界，已成功启动过也必须复用，
                # 否则模型会在同一工作项里重复提交，制造多个 run。
                if risk != "write" or tool_id == "case.run":
                    same = next((p for p in prior if _same_tool_call(
                        p, tool_id, decision.get("args") or {})), None)
                    if same is not None and tool_id == "case.run":
                        data = (same.output_json or {}).get("data") if isinstance(same.output_json, dict) else {}
                        if not (isinstance(data, dict) and data.get("run_id")):
                            same = None
            if same is not None:
                child = _add_step(
                    db, row, kind="tool.call",
                    step_key=(f"reuse:{current.step_id}:{same.step_id}")[:160],
                    agent_id=role_id, role_id=role_id,
                    title=f"复用 {tool_id} 已有结果（不重复启动）",
                    intent=str(step_for_turn.get("intent") or "")[:1200],
                    input_json={"tool_id": decision["tool_id"],
                                "args": decision.get("args") or {},
                                "reused": True},
                    parent_step_id=current.step_id,
                )
                finish_step_in_db(db, child, status="done", output={
                    "ok": True, "reused": True,
                    "summary": "同一工具与参数已成功执行过，复用上次结果，未重复执行。",
                    "data": same.output_json.get("data") if isinstance(same.output_json, dict) else {},
                })
                finish_step_in_db(db, current, status="waiting", output={
                    "action": "tool", "tool_id": decision["tool_id"],
                    "child_step_id": child.step_id, "reused": True,
                })
                resume_parent_in_db(db, current.step_id, reason="tool_reused")
                return {"status": "waiting"}
            child = _add_step(
                db, row, kind="tool.call",
                step_key=(f"tool:{current.step_id}:{int(current.attempt or 1)}")[:160],
                agent_id=role_id, role_id=role_id,
                title=f"调用 {decision['tool_id']}",
                intent=str(step_for_turn.get("intent") or "")[:1200],
                input_json={"tool_id": decision["tool_id"], "args": decision.get("args") or {}},
                parent_step_id=current.step_id,
            )
            finish_step_in_db(db, current, status="waiting",
                              output={"action": "tool", "tool_id": decision["tool_id"],
                                      "child_step_id": child.step_id})
            return {"status": "waiting"}
        if action == "delegate":
            target = decision["agent_id"]
            job_id = ""
            from mino_nexus.ai.roles_catalog import get_role
            role = get_role(target) or {}
            jobs = list(role.get("chat_jobs") or [])
            job_id = jobs[0] if jobs else ""
            child = _add_step(
                db, row, kind="job.call",
                step_key=(f"job:{current.step_id}:{target}:{int(current.attempt or 1)}")[:160],
                agent_id=target, role_id=target, job_id=job_id,
                title=f"委派 {target}",
                intent=str(decision.get("intent") or "")[:1200],
                input_json={"role_id": target, "job_id": job_id,
                            "intent": decision.get("intent") or ""},
                parent_step_id=current.step_id,
            )
            finish_step_in_db(db, current, status="waiting",
                              output={"action": "delegate", "agent_id": target,
                                      "child_step_id": child.step_id})
            return {"status": "waiting"}
        if action == "approval":
            approval = create_approval(
                db, row, kind="turn",
                title=str(decision.get("title") or "需要确认"),
                prompt=str(decision.get("prompt") or ""),
                requested_by=role_id,
            )
            _approval_step(db, row, approval, parent_step_id=current.step_id,
                           title=str(decision.get("title") or "需要确认"))
            row.status = "paused" if row.status == "paused" or int(row.stop_requested or 0) else "waiting_human"
            row.updated_at = _now()
            finish_step_in_db(db, current, status="waiting",
                              output={"action": "approval", "approval_id": approval.approval_id})
            return {"status": "waiting"}
        if action == "complete":
            finish_step_in_db(db, current, status="done",
                              output={"action": "complete", "text": decision.get("text") or ""})
            if delegated and current.parent_step_id:
                resume_parent_in_db(db, current.parent_step_id, reason="delegate_completed")
                row.status = "running" if row.status != "paused" else "paused"
                row.updated_at = _now()
            elif use_plan:
                # 计划还有下一步就继续，不能因为一个回合 complete 就跳过计划。
                _advance_plan(db, row, current, reason="turn_complete")
            else:
                _create_review(db, row)
                row.status = "running" if row.status != "paused" else "paused"
                row.updated_at = _now()
            return {"status": "done"}
        # reply
        add_artifact(db, row, step_id=current.step_id, kind="note",
                     title="角色回复", ref_id=current.step_id,
                     meta={"text": decision.get("text") or "", "agent_id": role_id})
        if delegated:
            if current.parent_step_id:
                resume_parent_in_db(db, current.parent_step_id, reason="delegate_replied")
            finish_step_in_db(db, current, status="done",
                              output={"action": "reply", "text": decision.get("text") or ""})
            return {"status": "done"}
        _advance_plan(db, row, current, reason="turn_reply")
        finish_step_in_db(db, current, status="done",
                          output={"action": "reply", "text": decision.get("text") or ""})
        return {"status": "done"}


def _tool_result_data(outcome) -> dict[str, Any]:
    data = outcome.data if isinstance(outcome.data, dict) else {}
    return data


def handle_tool_call(step: dict[str, Any], work: dict[str, Any]) -> dict[str, Any]:
    from mino_nexus.assistant.assistant_tools import run_tool, tool_by_function_name
    from mino_nexus.platform.llm import dispatch_log

    payload = step.get("input") or {}
    tool_id = str(payload.get("tool_id") or "")
    args = _as_dict(payload.get("args"))
    # 模型常把用例 id 填进 search（search 只按名称关键字筛）。对 api-1-210 这类 id
    # 做一次确定性的参数纠正，避免“范围没有用例”的空跑。
    if tool_id == "case.run" and not args.get("case_ids"):
        search = str(args.get("search") or "").strip()
        if re.fullmatch(r"[A-Za-z]+-\d+-\d+", search):
            args = {**args, "case_ids": [search]}
            args.pop("search", None)
    spec = tool_by_function_name(tool_id)
    allowed = {t["id"] for t in work_agent.available_tools(str(step.get("agent_id") or "conductor"))}
    if not spec or tool_id not in allowed:
        with work_session() as db:
            row = db.get(WorkItem, work["work_id"])
            current = db.get(WorkStep, step["step_id"])
            if row and current:
                row.status = "blocked"
                row.blocked_reason = "tool_not_allowed:" + tool_id
                finish_step_in_db(db, current, status="failed", error=row.blocked_reason)
        return {"status": "failed", "error": "tool_not_allowed"}
    policy = work.get("policy") or {}
    if (spec.get("risk") == "write" and str(policy.get("write_approval") or "") == "always"
            and not bool(payload.get("approved"))):
        with work_session() as db:
            row = db.get(WorkItem, work["work_id"])
            current = db.get(WorkStep, step["step_id"])
            if not row or not current:
                return {"status": "failed", "error": "work_step_missing"}
            if row.status == "cancelled" or current.status == "cancelled":
                finish_step_in_db(db, current, status="cancelled")
                return {"status": "cancelled"}
            approval = create_approval(
                db, row, kind="tool", step_id=current.step_id,
                title="批准写操作 " + tool_id,
                prompt="即将执行写工具 " + tool_id + "，参数：" + json.dumps(args, ensure_ascii=False)[:3000],
                requested_by=str(step.get("agent_id") or "conductor"),
            )
            approval_step = _approval_step(db, row, approval,
                                            parent_step_id=current.parent_step_id,
                                            title="批准写操作 " + tool_id)
            approval_step.input_json = {
                **dict(approval_step.input_json or {}),
                "tool_id": tool_id, "args": args,
                "tool_step_id": current.step_id,
                "agent_id": str(step.get("agent_id") or "conductor"),
                "execute": True,
            }
            row.status = "paused" if row.status == "paused" or int(row.stop_requested or 0) else "waiting_human"
            row.updated_at = _now()
            finish_step_in_db(db, current, status="done",
                              output={"ok": False, "preapproved": True,
                                      "approval_id": approval.approval_id, "args": args})
        return {"status": "done", "ask": True}
    if spec.get("risk") == "write" and "client_request_id" in ((spec.get("params_schema") or {}).get("properties") or {}):
        args.setdefault("client_request_id", step["step_key"])
    token = dispatch_log.bind(assistant_mutation_scope=step["step_id"])
    try:
        outcome = run_tool(spec, args, user_id=work["owner_user_id"], surface="mcp")
    finally:
        dispatch_log.reset(token)
    data = _tool_result_data(outcome)
    run_id = str(data.get("run_id") or data.get("task_id") or "")
    with work_session() as db:
        row = db.get(WorkItem, work["work_id"])
        current = db.get(WorkStep, step["step_id"])
        if not row or not current:
            return {"status": "failed", "error": "work_step_missing"}
        if row.status == "cancelled" or current.status == "cancelled":
            finish_step_in_db(db, current, status="cancelled")
            return {"status": "cancelled"}
        if outcome.ask:
            approval = create_approval(
                db, row, kind="tool",
                title="确认执行 " + tool_id,
                prompt=str(outcome.ask)[:4000],
                step_id=current.step_id,
                requested_by=str(step.get("agent_id") or "conductor"),
            )
            approval_step = _approval_step(db, row, approval,
                                            parent_step_id=current.parent_step_id,
                                            title="确认执行 " + tool_id)
            approval_step.input_json = {
                **dict(approval_step.input_json or {}),
                "tool_id": tool_id, "args": args,
                "tool_step_id": current.step_id,
                "agent_id": str(step.get("agent_id") or "conductor"),
                "execute": not bool(outcome.field or outcome.options),
            }
            row.status = "paused" if row.status == "paused" or int(row.stop_requested or 0) else "waiting_human"
            row.updated_at = _now()
            finish_step_in_db(db, current, status="done",
                              output={"ok": False, "ask": outcome.ask,
                                      "options": outcome.options,
                                      "approval_id": approval.approval_id,
                                      "args": args})
            return {"status": "done", "ask": True}
        if not outcome.ok:
            finish_step_in_db(db, current, status="failed", error=outcome.summary or "tool_failed",
                              output={"ok": False, "status": outcome.status,
                                      "summary": outcome.summary, "args": args})
            row.status = "blocked"
            row.blocked_reason = "tool_failed:" + tool_id
            row.updated_at = _now()
            append_event(db, row, "work.blocked", step_id=current.step_id,
                         payload={"reason": row.blocked_reason})
            return {"status": "failed", "error": outcome.summary}
        add_artifact(db, row, step_id=current.step_id, kind="tool_result",
                     title=outcome.summary or tool_id, ref_type="tool", ref_id=current.step_id,
                     meta={"tool_id": tool_id, "summary": outcome.summary,
                           "data": data, "args": args})
        if run_id:
            add_link(db, row, step_id=current.step_id, kind="run", ref_id=run_id,
                     meta={"tool_id": tool_id})
            add_artifact(db, row, step_id=current.step_id, kind="run",
                         title="执行任务 " + run_id, ref_type="run", ref_id=run_id,
                         meta={"status": str(data.get("status") or "")})
            _add_step(
                db, row, kind="run.wait",
                step_key=f"wait:{current.step_id}"[:160],
                agent_id=str(step.get("agent_id") or ""), role_id=str(step.get("role_id") or ""),
                title="等待执行结果", intent="等待 Scout 完成本次 run",
                input_json={"run_id": run_id, "tool_step_id": current.step_id},
                parent_step_id=str(current.parent_step_id or ""),
                ref_type="tool_step", ref_id=current.step_id,
            )
            finish_step_in_db(db, current, status="done",
                              output={"ok": True, "summary": outcome.summary,
                                      "run_id": run_id, "data": data})
            return {"status": "done", "run_id": run_id}
        add_artifact(db, row, step_id=current.step_id, kind="tool_result",
                     title=(outcome.summary or tool_id) + " 结果",
                     ref_type="tool", ref_id=current.step_id,
                     meta={"data": data, "summary": outcome.summary})
        if current.parent_step_id:
            resume_parent_in_db(db, current.parent_step_id, reason="tool_done")
        finish_step_in_db(db, current, status="done",
                          output={"ok": True, "summary": outcome.summary, "data": data})
        return {"status": "done"}


def _collect_sessions(value: Any, out: set[str]) -> None:
    if isinstance(value, dict):
        sid = str(value.get("session_id") or value.get("scout_session_id") or "")
        if sid:
            out.add(sid)
        for key in ("sessions", "cases", "execution_units", "results"):
            if key in value:
                _collect_sessions(value[key], out)
    elif isinstance(value, list):
        for item in value:
            _collect_sessions(item, out)


def handle_run_wait(step: dict[str, Any], work: dict[str, Any]) -> dict[str, Any]:
    import time

    from mino_nexus.assistant.work.work_store import park_wait
    from mino_nexus.casework import run_store

    run_id = str((step.get("input") or {}).get("run_id") or "")
    doc = run_store.get(run_id) if run_id else None
    if not doc or run_store.task_is_live(doc) or str(doc.get("status") or "") not in TERMINAL_RUN:
        owner = str(step.get("lease_owner") or "")
        if not park_wait(step["step_id"], worker_id=owner, next_run_at=time.time() + 3.0):
            return {"status": "failed", "error": "run_wait_timeout"}
        return {"status": "waiting"}
    with work_session() as db:
        row = db.get(WorkItem, work["work_id"])
        current = db.get(WorkStep, step["step_id"])
        if not row or not current:
            return {"status": "failed", "error": "work_step_missing"}
        if row.status == "cancelled" or current.status == "cancelled":
            finish_step_in_db(db, current, status="cancelled")
            return {"status": "cancelled"}
        status = str(doc.get("status") or "")
        add_artifact(db, row, step_id=current.step_id, kind="run",
                     title="执行结果 " + run_id, ref_type="run", ref_id=run_id,
                     meta={"status": status, "passed": doc.get("passed"), "failed": doc.get("failed"),
                           "total": doc.get("total"), "error": doc.get("error") or ""})
        add_link(db, row, step_id=current.step_id, kind="run", ref_id=run_id,
                 meta={"status": status})
        sessions: set[str] = set()
        _collect_sessions(doc, sessions)
        for sid in sorted(sessions):
            add_artifact(db, row, step_id=current.step_id, kind="session",
                         title="Scout 会话 " + sid, ref_type="session", ref_id=sid)
            add_link(db, row, step_id=current.step_id, kind="session", ref_id=sid)
        finish_step_in_db(db, current, status="done",
                          output={"run_id": run_id, "status": status,
                                  "passed": doc.get("passed"), "failed": doc.get("failed")})
        parent = db.get(WorkStep, current.parent_step_id) if current.parent_step_id else None
        if parent and parent.status == "waiting" and parent.kind == "agent.turn":
            # run 已有终态：收口父回合；计划还有下一步就继续，最后一步才进验收。
            finish_step_in_db(db, parent, status="done",
                              output={"run_id": run_id, "status": status,
                                      "completed_by": "run_wait",
                                      "passed": doc.get("passed"), "failed": doc.get("failed")})
            _advance_plan(db, row, parent, reason="run_finished")
            row.status = "running" if row.status != "paused" else "paused"
            row.updated_at = _now()
        elif parent and parent.status == "waiting" and parent.kind == "job.call":
            finish_step_in_db(db, parent, status="done",
                              output={"run_id": run_id, "status": status,
                                      "completed_by": "run_wait"})
            if parent.parent_step_id:
                resume_parent_in_db(db, parent.parent_step_id, reason="job_run_finished")
            else:
                _create_review(db, row)
            row.status = "running" if row.status != "paused" else "paused"
            row.updated_at = _now()
        elif current.parent_step_id:
            resume_parent_in_db(db, current.parent_step_id, reason="run_finished")
        return {"status": "done"}


def handle_job_call(step: dict[str, Any], work: dict[str, Any]) -> dict[str, Any]:
    from mino_nexus.ai.roles_catalog import chat_with_role, get_role

    role_id = str((step.get("input") or {}).get("role_id") or step.get("agent_id") or "")
    job_id = str((step.get("input") or {}).get("job_id") or step.get("job_id") or "")
    intent = str((step.get("input") or {}).get("intent") or step.get("intent") or work.get("goal") or "")
    role = get_role(role_id) or {}
    # 有工具的 agent 不能走 text_chat job：那条路径没有工具循环，只会说“没有权限”。
    # 把 job.call 当成一次工具型回合执行；它仍保留原来的 parent/child 结构。
    if role.get("tools"):
        return handle_agent_turn(step, work, use_plan=False)
    jobs = list(role.get("chat_jobs") or [])
    if not job_id:
        job_id = jobs[0] if jobs else ""
    if not role or not job_id:
        with work_session() as db:
            row = db.get(WorkItem, work["work_id"])
            current = db.get(WorkStep, step["step_id"])
            if row and current:
                row.status = "blocked"
                row.blocked_reason = "role_job_missing:" + role_id
                finish_step_in_db(db, current, status="failed", error=row.blocked_reason)
        return {"status": "failed", "error": "role_job_missing"}
    result = chat_with_role(role_id=role_id, messages=[{"role": "user", "content": intent}],
                            job_id=job_id, timeout_sec=90)
    reply = str(result.get("reply") or "")
    with work_session() as db:
        row = db.get(WorkItem, work["work_id"])
        current = db.get(WorkStep, step["step_id"])
        if not row or not current:
            return {"status": "failed", "error": "work_step_missing"}
        if row.status == "cancelled" or current.status == "cancelled":
            finish_step_in_db(db, current, status="cancelled")
            return {"status": "cancelled"}
        add_artifact(db, row, step_id=current.step_id, kind="note",
                     title=role.get("label") or role_id, ref_id=current.step_id,
                     meta={"agent_id": role_id, "job_id": job_id, "text": reply,
                           "usage": result.get("usage") or {}})
        upsert_agent_session(db, row, agent_id=role_id, role_id=role_id, job_id=job_id,
                             summary=str(reply or "")[:4000],
                             last_event_seq=int(row.last_event_seq or 0))
        if current.parent_step_id:
            resume_parent_in_db(db, current.parent_step_id, reason="job_done")
        finish_step_in_db(db, current, status="done", output={"reply": reply, "role_id": role_id})
        return {"status": "done"}


def _review_results(work: dict[str, Any]) -> list[dict[str, Any]]:
    """给验收器的工具/回合事实摘要；不要让它从被截断的全量产物里猜。"""
    out: list[dict[str, Any]] = []
    for step in (work.get("steps") or []):
        if str(step.get("status") or "") not in ("done", "failed", "unknown"):
            continue
        kind = str(step.get("kind") or "")
        if kind == "tool.call":
            payload = step.get("input") or {}
            item = {
                "kind": "tool.call",
                "title": str(step.get("title") or "")[:200],
                "tool_id": str(payload.get("tool_id") or ""),
                "status": step.get("status"),
            }
            item.update(_compact_tool_output(step.get("output") or {}))
            out.append(item)
        elif kind == "agent.turn":
            output = step.get("output") or {}
            out.append({
                "kind": "agent.turn",
                "title": str(step.get("title") or "")[:200],
                "status": step.get("status"),
                "text": str(output.get("text") or output.get("action") or "")[:2000],
            })
        elif kind == "run.wait":
            out.append({"kind": "run.wait", "status": step.get("status"),
                        "output": _bounded(step.get("output") or {}, 1000)})
    return out[-20:]


def _review_artifacts(work: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for art in (work.get("artifacts") or []):
        kind = str(art.get("kind") or "")
        meta = art.get("meta") or {}
        keys = ("review", "status", "summary", "reason", "text", "tool_id")
        brief = {k: meta.get(k) for k in keys if meta.get(k) not in (None, "")}
        out.append({"artifact_id": art.get("artifact_id"), "kind": kind,
                    "title": art.get("title"), "ref_id": art.get("ref_id"),
                    "meta": _bounded(brief, 2000)})
    return out


def _fill_evidence(review: dict[str, Any], work: dict[str, Any]) -> None:
    """模型常给出 met=true 却漏 evidence；按 note 里的工具名补真实 artifact id。"""
    artifacts = list(work.get("artifacts") or [])
    by_tool: dict[str, dict[str, Any]] = {}
    for art in artifacts:
        tool_id = str((art.get("meta") or {}).get("tool_id") or "")
        if tool_id and tool_id not in by_tool:
            by_tool[tool_id] = art
    for crit in review.get("criteria") or []:
        if not crit.get("met") or crit.get("evidence"):
            continue
        text = str(crit.get("note") or "") + " " + str(crit.get("id") or "")
        for tool_id, art in by_tool.items():
            if tool_id in text:
                crit["evidence"] = [{
                    "type": "artifact",
                    "ref": art.get("artifact_id") or "",
                    "note": "tool result: " + tool_id,
                }]
                break


def handle_work_review(step: dict[str, Any], work: dict[str, Any]) -> dict[str, Any]:
    artifacts = _review_artifacts(work)
    runs = []
    for link in (work.get("links") or []):
        if str(link.get("kind") or "") == "run":
            runs.append({"run_id": link.get("ref_id"), "meta": link.get("meta") or {}})
    review = work_agent.review_work(work, artifacts, runs, _review_results(work))
    _fill_evidence(review, work)
    ok, reason = work_agent.validate_review(work, review, artifacts, list(work.get("links") or []))
    with work_session() as db:
        row = db.get(WorkItem, work["work_id"])
        current = db.get(WorkStep, step["step_id"])
        if not row or not current:
            return {"status": "failed", "error": "work_step_missing"}
        if row.status == "cancelled" or current.status == "cancelled":
            finish_step_in_db(db, current, status="cancelled")
            return {"status": "cancelled"}
        row.review_json = review
        add_artifact(db, row, step_id=current.step_id, kind="review",
                     title="验收结果", ref_id=current.step_id,
                     meta={"review": review, "reason": reason})
        if ok:
            row.status = "done"
            row.finished_at = _now()
            row.updated_at = _now()
            append_event(db, row, "work.completed", step_id=current.step_id,
                         payload={"review": review})
            upsert_agent_session(db, row, agent_id="conductor", role_id="conductor",
                                 job_id="work-review", summary="验收通过：" + str(review.get("summary") or ""),
                                 last_event_seq=int(row.last_event_seq or 0))
            remember(db, row, agent_id="conductor", kind="lesson",
                     content="验收通过：" + str(review.get("summary") or ""),
                     source_ref=current.step_id, confidence=70, ttl_days=90)
            finish_step_in_db(db, current, status="done", output={"verdict": "done"})
            return {"status": "done"}
        max_replans = 1
        try:
            max_replans = max(0, min(3, int((row.policy_json or {}).get("max_replans") or 1)))
        except (TypeError, ValueError):
            max_replans = 1
        replans = (
            db.query(WorkStep)
            .filter_by(work_id=row.work_id, kind="agent.plan")
            .filter(WorkStep.input_json.isnot(None))
            .all()
        )
        replan_count = sum(1 for item in replans if bool((item.input_json or {}).get("replan")))
        if replan_count < max_replans:
            row.status = "running"
            row.blocked_reason = ""
            row.updated_at = _now()
            _add_step(
                db, row, kind="agent.plan",
                step_key=f"plan:replan:{int(row.plan_revision or 0)}:{replan_count + 1}",
                agent_id="conductor", role_id="conductor",
                title="根据验收结果重新规划",
                intent="验收未通过，重新规划可执行步骤：" + str(reason or "acceptance_not_met"),
                input_json={"replan": True, "reason": reason or "acceptance_not_met",
                            "review": review},
                status="ready",
            )
            append_event(db, row, "work.replanned", step_id=current.step_id,
                         payload={"reason": reason or "acceptance_not_met",
                                  "replan_count": replan_count + 1})
            finish_step_in_db(db, current, status="failed", error=reason or "acceptance_not_met",
                              output={"verdict": review.get("verdict"), "reason": reason,
                                      "replanned": True})
            return {"status": "done", "replanned": True}
        row.status = "blocked"
        row.blocked_reason = reason or "acceptance_not_met"
        row.updated_at = _now()
        append_event(db, row, "work.blocked", step_id=current.step_id,
                     payload={"reason": row.blocked_reason, "review": review})
        upsert_agent_session(db, row, agent_id="conductor", role_id="conductor",
                             job_id="work-review", summary="验收未通过：" + str(row.blocked_reason),
                             last_event_seq=int(row.last_event_seq or 0))
        remember(db, row, agent_id="conductor", kind="lesson",
                 content="验收未通过：" + str(row.blocked_reason),
                 source_ref=current.step_id, confidence=60, ttl_days=90)
        finish_step_in_db(db, current, status="failed", error=row.blocked_reason,
                          output={"verdict": review.get("verdict"), "reason": reason})
        return {"status": "failed", "error": row.blocked_reason}


HANDLERS = {
    "agent.plan": handle_agent_plan,
    "agent.turn": handle_agent_turn,
    "tool.call": handle_tool_call,
    "run.wait": handle_run_wait,
    "human.approval": handle_human_approval,
    "job.call": handle_job_call,
    "work.review": handle_work_review,
}


def execute_step(step: dict[str, Any], work: dict[str, Any]) -> dict[str, Any]:
    handler = HANDLERS.get(str(step.get("kind") or ""))
    if not handler:
        raise work_agent.WorkAgentError("handler_not_found:" + str(step.get("kind")), permanent=True)
    return handler(step, work)
