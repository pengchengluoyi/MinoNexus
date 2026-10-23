"""工作层 LLM：计划、角色回合、验收。"""
from __future__ import annotations

import json
from typing import Any

from mino_nexus.assistant.work.work_store import add_tokens, normalize_acceptance


class WorkAgentError(RuntimeError):
    def __init__(self, message: str, *, permanent: bool = False):
        super().__init__(message)
        self.permanent = permanent


def _provider():
    from mino_nexus.platform.llm.client import resolve_regression_provider

    provider, gate = resolve_regression_provider()
    if not provider:
        reason = str((gate or {}).get("reason") or "未配置可用的大模型")
        raise WorkAgentError("llm_not_configured:" + reason)
    return provider


def _render_json(job_id: str, slots: dict[str, Any], work: dict[str, Any]) -> dict[str, Any]:
    from mino_nexus.platform.llm.client import call_chat_text
    from mino_nexus.platform.llm.render import render

    text_slots = {
        key: value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
        for key, value in slots.items()
    }
    messages, info = render(job_id, text_slots)
    provider = _provider()
    parsed, meta = call_chat_text(
        provider=provider,
        messages=messages,
        temperature=0.1,
        max_tokens=2400,
        timeout_sec=120,
        json_mode=True,
    )
    if not isinstance(parsed, dict):
        raise WorkAgentError("llm_no_json:" + str(meta.get("error") or ""))
    if meta.get("truncated") or meta.get("salvaged"):
        raise WorkAgentError("llm_output_incomplete")
    usage = meta.get("usage") if isinstance(meta.get("usage"), dict) else {}
    try:
        add_tokens(work["work_id"], int(usage.get("total_tokens") or 0))
    except Exception:
        pass
    return parsed


def role_specs() -> list[dict[str, Any]]:
    from mino_nexus.platform.llm.repo import role_specs as _role_specs

    return [dict(row) for row in _role_specs()]


def _team() -> list[dict[str, Any]]:
    rows = []
    for spec in role_specs():
        rid = str(spec.get("id") or "")
        if not rid:
            continue
        jobs = list(spec.get("chat_jobs") or [])
        if rid != "conductor" and not jobs:
            continue
        rows.append({
            "id": rid,
            "label": str(spec.get("label") or rid),
            "chat_jobs": jobs[:8],
            "work_tools": list(spec.get("work_tools") or spec.get("tools") or [])[:30],
        })
    return rows


def _role(role_id: str) -> dict[str, Any]:
    return next((r for r in role_specs() if str(r.get("id") or "") == role_id), {})


def role_tools(role_id: str) -> list[str]:
    spec = _role(role_id)
    return [str(x) for x in (spec.get("work_tools") or spec.get("tools") or []) if str(x).strip()]


def available_tools(role_id: str) -> list[dict[str, Any]]:
    from mino_nexus.assistant.assistant_tools import all_tools

    wanted = set(role_tools(role_id))
    out = []
    for spec in all_tools():
        sid = str(spec.get("id") or "")
        if sid not in wanted:
            continue
        out.append({
            "id": sid,
            "title": str(spec.get("title") or sid),
            "description": str(spec.get("description") or ""),
            "risk": str(spec.get("risk") or "read"),
            "params_schema": spec.get("params_schema") or {},
        })
    return out


def _bounded(value: Any, limit: int = 16000) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + "…[truncated]"


def workspace_context(work: dict[str, Any]) -> dict[str, Any]:
    """把项目/应用的真实 ID 固定给模型，避免把中文名当 project_id/app_id。"""
    from mino_nexus.resources import project_store as ps

    pid = str(work.get("project_id") or "")
    project = ps.find_project(pid) or {}
    apps = ps.list_apps(pid) if pid else []
    return {
        "project": {"id": pid, "name": str(project.get("name") or "")},
        "apps": [
            {"id": str(a.get("id") or ""), "name": str(a.get("name") or ""),
             "platforms": str(a.get("platforms") or "")}
            for a in apps
        ],
        "id_rule": "工具参数里的 project_id / app_id 必须用上面的 id；名称只能用于搜索或展示，不能填进 ID 字段。",
    }


def load_facts(work: dict[str, Any]) -> dict[str, Any]:
    """工作区事实只给计划/回合做背景；实时数字由工具返回，不在这里复制。"""
    return {
        "project_id": work.get("project_id") or "",
        "topic_id": work.get("topic_id") or "",
        "goal": work.get("goal") or "",
        "acceptance": work.get("acceptance") or [],
        "plan": work.get("plan") or {},
        "instructions": work.get("instructions") or [],
        "workspace_context": workspace_context(work),
    }


def plan_work(work: dict[str, Any]) -> dict[str, Any]:
    team = _team()
    session_summary, memories = _agent_context(work, "conductor")
    parsed = _render_json("work-plan", {
        "goal": work.get("goal") or "",
        "acceptance": _bounded(work.get("acceptance") or [], 4000),
        "workspace_facts": _bounded(load_facts(work), 16000),
        "team": _bounded(team, 10000),
        "constraints": _bounded(work.get("instructions") or [], 4000),
        "budget": _bounded(work.get("budget") or {}, 2000),
        "agent_session": session_summary[:4000],
        "memories": memories[:4000],
        "previous_review": _bounded(work.get("review") or {}, 5000),
    }, work)
    user_acceptance = normalize_acceptance(work.get("acceptance") or [])
    acceptance = user_acceptance or normalize_acceptance(parsed.get("acceptance") or [])
    if not acceptance:
        raise WorkAgentError("plan_acceptance_empty", permanent=True)
    raw_steps = parsed.get("steps")
    if not isinstance(raw_steps, list) or not (1 <= len(raw_steps) <= 12):
        raise WorkAgentError("plan_step_count_invalid", permanent=True)
    team_ids = {str(row["id"]) for row in team}
    steps: list[dict[str, Any]] = []
    seen: set[str] = set()
    for idx, raw in enumerate(raw_steps, 1):
        if not isinstance(raw, dict):
            raise WorkAgentError("plan_step_invalid", permanent=True)
        sid = str(raw.get("id") or f"p{idx}")[:64]
        if sid in seen:
            raise WorkAgentError("plan_step_duplicate:" + sid, permanent=True)
        seen.add(sid)
        agent_id = str(raw.get("agent_id") or "conductor")[:64]
        if agent_id not in team_ids:
            raise WorkAgentError("plan_agent_unknown:" + agent_id, permanent=True)
        title = str(raw.get("title") or f"步骤 {idx}")[:160]
        intent = str(raw.get("intent") or title)[:1200]
        if not title.strip() or not intent.strip():
            raise WorkAgentError("plan_step_empty", permanent=True)
        steps.append({"id": sid, "title": title, "agent_id": agent_id, "intent": intent})
    return {
        "title": str(parsed.get("title") or work.get("title") or work.get("goal") or "")[:200],
        "goal": str(parsed.get("goal") or work.get("goal") or "")[:4000],
        "acceptance": acceptance,
        "steps": steps,
        "risks": [str(x)[:400] for x in (parsed.get("risks") or []) if str(x).strip()][:8],
    }


def _agent_context(work: dict[str, Any], role_id: str) -> tuple[str, str]:
    session = next((s for s in (work.get("agent_sessions") or [])
                    if str(s.get("agent_id") or "") == role_id), None)
    summary = str((session or {}).get("summary") or "")
    lines = []
    for mem in (work.get("memories") or []):
        if not isinstance(mem, dict):
            continue
        if str(mem.get("agent_id") or "") not in ("", role_id):
            continue
        content = str(mem.get("content") or "").strip()
        if content:
            lines.append(f"- [{mem.get('kind') or 'fact'}] {content}")
    return summary, "\n".join(lines[-30:])


def turn_work(work: dict[str, Any], step: dict[str, Any],
              prior_results: list[dict[str, Any]], approvals: list[dict[str, Any]]) -> dict[str, Any]:
    role_id = str(step.get("agent_id") or "conductor")
    role = _role(role_id)
    tools = available_tools(role_id)
    session_summary, memories = _agent_context(work, role_id)
    parsed = _render_json("work-turn", {
        "role_brief": _bounded(role.get("default_prompt") or role.get("summary") or role_id, 3000),
        "work_goal": work.get("goal") or "",
        "workspace_context": _bounded(workspace_context(work), 6000),
        "acceptance": _bounded(work.get("acceptance") or [], 4000),
        "plan": _bounded(work.get("plan") or {}, 10000),
        "current_step": _bounded(step, 5000),
        "prior_results": _bounded(prior_results, 16000),
        "approvals": _bounded(approvals, 4000),
        "tools": _bounded(tools, 12000),
        "instructions": _bounded(work.get("instructions") or [], 3000),
        "agent_session": session_summary[:4000],
        "memories": memories[:4000],
    }, work)
    action = str(parsed.get("action") or "reply").strip()
    if action not in ("tool", "reply", "delegate", "approval", "complete"):
        raise WorkAgentError("turn_action_invalid", permanent=True)
    if action == "tool":
        tool_id = str(parsed.get("tool_id") or "").strip()
        if not tool_id or tool_id not in {t["id"] for t in tools}:
            raise WorkAgentError("turn_tool_not_allowed:" + tool_id, permanent=True)
        args = parsed.get("args")
        if not isinstance(args, dict):
            raise WorkAgentError("turn_args_invalid", permanent=True)
        return {"action": "tool", "tool_id": tool_id, "args": args,
                "text": str(parsed.get("text") or "")[:2000]}
    if action == "delegate":
        target = str(parsed.get("agent_id") or "").strip()
        if not target or target not in {row["id"] for row in _team()}:
            raise WorkAgentError("delegate_target_invalid", permanent=True)
        return {"action": "delegate", "agent_id": target,
                "intent": str(parsed.get("intent") or parsed.get("text") or "")[:1200]}
    if action == "approval":
        return {"action": "approval",
                "title": str(parsed.get("title") or "需要确认")[:200],
                "prompt": str(parsed.get("prompt") or parsed.get("text") or "")[:4000]}
    if action == "complete":
        return {"action": "complete", "text": str(parsed.get("text") or "")[:4000]}
    return {"action": "reply", "text": str(parsed.get("text") or "")[:4000]}


def review_work(work: dict[str, Any], artifacts: list[dict[str, Any]],
                runs: list[dict[str, Any]],
                results: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    session_summary, memories = _agent_context(work, "conductor")
    parsed = _render_json("work-review", {
        "goal": work.get("goal") or "",
        "acceptance": _bounded(work.get("acceptance") or [], 5000),
        "plan": _bounded(work.get("plan") or {}, 8000),
        "results": _bounded(results or [], 12000),
        "artifacts": _bounded(artifacts, 12000),
        "runs": _bounded(runs, 8000),
        "agent_session": session_summary[:4000],
        "memories": memories[:4000],
    }, work)
    verdict = str(parsed.get("verdict") or "blocked")
    if verdict not in ("done", "blocked", "failed"):
        raise WorkAgentError("review_verdict_invalid", permanent=True)
    criteria = []
    for raw in parsed.get("criteria") or []:
        if not isinstance(raw, dict):
            continue
        evidence = []
        for ev in raw.get("evidence") or []:
            if isinstance(ev, str):
                evidence.append({"type": "ref", "ref": ev})
            elif isinstance(ev, dict):
                evidence.append({
                    "type": str(ev.get("type") or "ref"),
                    "ref": str(ev.get("ref") or ev.get("id") or ""),
                    "note": str(ev.get("note") or "")[:500],
                })
        criteria.append({
            "id": str(raw.get("id") or "")[:32],
            "met": bool(raw.get("met")),
            "note": str(raw.get("note") or "")[:1000],
            "evidence": evidence[:8],
        })
    return {"verdict": verdict, "criteria": criteria,
            "summary": str(parsed.get("summary") or "")[:4000]}


def validate_review(work: dict[str, Any], review: dict[str, Any],
                    artifacts: list[dict[str, Any]], links: list[dict[str, Any]]) -> tuple[bool, str]:
    """模型说完成不算；逐条验收 + 证据必须能落到本地真实记录。"""
    from mino_nexus.casework import run_store
    from mino_nexus.core.database import SessionLocal
    from mino_nexus.models.session_event import SessionMeta

    acceptance = [a for a in (work.get("acceptance") or []) if a.get("required", True)]
    if not acceptance:
        return False, "acceptance_empty"
    criteria = {str(c.get("id") or ""): c for c in (review.get("criteria") or [])}
    artifact_ids = {str(a.get("artifact_id") or "") for a in artifacts}
    artifact_by_id = {str(a.get("artifact_id") or ""): a for a in artifacts}
    link_refs = {(str(l.get("kind") or ""), str(l.get("ref_id") or "")) for l in links}
    for item in acceptance:
        cid = str(item.get("id") or "")
        crit = criteria.get(cid)
        if not crit or not crit.get("met"):
            return False, "acceptance_not_met:" + cid
        evidence = crit.get("evidence") or []
        if not evidence:
            return False, "evidence_missing:" + cid
        for ev in evidence:
            typ = str(ev.get("type") or "ref")
            ref = str(ev.get("ref") or "")
            if not ref:
                return False, "evidence_ref_empty:" + cid
            # 验收模型有时把 artifact id 填进 ref；只要 artifact 真实存在，
            # 就按 artifact 的 ref_id 解析成 run/session，避免误杀正确结论。
            referenced = artifact_by_id.get(ref)
            if referenced:
                art_kind = str(referenced.get("kind") or "")
                if typ in ("run", "task") and art_kind == "run":
                    ref, typ = str(referenced.get("ref_id") or ""), "run"
                elif typ == "session" and art_kind == "session":
                    ref, typ = str(referenced.get("ref_id") or ""), "session"
            if typ == "ref":
                if ("run", ref) in link_refs:
                    typ = "run"
                elif ("session", ref) in link_refs:
                    typ = "session"
                elif ref in artifact_ids:
                    typ = "artifact"
            if typ in ("run", "task"):
                doc = run_store.get(ref)
                if not doc:
                    return False, "evidence_run_not_found:" + ref
                if run_store.task_is_live(doc):
                    return False, "evidence_run_not_terminal:" + ref
                if ("run", ref) not in link_refs:
                    return False, "evidence_run_not_linked:" + ref
            elif typ == "session":
                with SessionLocal() as db:
                    row = db.get(SessionMeta, ref)
                    if not row or (not row.finished_at and str(row.status or "") not in
                                   ("done", "finished", "failed", "cancelled", "interrupted")):
                        return False, "evidence_session_not_finished:" + ref
                if ("session", ref) not in link_refs:
                    return False, "evidence_session_not_linked:" + ref
            elif typ == "artifact":
                if ref not in artifact_ids:
                    return False, "evidence_artifact_not_found:" + ref
            elif typ == "decision":
                approval = next((a for a in (work.get("approvals") or [])
                                 if str(a.get("approval_id") or "") == ref), None)
                if not approval or approval.get("status") != "approved":
                    return False, "evidence_decision_not_approved:" + ref
            else:
                return False, "evidence_type_invalid:" + typ
    if review.get("verdict") != "done":
        return False, "review_verdict_not_done"
    return True, ""


def review_summary(review: dict[str, Any]) -> str:
    return str(review.get("summary") or "")[:1000]
