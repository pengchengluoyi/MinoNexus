"""协作交付流水线共享状态（LangGraph / AgentScope Team 共用）。"""
from __future__ import annotations

from typing import Any


def new_delivery_state(
    *,
    topic_id: str,
    topic_title: str,
    goal: str,
    discussion: str = "",
    memory: str = "",
    app_id: str = "",
) -> dict[str, Any]:
    return {
        "topic_id": topic_id,
        "topic_title": topic_title,
        "app_id": str(app_id or "").strip(),
        "goal": goal,
        "discussion": discussion,
        "memory": memory,
        "requirement": {},
        "blocked": False,
        "block_reason": "",
        "work_specs": [],
        "pipeline_trace": [],
        "agent_transcript": [],
        "team_events": [],
    }
