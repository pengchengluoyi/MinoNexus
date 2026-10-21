"""P6-P1：看图执行（agent-vision-exec）——按 success_criteria.milestones 执行一步。"""
from __future__ import annotations

from typing import Any

from mino_nexus.ai.planner import vision_exec_turn
from mino_nexus.ai.schemas import AgentDecision
from mino_nexus.loop.llm_step_context import step_scope_key
from mino_nexus.loop.milestone_orchestrator import focus_exec_menu_ids, success_criteria_for_exec_llm
from mino_nexus.loop.vision_flags import execution_phase_active

__all__ = ["run_vision_exec_turn", "execution_phase_active"]


def visual_execution_block_reason(shot: Any, inspect_slots: dict[str, str], *, send_image: bool, ctx: Any = None) -> str:
    """视觉执行和绑定重放共用准入；程序动作另走原有路径。"""
    if not send_image:
        if str(inspect_slots.get("hierarchy_text") or "").strip():
            return ""
        return "foreground_image_withheld" if getattr(ctx, "app_foreground", "") == "no" else "model_image_withheld"
    from mino_nexus.loop.screen_capture import analyze_shot
    facts = analyze_shot(shot)
    if not str(inspect_slots.get("hierarchy_text") or "").strip():
        if facts.get("capture_ok") != "yes":
            return "screenshot_decode_failed" if facts.get("capture_error") == "screenshot_decode_failed" else "screenshot_missing"
        if facts.get("capture_black") == "yes":
            return "screenshot_black"
    return ""


def blocked_visual_decision(reason: str, writer: Any = None, *, facts: dict[str, Any] | None = None) -> AgentDecision:
    facts = dict(facts or {})
    message = {
        "foreground_image_withheld": "前台观察明确为异应用，当前截图因送图策略未进入执行模型。",
        "model_image_withheld": "当前截图未进入执行模型，所选执行通道也没有提供层级文本。",
        "screenshot_missing": "当前回合未取得可用截图。",
        "screenshot_decode_failed": "当前截图已采集，但图像解码失败。",
        "screenshot_black": "当前截图近乎全黑，无法完成视觉动作或回看；不能仅凭此断言设备锁屏。",
    }.get(reason, "当前模型输入缺少可用观察，不能执行或判定业务结果。")
    nodes = int(facts.get("source_node_count") or 0)
    if nodes:
        message += f"已采集 {nodes} 个结构节点；当前视觉通道未将这些节点作为模型输入。"
    message += "已停止本次视觉执行，结果未核验。"
    if writer is not None:
        writer.append("exec/observation_blocked", {"reason": reason, "reason_code": reason,
            "model_called": False, **facts})
    return AgentDecision(status="give_up", thought=message, error_kind=reason,
                         parse_warnings=["visual_observation_unavailable"])


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
    exec_mode: str = "",
) -> AgentDecision:
    from mino_nexus.action_space.scheme import action_scheme, execution_scheme
    from mino_nexus.loop.login_evidence_navigation import active_evidence_navigation, restore_execution_reference

    inspect_slots = dict(inspect_slots)
    source_hierarchy_chars = len(str(inspect_slots.get("hierarchy_text") or "").strip())
    reference = restore_execution_reference(cursor, ctx)
    from mino_nexus.loop.prep_program_plan import active_reauthentication_navigation
    current_navigation = bool(reference) or active_reauthentication_navigation(cursor) or active_evidence_navigation(cursor)
    if execution_scheme(ctx) == "visual" and not (current_navigation and action_scheme(ctx) == "dom_preferred"):
        # 截图兜底以当前图为观察合同，不再把低置信 DOM 页名/控件提示喂给执行模型。
        inspect_slots["hierarchy_text"] = ""
        inspect_slots["nav_assist"] = ""
    import hashlib
    from mino_nexus.loop.screen_capture import analyze_shot
    raw_image = str(getattr(shot, "image_base64", "") or "")
    facts = {**analyze_shot(shot), "source_node_count": len(getattr(ctx, "nav_hierarchy_nodes", None) or []),
             "source_hierarchy_chars": source_hierarchy_chars,
             "model_hierarchy_chars": len(str(inspect_slots.get("hierarchy_text") or "").strip()),
             "image_in_model_input": False, "hierarchy_in_model_input": False,
             "app_foreground": str(getattr(ctx, "app_foreground", "") or "unknown"),
             "raw_image_hash": hashlib.sha256(raw_image.encode()).hexdigest() if raw_image else "",
             "width": int(getattr(shot, "width", 0) or 0), "height": int(getattr(shot, "height", 0) or 0)}
    reason = visual_execution_block_reason(shot, inspect_slots, send_image=send_image, ctx=ctx)
    if reason:
        return blocked_visual_decision(reason, writer, facts=facts)
    from mino_nexus.loop.vision_observation import apply_vision_observation_slots
    from mino_nexus.loop.screen_capture import shot_usable

    apply_vision_observation_slots(inspect_slots, cursor, writer=writer)
    setattr(cursor, "run_context", ctx)
    case_step, phase = step_scope_key(cursor)
    has_img = shot_usable(shot)
    img_b64 = str(getattr(shot, "image_base64", "") or "") if send_image and has_img else ""
    if not img_b64:
        reference = {}
    if writer is not None:
        writer.append("exec/observation", {**facts, "image_in_model_input": bool(img_b64),
            "hierarchy_in_model_input": bool(str(inspect_slots.get("hierarchy_text") or "").strip()),
            "model_requested": True})
    decision = vision_exec_turn(
        run_context=ctx,
        cursor=cursor,
        success_criteria=success_criteria_for_exec_llm(cursor),
        width=int(getattr(shot, "width", None) or 1080),
        height=int(getattr(shot, "height", None) or 1920),
        image_base64=img_b64,
        image_mime=str(getattr(shot, "image_mime", "") or "image/png"),
        reference_image_base64=reference.get("image_base64", ""),
        reference_image_mime=reference.get("image_mime", "image/png"),
        hierarchy_text=str(inspect_slots.get("hierarchy_text") or ""),
        provider_id=provider_id or None,
        phase=phase,
        tool_kinds=phase_tool_kinds,
        menu_ids=focus_exec_menu_ids(cursor, ctx),
        session_block=session_block,
        knowledge_hint=str(inspect_slots.get("knowledge_hint") or ""),
        knowledge_body=str(inspect_slots.get("knowledge_body") or ""),
        nav_assist=str(inspect_slots.get("nav_assist") or ""),
        doc_context=str(inspect_slots.get("doc_context") or ""),
    )
    if writer is not None:
        try:
            cap = str(getattr(getattr(decision, "action", None), "capability_id", "") or "")
            mode = str(exec_mode or "").strip()
            payload = {
                "phase": phase,
                "case_step": case_step,
                "capability_id": cap,
                "status": str(getattr(decision, "status", "") or ""),
                "thought": (str(getattr(decision, "thought", "") or ""))[:400],
                "prior_status": str(getattr(decision, "prior_status", "") or ""),
                "parse_warnings": list(getattr(decision, "parse_warnings", None) or []),
            }
            if mode:
                payload["exec_mode"] = mode
            writer.append("exec/vision", payload)
            if mode == "heal_skip":
                writer.append("exec/heal_skip", dict(payload))
            elif mode == "interrupt":
                writer.append("exec/interrupt", dict(payload))
        except Exception:  # noqa: BLE001
            pass
    return decision
