"""AgentScope 路径：TeamPipeline 为主，LangGraph 为回退。"""
from __future__ import annotations

import os
from typing import Any

from mino_nexus.core.log import SLog
from mino_nexus.services.collab_agentscope_llm import regression_provider_ready
from mino_nexus.services.collab_langgraph import run_delivery_pipeline

TAG = "CollabAgentScope"


def _langgraph_only() -> bool:
    return str(os.environ.get("MINO_COLLAB_AGENTS_SCOPE_LANGGRAPH_ONLY") or "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def run_delivery_agentscope(
    *,
    topic_id: str,
    topic_title: str,
    goal: str,
    discussion: str,
    memory: str,
    app_id: str,
) -> dict[str, Any]:
    """AgentScope TeamPipeline + qa_cover 工具链；无模型或失败时回退 LangGraph。"""
    if not _langgraph_only():
        ready, gate = regression_provider_ready()
        if ready:
            try:
                from mino_nexus.services.collab_agentscope_team import run_delivery_team_pipeline

                return run_delivery_team_pipeline(
                    topic_id=topic_id,
                    topic_title=topic_title,
                    goal=goal,
                    discussion=discussion,
                    memory=memory,
                    app_id=app_id,
                )
            except Exception as exc:
                SLog.w(TAG, f"team pipeline unavailable, fallback langgraph: {exc}")

    out = run_delivery_pipeline(
        topic_id=topic_id,
        topic_title=topic_title,
        goal=goal,
        discussion=discussion,
        memory=memory,
        app_id=app_id,
    )
    out["engine"] = "agentscope_fallback_langgraph"
    if str(os.environ.get("MINO_COLLAB_AGENTS_SCOPE_TEAM") or "").strip() in ("1", "true", "yes"):
        try:
            from mino_nexus.services.collab_agentscope_team import augment_with_team_leader

            out = augment_with_team_leader(out, goal=goal, discussion=discussion)
            out["engine"] = "agentscope_fallback_langgraph+leader"
        except Exception as exc:
            SLog.w(TAG, f"team leader skipped: {exc}")
    return out
