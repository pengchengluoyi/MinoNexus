"""多 Agent 协作轨迹与画布节点（Workbench dots 视图数据源）。"""
from __future__ import annotations

from typing import Any

AGENT_ROLES: dict[str, dict[str, str]] = {
    "coordinator": {"label": "协调者", "kind": "leader"},
    "req-analyst": {"label": "需求分析", "kind": "member"},
    "req-reviewer": {"label": "需求评审", "kind": "member"},
    "mindmap": {"label": "脑图", "kind": "member"},
    "case-author": {"label": "用例设计", "kind": "member"},
    "case-reviewer": {"label": "用例评审", "kind": "member"},
    "emitter": {"label": "交付", "kind": "member"},
}

def _mermaid_id(agent_id: str) -> str:
    return str(agent_id or "node").replace("-", "_")


def build_mermaid_flowchart(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, str]],
    *,
    active_agent: str = "",
) -> str:
    """Mermaid flowchart（Workbench 用官方 mermaid 渲染，不手绘 SVG）。"""
    lines = ["flowchart LR"]
    for node in nodes:
        nid = _mermaid_id(str(node.get("id") or ""))
        label = str(node.get("label") or node.get("id") or nid).replace('"', "'")
        st = str(node.get("status") or "idle")
        if active_agent and str(node.get("id") or "") == active_agent:
            st = "running"
        lines.append(f'  {nid}["{label}"]:::{st}')
    for edge in edges:
        a = _mermaid_id(str(edge.get("from") or ""))
        b = _mermaid_id(str(edge.get("to") or ""))
        if a and b:
            lines.append(f"  {a} --> {b}")
    lines.extend(
        [
            "classDef ok fill:#d9f7be,stroke:#389e0d,color:#135200",
            "classDef running fill:#e6f4ff,stroke:#1677ff,color:#003a8c",
            "classDef error fill:#fff1f0,stroke:#cf1322,color:#820014",
            "classDef blocked fill:#fff1f0,stroke:#cf1322,color:#820014",
            "classDef idle fill:#fafafa,stroke:#d9d9d9,color:#595959",
            "classDef pending fill:#fffbe6,stroke:#d48806,color:#613400",
        ]
    )
    return "\n".join(lines)


# LangGraph 编译图节点 id → 画布 Agent id（Workbench 双向高亮）
LANGGRAPH_AGENT_LINKS: list[tuple[str, str, str]] = [
    ("analyze", "req-analyst", "需求分析"),
    ("req_review", "req-reviewer", "需求评审"),
    ("mindmap", "mindmap", "脑图"),
    ("draft_cases", "case-author", "用例设计"),
    ("case_review", "case-reviewer", "用例评审"),
    ("emit", "emitter", "交付"),
]

PIPELINE_EDGES: list[tuple[str, str]] = [
    ("coordinator", "req-analyst"),
    ("req-analyst", "req-reviewer"),
    ("req-reviewer", "mindmap"),
    ("mindmap", "case-author"),
    ("case-author", "case-reviewer"),
    ("case-reviewer", "emitter"),
]


def append_turn(
    state: dict[str, Any],
    *,
    agent_id: str,
    step: str,
    status: str,
    body: str = "",
    detail: str = "",
) -> None:
    rows = list(state.get("agent_transcript") or [])
    meta = AGENT_ROLES.get(agent_id, {"label": agent_id, "kind": "member"})
    rows.append(
        {
            "agent_id": agent_id,
            "label": meta["label"],
            "kind": meta["kind"],
            "step": step,
            "status": status,
            "body": str(body or detail or "")[:4000],
            "detail": str(detail or "")[:500],
        }
    )
    state["agent_transcript"] = rows


def build_agent_canvas(state: dict[str, Any]) -> dict[str, Any]:
    transcript = list(state.get("agent_transcript") or [])
    trace = list(state.get("pipeline_trace") or [])
    status_by_agent: dict[str, str] = {}
    last_body: dict[str, str] = {}
    for row in transcript:
        aid = str(row.get("agent_id") or "")
        if aid:
            status_by_agent[aid] = str(row.get("status") or "pending")
            if row.get("body"):
                last_body[aid] = str(row["body"])[:280]

    nodes: list[dict[str, Any]] = []
    for aid, meta in AGENT_ROLES.items():
        st = status_by_agent.get(aid, "idle")
        if aid == "coordinator" and transcript and st == "idle":
            st = "ok"
        idx = len(nodes)
        nodes.append(
            {
                "id": aid,
                "label": meta["label"],
                "kind": meta["kind"],
                "status": st,
                "snippet": last_body.get(aid, ""),
                "position": {
                    "x": float(idx * 200),
                    "y": 0.0 if meta["kind"] == "leader" else 48.0,
                },
            }
        )
    edges = [{"from": a, "to": b} for a, b in PIPELINE_EDGES]
    active_agent = ""
    for row in reversed(transcript):
        st = str(row.get("status") or "")
        if st in ("running", "blocked"):
            active_agent = str(row.get("agent_id") or "")
            break
    trace_by_step: dict[str, str] = {}
    for row in trace:
        step = str(row.get("step") or "")
        if step:
            trace_by_step[step] = str(row.get("status") or "")

    langgraph_steps: list[dict[str, Any]] = []
    agent_to_langgraph: dict[str, str] = {}
    langgraph_to_agent: dict[str, str] = {}
    for lg_id, agent_id, label in LANGGRAPH_AGENT_LINKS:
        langgraph_to_agent[lg_id] = agent_id
        agent_to_langgraph[agent_id] = lg_id
        st = trace_by_step.get(lg_id) or status_by_agent.get(agent_id, "idle")
        langgraph_steps.append(
            {
                "id": lg_id,
                "label": label,
                "agent_id": agent_id,
                "status": st,
            }
        )

    mermaid = build_mermaid_flowchart(nodes, edges, active_agent=active_agent)
    return {
        "nodes": nodes,
        "edges": edges,
        "transcript": transcript[-40:],
        "trace": trace[-20:],
        "engine": str(state.get("engine") or ""),
        "layout": "pipeline",
        "active_agent": active_agent,
        "mermaid": mermaid,
        "langgraph_steps": langgraph_steps,
        "agent_to_langgraph": agent_to_langgraph,
        "langgraph_to_agent": langgraph_to_agent,
        "renderers": {
            "mermaid": "https://mermaid.js.org",
            "xyflow": "https://reactflow.dev",
            "langgraph": "https://github.com/langchain-ai/langgraph",
            "agentscope": "https://github.com/agentscope-ai/agentscope",
        },
    }
