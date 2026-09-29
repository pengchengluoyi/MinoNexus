"""P6-P1：看图执行（agent-vision-exec）——按 success_criteria.milestones 执行一步。"""
from __future__ import annotations

from typing import Any

from mino_nexus.ai.planner import vision_exec_turn
from mino_nexus.ai.schemas import AgentDecision
from mino_nexus.loop.llm_step_context import step_scope_key
from mino_nexus.loop.milestone_orchestrator import focus_exec_menu_ids, success_criteria_for_exec_llm
from mino_nexus.loop.vision_flags import vision_exec_v1_enabled

__all__ = ["run_vision_exec_turn", "vision_exec_v1_enabled"]


def run_vision_exec_turn(
    *,
    cursor: Any,
    ctx: Any,
    writer: Any,
    shot: Any,
    steps: list[dict[str, Any]],
    inspect_slots: dict[str, str],
    provider_id: str = "",
    phase_tool_kinds: list[str] | None = None,
    session_block: str = "",
    goal: str = "",
    send_image: bool = True,
) -> AgentDecision:
    from mino_nexus.loop.vision_observation import apply_vision_observation_slots

    apply_vision_observation_slots(inspect_slots, cursor, writer=writer)
    case_step, phase = step_scope_key(cursor)
    has_img = bool(callable(getattr(shot, "has_image", None)) and shot.has_image())
    img_b64 = str(getattr(shot, "image_base64", "") or "") if send_image and has_img else ""
    decision = vision_exec_turn(
        run_context=ctx,
        cursor=cursor,
        success_criteria=success_criteria_for_exec_llm(cursor),
        width=int(getattr(shot, "width", None) or 1080),
        height=int(getattr(shot, "height", None) or 1920),
        image_base64=img_b64,
        image_mime=str(getattr(shot, "image_mime", "") or "image/png"),
        hierarchy_text=str(inspect_slots.get("hierarchy_text") or ""),
        provider_id=provider_id or None,
        phase=phase,
        tool_kinds=phase_tool_kinds,
        menu_ids=focus_exec_menu_ids(cursor),
        session_block=session_block,
        knowledge_hint=str(inspect_slots.get("knowledge_hint") or ""),
        knowledge_body=str(inspect_slots.get("knowledge_body") or ""),
        nav_assist=str(inspect_slots.get("nav_assist") or ""),
        doc_context=str(inspect_slots.get("doc_context") or ""),
    )
    if writer is not None:
        try:
            cap = str(getattr(getattr(decision, "action", None), "capability_id", "") or "")
            writer.append(
                "exec/vision",
                {
                    "phase": phase,
                    "case_step": case_step,
                    "capability_id": cap,
                    "status": str(getattr(decision, "status", "") or ""),
                    "thought": (str(getattr(decision, "thought", "") or ""))[:400],
                    "parse_warnings": list(getattr(decision, "parse_warnings", None) or []),
                },
            )
        except Exception:  # noqa: BLE001
            pass
    return decision
