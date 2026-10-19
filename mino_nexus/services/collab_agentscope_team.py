"""AgentScope TeamPipeline：协调者 TeamAssign + 成员工具对接 qa_cover / LangGraph 节点。"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

from mino_nexus.core.log import SLog
from mino_nexus.services.collab_agent_transcript import append_turn
from mino_nexus.services.collab_delivery_state import new_delivery_state

TAG = "CollabAgentScopeTeam"


def _user_msg(text: str):
    from agentscope.message import UserMsg

    body = str(text or "").strip() or "请按流程完成测试交付。"
    return UserMsg(name="user", content=body)


def _bind_node_runners() -> list[tuple[str, str, str, Callable[[dict[str, Any]], dict[str, Any]]]]:
    from mino_nexus.services.collab_langgraph import (
        _node_analyze,
        _node_case_review,
        _node_draft_cases,
        _node_mindmap,
        _node_req_review,
    )

    return [
        ("req-analyst", "需求分析：理解议题目标与讨论，产出可测需求摘要", "analyze_req", _node_analyze),
        ("req-reviewer", "需求评审：检查范围与可测性", "req-qa-bm", _node_req_review),
        ("mindmap", "脑图：生成测试结构脑图", "draft_mindmap", _node_mindmap),
        ("case-author", "用例设计：起草用例候选", "draft_cases", _node_draft_cases),
        ("case-reviewer", "用例评审：检查用例候选质量", "knowledge-reviewer", _node_case_review),
    ]


def _make_member_tool(
    state: dict[str, Any],
    agent_id: str,
    step: str,
    runner: Callable[[dict[str, Any]], dict[str, Any]],
):
    from agentscope.tool import FunctionTool

    def deliver_step(task_brief: str) -> str:
        if state.get("blocked"):
            return f"流水线已阻塞，跳过：{state.get('block_reason') or ''}"
        append_turn(
            state,
            agent_id="coordinator",
            step="assign",
            status="ok",
            body=f"派单 → {agent_id}：{str(task_brief or '')[:400]}",
        )
        runner(state)
        if state.get("blocked"):
            reason = str(state.get("block_reason") or "blocked")
            return json.dumps({"status": "blocked", "message": reason}, ensure_ascii=False)
        return json.dumps({"status": "ok", "step": step}, ensure_ascii=False)

    return FunctionTool(
        deliver_step,
        name="deliver_step",
        description="执行本角色负责的交付步骤；task_brief 为协调者下发的完整任务说明。",
    )


def _leader_system_prompt(member_lines: str) -> str:
    return (
        "你是 Mino Bots 测试交付协调者。你必须使用 TeamAssign 工具按顺序派单，"
        "每次只派一个成员，等其返回结果后再派下一个。顺序固定：\n"
        "1. req-analyst\n2. req-reviewer\n3. mindmap\n4. case-author\n5. case-reviewer\n"
        "若某成员返回 status=blocked，停止派单并用中文总结阻塞原因。\n"
        "派单时 prompt 须包含：议题目标、讨论摘要、以及该步骤要做什么。\n"
        "成员列表：\n"
        f"{member_lines}\n"
        "全部完成后用简短中文总结交付结果。"
    )


async def _run_team_async(state: dict[str, Any], *, goal: str, discussion: str, memory: str) -> None:
    from agentscope.agent import Agent
    from agentscope.pipeline import TeamMember, TeamPipeline
    from agentscope.tool import Toolkit
    from agentscope.event import TextBlockDeltaEvent, ToolCallStartEvent

    from mino_nexus.services.collab_agentscope_llm import build_agentscope_chat_model

    model = build_agentscope_chat_model()
    specs = _bind_node_runners()
    members: list[TeamMember] = []
    member_desc = []
    for agent_id, desc, _step, runner in specs:
        toolkit = Toolkit()
        tool = _make_member_tool(state, agent_id, _step, runner)
        await toolkit.add_tool(tool)
        agent = Agent(
            name=agent_id,
            system_prompt=(
                f"你是{desc}。收到协调者任务后，必须调用 deliver_step，"
                "将协调者 prompt 全文填入 task_brief，不要自行编造结论。"
            ),
            model=model,
            toolkit=toolkit,
        )
        members.append(TeamMember(agent=agent, description=desc))
        member_desc.append(f"- {agent_id}: {desc}")

    leader = Agent(
        name="coordinator",
        system_prompt=_leader_system_prompt("\n".join(member_desc)),
        model=model,
    )
    team = TeamPipeline(leader, members, reset_members=True)

    brief = "\n".join(
        p
        for p in (
            f"议题目标：{goal}",
            f"长期记忆摘要：{memory[:2000]}" if memory else "",
            f"讨论上下文：{discussion[:6000]}" if discussion else "",
        )
        if p
    )
    append_turn(state, agent_id="coordinator", step="kickoff", status="running", body="TeamPipeline 启动")
    msg = _user_msg(brief or goal or "请按流程完成测试交付。")

    leader_text: dict[str, list[str]] = {}
    async for evt in team.reply_stream(msg):
        if isinstance(evt, ToolCallStartEvent) and evt.tool_call_name == "TeamAssign":
            append_turn(
                state,
                agent_id="coordinator",
                step="team_assign",
                status="running",
                body="协调者正在派单…",
            )
        if isinstance(evt, TextBlockDeltaEvent):
            leader_text.setdefault(evt.reply_id, []).append(evt.delta)

    summary = ""
    if leader_text:
        summary = "\n".join("".join(parts) for parts in leader_text.values()).strip()[:2000]
    if summary:
        append_turn(state, agent_id="coordinator", step="team_summary", status="ok", body=summary)
    append_turn(
        state,
        agent_id="coordinator",
        step="kickoff",
        status="ok" if not state.get("blocked") else "blocked",
        body="TeamPipeline 结束",
    )


def run_delivery_team_pipeline(
    *,
    topic_id: str,
    topic_title: str,
    goal: str,
    discussion: str = "",
    memory: str = "",
    app_id: str = "",
) -> dict[str, Any]:
    from mino_nexus.services.collab_langgraph import _node_emit_specs, run_delivery_pipeline

    state = new_delivery_state(
        topic_id=topic_id,
        topic_title=topic_title,
        goal=goal,
        discussion=discussion,
        memory=memory,
        app_id=app_id,
    )
    team_err = ""
    try:
        asyncio.run(_run_team_async(state, goal=goal, discussion=discussion, memory=memory))
    except Exception as exc:
        team_err = str(exc)
        SLog.e(TAG, f"team pipeline failed: {exc}")

    if not team_err and not state.get("blocked"):
        _node_emit_specs(state)
        state["engine"] = "agentscope_team"
        return state

    reason = team_err or str(state.get("block_reason") or "team_blocked")
    SLog.w(TAG, f"team pipeline unusable, fallback langgraph: {reason[:300]}")
    out = run_delivery_pipeline(
        topic_id=topic_id,
        topic_title=topic_title,
        goal=goal,
        discussion=discussion,
        memory=memory,
        app_id=app_id,
    )
    trace = list(out.get("pipeline_trace") or [])
    trace.append({"step": "team_pipeline", "status": "fallback_langgraph", "detail": reason[:500]})
    out["pipeline_trace"] = trace
    out["engine"] = "langgraph"
    return out


def augment_with_team_leader(state: dict[str, Any], *, goal: str, discussion: str) -> dict[str, Any]:
    """兼容旧 env 开关：在已有 state 上补一轮协调者摘要（LangGraph 路径用）。"""
    from mino_nexus.services.collab_agentscope_llm import build_agentscope_chat_model
    from agentscope.agent import Agent

    async def _leader_plan() -> str:
        leader = Agent(
            name="coordinator",
            system_prompt="你是测试交付协调者。用简短中文列出 3 步派单计划，不要调用工具。",
            model=build_agentscope_chat_model(),
        )
        msg = _user_msg(f"目标：{goal}\n讨论：{discussion[:4000]}")
        chunks: list[str] = []
        async for evt in leader.reply_stream(msg):
            from agentscope.event import TextBlockDeltaEvent

            if isinstance(evt, TextBlockDeltaEvent):
                chunks.append(evt.delta)
        return "".join(chunks)[:2000]

    plan = asyncio.run(_leader_plan())
    append_turn(state, agent_id="coordinator", step="team_plan", status="ok", body=plan or "协调者已生成派单计划")
    return state
