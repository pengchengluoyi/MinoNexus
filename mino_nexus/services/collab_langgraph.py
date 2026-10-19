"""LangGraph 交付流水线：需求分析 → 评审 → 脑图 → 用例 → 评审 → 工作项规格。"""
from __future__ import annotations

import json
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from mino_nexus.core.log import SLog
from mino_nexus.services.collab_agent_transcript import append_turn

TAG = "CollabLangGraph"


class DeliveryState(TypedDict, total=False):
    goal: str
    discussion: str
    memory: str
    topic_id: str
    topic_title: str
    app_id: str
    requirement: dict[str, Any]
    analyze_art: dict[str, Any]
    req_review_art: dict[str, Any]
    mindmap_art: dict[str, Any]
    draft_art: dict[str, Any]
    case_review_art: dict[str, Any]
    blocked: bool
    block_reason: str
    work_specs: list[dict[str, Any]]
    pipeline_trace: list[dict[str, Any]]
    agent_transcript: list[dict[str, Any]]


def _trace(state: DeliveryState, step: str, status: str, detail: str = "") -> None:
    rows = list(state.get("pipeline_trace") or [])
    rows.append({"step": step, "status": status, "detail": detail[:500]})
    state["pipeline_trace"] = rows


def _req(state: DeliveryState) -> dict[str, Any]:
    base = dict(state.get("requirement") or {})
    goal = str(state.get("goal") or "").strip()
    if goal and not base.get("source_text"):
        base["source_text"] = goal
    if not base.get("title"):
        base["title"] = str(state.get("topic_title") or "议题")
    if not base.get("id"):
        base["id"] = str(state.get("topic_id") or "")
    feedback_parts = [str(state.get("memory") or "").strip(), str(state.get("discussion") or "").strip()]
    merged = "\n".join(p for p in feedback_parts if p)
    if merged:
        base["human_feedback"] = merged
    return base


def _node_analyze(state: DeliveryState) -> DeliveryState:
    from mino_nexus.services.qa_cover import run_llm_job

    append_turn(state, agent_id="coordinator", step="kickoff", status="running", body="分配需求分析任务")
    append_turn(state, agent_id="req-analyst", step="analyze_req", status="running", body="正在理解议题目标与讨论上下文")
    req = _req(state)
    art = run_llm_job("analyze_req", requirement=req, cases=[], qa_process=None)
    state["analyze_art"] = art
    if art.get("status") != "ok":
        state["blocked"] = True
        state["block_reason"] = str(art.get("error") or "需求分析失败")
        _trace(state, "analyze_req", "error", state["block_reason"])
        append_turn(state, agent_id="req-analyst", step="analyze_req", status="error", body=state["block_reason"])
        return state
    payload = art.get("payload") if isinstance(art.get("payload"), dict) else {}
    req = dict(req)
    if payload.get("summary"):
        req["summary"] = payload["summary"]
    und = dict(req.get("understanding") or {})
    und.update({k: payload[k] for k in ("summary", "ac", "points") if k in payload})
    req["understanding"] = und
    state["requirement"] = req
    _trace(state, "analyze_req", "ok")
    summary = str(payload.get("summary") or "")[:1200]
    append_turn(state, agent_id="req-analyst", step="analyze_req", status="ok", body=summary or "分析完成")
    return state


def _node_req_review(state: DeliveryState) -> DeliveryState:
    if state.get("blocked"):
        return state
    from mino_nexus.services.qa_cover import _ask_json  # noqa: PLC2701

    append_turn(state, agent_id="req-reviewer", step="req-qa-bm", status="running", body="评审需求可测性与范围")
    req = _req(state)
    user = json.dumps(
        {"title": req.get("title"), "understanding": req.get("understanding"), "source_text": req.get("source_text")},
        ensure_ascii=False,
    )
    parsed, meta = _ask_json("req-qa-bm", user)
    art = {"status": "ok" if parsed is not None else "error", "payload": parsed or {}, "meta": meta}
    state["req_review_art"] = art
    verdict = str((parsed or {}).get("verdict") or (parsed or {}).get("status") or "pass").lower()
    if parsed is None:
        _trace(state, "req-qa-bm", "skip", str(meta.get("error") or "评审未执行"))
        return state
    if verdict in ("reject", "blocked", "needs_correction", "fail"):
        state["blocked"] = True
        state["block_reason"] = str((parsed or {}).get("message") or (parsed or {}).get("summary") or "需求评审未通过")
        _trace(state, "req-qa-bm", "blocked", state["block_reason"])
        append_turn(state, agent_id="req-reviewer", step="req-qa-bm", status="blocked", body=state["block_reason"])
        return state
    _trace(state, "req-qa-bm", "ok")
    append_turn(state, agent_id="req-reviewer", step="req-qa-bm", status="ok", body="需求评审通过")
    return state


def _node_mindmap(state: DeliveryState) -> DeliveryState:
    if state.get("blocked"):
        return state
    from mino_nexus.services.qa_cover import run_llm_job

    append_turn(state, agent_id="mindmap", step="draft_mindmap", status="running", body="生成测试脑图")
    req = _req(state)
    art = run_llm_job("draft_mindmap", requirement=req, cases=[], qa_process=None)
    state["mindmap_art"] = art
    if art.get("status") != "ok":
        _trace(state, "draft_mindmap", "error", str(art.get("error") or ""))
        return state
    payload = art.get("payload") if isinstance(art.get("payload"), dict) else {}
    req = dict(req)
    tree = payload.get("mindmap") if isinstance(payload.get("mindmap"), dict) else payload
    req["mindmap"] = tree
    state["requirement"] = req
    _trace(state, "draft_mindmap", "ok")
    append_turn(state, agent_id="mindmap", step="draft_mindmap", status="ok", body="脑图已生成")
    return state


def _node_draft_cases(state: DeliveryState) -> DeliveryState:
    if state.get("blocked"):
        return state
    from mino_nexus.services.qa_cover import run_llm_job

    append_turn(state, agent_id="case-author", step="draft_cases", status="running", body="起草用例候选")
    req = _req(state)
    art = run_llm_job("draft_cases", requirement=req, cases=[], qa_process=None)
    state["draft_art"] = art
    if art.get("status") != "ok":
        _trace(state, "draft_cases", "error", str(art.get("error") or ""))
        return state
    _trace(state, "draft_cases", "ok")
    append_turn(state, agent_id="case-author", step="draft_cases", status="ok", body="用例候选已生成，待人工确认写入")
    return state


def _node_case_review(state: DeliveryState) -> DeliveryState:
    if state.get("blocked"):
        return state
    from mino_nexus.services.qa_cover import _ask_json  # noqa: PLC2701

    append_turn(state, agent_id="case-reviewer", step="knowledge-reviewer", status="running", body="评审用例候选")
    draft = state.get("draft_art") if isinstance(state.get("draft_art"), dict) else {}
    payload = draft.get("payload") if isinstance(draft.get("payload"), dict) else {}
    user = json.dumps({"cases": payload.get("cases") or [], "requirement": _req(state)}, ensure_ascii=False)
    parsed, meta = _ask_json("knowledge-reviewer", user)
    art = {"status": "ok" if parsed is not None else "error", "payload": parsed or {}, "meta": meta}
    state["case_review_art"] = art
    if parsed is None:
        _trace(state, "knowledge-reviewer", "skip", str(meta.get("error") or ""))
        return state
    verdict = str((parsed or {}).get("verdict") or "pass").lower()
    if verdict in ("reject", "blocked", "needs_correction", "fail"):
        state["blocked"] = True
        state["block_reason"] = str((parsed or {}).get("message") or "用例评审未通过")
        _trace(state, "knowledge-reviewer", "blocked", state["block_reason"])
        append_turn(state, agent_id="case-reviewer", step="knowledge-reviewer", status="blocked", body=state["block_reason"])
        return state
    _trace(state, "knowledge-reviewer", "ok")
    append_turn(state, agent_id="case-reviewer", step="knowledge-reviewer", status="ok", body="用例评审通过")
    return state


def _openhands_spec(goal: str) -> dict[str, Any] | None:
    from mino_nexus.services.collab_openhands import openhands_is_configured

    if not openhands_is_configured():
        return None
    return {
        "title": "OpenHands 开发任务",
        "goal": goal or "按议题目标修改代码或脚本",
        "action_kind": "openhands",
        "action_payload": {"prompt": goal},
    }


def _node_emit_specs(state: DeliveryState) -> DeliveryState:
    specs: list[dict[str, Any]] = []
    analyze = state.get("analyze_art") if isinstance(state.get("analyze_art"), dict) else {}
    draft = state.get("draft_art") if isinstance(state.get("draft_art"), dict) else {}
    state["deliverables"] = {
        "analyze": analyze,
        "req_review": state.get("req_review_art"),
        "mindmap": state.get("mindmap_art"),
        "draft": draft,
        "case_review": state.get("case_review_art"),
    }
    if state.get("blocked"):
        specs.append(
            {
                "_slot": "blocked",
                "title": "待纠正",
                "goal": str(state.get("block_reason") or "评审未通过"),
                "action_kind": "none",
                "status": "needs_correction",
            }
        )
    else:
        if draft.get("status") == "ok":
            specs.append(
                {
                    "_slot": "apply_drafts",
                    "title": "用例候选",
                    "goal": "模型生成的用例草稿，校验去重后写入项目",
                    "action_kind": "apply_drafts",
                    "action_payload": {"artifact": draft},
                    "acceptance": "写入证据含新增/修改/跳过重复",
                    "origin": "pipeline",
                }
            )
            specs.append(
                {
                    "_slot": "compile",
                    "title": "编译用例",
                    "goal": "草稿写入后排队编译",
                    "action_kind": "compile",
                    "depends_on_slots": ["apply_drafts"],
                    "origin": "pipeline",
                }
            )
        specs.append(
            {
                "_slot": "review_record",
                "title": "多角色评审记录",
                "goal": "需求与用例评审结论（只读）",
                "action_kind": "none",
                "status": "done",
                "_artifact": {
                    "req_review": state.get("req_review_art"),
                    "case_review": state.get("case_review_art"),
                },
            }
        )
        oh = _openhands_spec(str(state.get("goal") or ""))
        if oh:
            specs.append(oh)
        app_id = str(state.get("app_id") or "").strip()
        if app_id:
            specs.append(
                {
                    "_slot": "case_run",
                    "title": "派发用例执行",
                    "goal": "编译通过后按策略自动执行",
                    "action_kind": "case_run",
                    "action_payload": {"app_id": app_id, "async_exec": True},
                    "depends_on_slots": ["compile"],
                    "origin": "pipeline",
                }
            )
    state["work_specs"] = specs
    _trace(state, "emit", "ok", f"{len(specs)} specs")
    append_turn(
        state,
        agent_id="emitter",
        step="emit",
        status="ok" if not state.get("blocked") else "blocked",
        body=f"生成 {len(specs)} 个工作项",
    )
    return state


def _route_after_analyze(state: DeliveryState) -> str:
    return "stop" if state.get("blocked") else "req_review"


def _route_after_req_review(state: DeliveryState) -> str:
    return "stop" if state.get("blocked") else "mindmap"


def _route_after_case_review(state: DeliveryState) -> str:
    return "emit"


def build_delivery_graph():
    g = StateGraph(DeliveryState)
    g.add_node("analyze", _node_analyze)
    g.add_node("req_review", _node_req_review)
    g.add_node("mindmap", _node_mindmap)
    g.add_node("draft_cases", _node_draft_cases)
    g.add_node("case_review", _node_case_review)
    g.add_node("emit", _node_emit_specs)
    g.set_entry_point("analyze")
    g.add_conditional_edges("analyze", _route_after_analyze, {"stop": "emit", "req_review": "req_review"})
    g.add_conditional_edges("req_review", _route_after_req_review, {"stop": "emit", "mindmap": "mindmap"})
    g.add_edge("mindmap", "draft_cases")
    g.add_edge("draft_cases", "case_review")
    g.add_conditional_edges("case_review", _route_after_case_review, {"emit": "emit"})
    g.add_edge("emit", END)
    return g.compile()


_GRAPH = None


def _sanitize_draw_mermaid(text: str) -> str:
    """LangGraph draw_mermaid 含 HTML 标签与 YAML 头，Mermaid 11 严格模式无法解析。"""
    import re

    raw = str(text or "").strip()
    if not raw:
        return ""
    if raw.startswith("---"):
        rest = raw[3:]
        end = rest.find("\n---")
        if end >= 0:
            raw = rest[end + 4 :].strip()
    raw = re.sub(r"<[^>]+>", "", raw)
    raw = raw.replace("&nbsp;", " ")
    raw = re.sub(
        r"\s-\.\s+([^;]+?)\s+\.->",
        lambda m: f' -."{m.group(1).strip().replace(chr(34), chr(39))}" .->',
        raw,
    )
    return raw.strip()


def delivery_graph_mermaid() -> str:
    """LangGraph 编译图自带的 Mermaid（与 Studio 同源 API）。"""
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = build_delivery_graph()
    try:
        return _sanitize_draw_mermaid(str(_GRAPH.get_graph().draw_mermaid()))
    except Exception as exc:
        SLog.w(TAG, f"draw_mermaid skipped: {exc}")
        return ""


def run_delivery_pipeline(
    *,
    topic_id: str,
    topic_title: str,
    goal: str,
    discussion: str = "",
    memory: str = "",
    app_id: str = "",
) -> DeliveryState:
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = build_delivery_graph()
    from mino_nexus.services.collab_delivery_state import new_delivery_state

    initial: DeliveryState = new_delivery_state(
        topic_id=topic_id,
        topic_title=topic_title,
        goal=goal,
        discussion=discussion,
        memory=memory,
        app_id=app_id,
    )
    try:
        out = _GRAPH.invoke(initial)
        out["engine"] = "langgraph"
        return out
    except Exception as exc:
        SLog.e(TAG, f"pipeline failed: {exc}")
        initial["blocked"] = True
        initial["block_reason"] = str(exc)
        _trace(initial, "graph", "error", str(exc))
        return initial
