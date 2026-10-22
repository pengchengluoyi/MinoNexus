"""同一回合的"先看后动"：视觉回看先验、里程碑程序钩子、输入焦点动作。

这是循环里唯一一段"决策已经产生、但要不要真的派发由上一张图的回看结果决定"的逻辑。
四个出口都必须在**派发之前**返回，否则会出现"没通过回看却已经点了屏幕"。

顺序不能换：观察先验 → wait/contract 停止 → reask/redo → 里程碑钩子 → 输入焦点守卫。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from mino_nexus.loop.execution.turn_log import _log_turn_end


@dataclass
class PriorFlow:
    cont: bool = False
    leave_case: bool = False
    leave: dict[str, Any] | None = None


def apply_turn_prior(
    *,
    ctx: Any,
    cursor: Any,
    decision: Any,
    writer: Any,
    cap_id: str,
    seq: int,
    in_prep: bool,
    in_check: bool,
    proxy: Any,
    scout_run_id: str,
    history: list[Any],
    cancel_check: Callable[[], bool] | None,
    rec: Callable[..., Any],
    emit: Callable[..., None],
    leave_blocked: Callable[..., dict[str, Any]],
    leave: Callable[..., dict[str, Any]],
) -> PriorFlow:
    from mino_nexus.loop.visual_review import apply_same_turn_prior

    prior_kind = apply_same_turn_prior(
        cursor,
        ctx,
        decision,
        writer=writer,
        in_prep=in_prep,
        in_check=in_check,
    )
    stop_prior = str(getattr(cursor, "semantic_stop", "") or "").strip()
    if prior_kind == "observation_stop":
        return PriorFlow(leave=leave_blocked(seq, cap_id, stop_prior, "review_observation_missing"))
    if prior_kind == "wait_stop":
        return PriorFlow(leave=leave_blocked(seq, cap_id, stop_prior, "review_wait_exhausted"))
    if prior_kind == "contract_stop":
        summary = (stop_prior or "LLM 回看输出契约错误，尚未验证")[:500]
        if writer:
            writer.append("contract/blocked", {
                "reason_code": str(getattr(cursor, "review_contract_reason", "") or "llm_prior_status_invalid"),
                "error_kind": "llm_output_contract_error",
                "capability_id": cap_id,
                "reason": summary,
            })
        emit(
            "think", thought=summary, summary=summary, step=seq, status="blocked",
            event_kind="output_contract_error",
            reason_code=str(getattr(cursor, "review_contract_reason", "") or "llm_prior_status_invalid"),
            execution_status="not_dispatched",
        )
        _log_turn_end(writer, cap="", status="blocked")
        return PriorFlow(leave=leave(status="blocked", summary=summary, reason_code="execution_contract_blocked"))
    if prior_kind == "stop" or (prior_kind == "redo" and stop_prior):
        return PriorFlow(leave=leave(status="fail", summary=(stop_prior or "回看未通过")[:500], reason_code="action_result_unverified"))
    if prior_kind == "reask":
        summary = (
            str(getattr(cursor, "correction_hint", "") or "").strip()
            or "上一张图必须给出 prior_status，只能是 pass 或 fail。"
        )
        emit(
            "think", thought=summary, summary=summary, step=seq, status="pending",
            event_kind="review_reask", execution_status="not_dispatched",
        )
        _log_turn_end(writer, cap="", status="pending")
        return PriorFlow(cont=True)
    if prior_kind == "redo" and not cap_id:
        emit(
            "think",
            thought="上一步未完成，但这次没有返回坐标，本回合不重复规划。",
            step=seq,
            status="pending",
        )
        _log_turn_end(writer, cap="", status="pending")
        return PriorFlow(cont=True)
    if prior_kind == "redo":
        setattr(cursor, "visual_turn_pending", True)
        if cap_id:
            sent = getattr(cursor, "visual_redo_sent", None)
            if not isinstance(sent, dict):
                sent = {}
            prior = getattr(cursor, "visual_prior", None)
            mid = str(prior.get("milestone_id") or "") if isinstance(prior, dict) else ""
            if mid:
                sent[mid] = True
                setattr(cursor, "visual_redo_sent", sent)
    elif prior_kind == "settled":
        from mino_nexus.loop.milestone_orchestrator import in_progress_milestone, program_hook_to_dispatch
        from mino_nexus.loop.milestones import read_state

        next_focus = in_progress_milestone(read_state(cursor))
        next_hook = program_hook_to_dispatch(next_focus)
        if next_hook in ("get_otp", "confirm_login_state") and not (cancel_check and cancel_check()):
            from mino_nexus.loop.flow_block_executor import try_dispatch_block_hook

            hook = try_dispatch_block_hook(
                proxy, ctx, cursor, run_id=scout_run_id, turn_seq=seq,
                app_id=str(getattr(ctx, "app_id", "") or ""),
                history_lines=list(history[-24:]), writer=writer,
            )
            if hook:
                hook_cap = str(hook.get("capability_id") or next_hook)
                hook_summary = str(hook.get("summary") or "")
                if hook.get("case_fail"):
                    if hook.get("status") in {"product_fail", "unverified", "env_blocked"}:
                        reason = str(hook.get("error_kind") or "login_transition_observation_error")
                        writer.append("login/completion_blocked", {"reason_code": reason, "status": hook["status"]})
                        return PriorFlow(leave=leave(status=hook["status"], summary=str(hook.get("summary") or reason), reason_code=reason))
                    if hook.get("status") == "blocked":
                        return PriorFlow(leave=leave_blocked(
                            seq, hook_cap, hook_summary,
                            str(hook.get("error_kind") or "program_hook_failed"),
                        ))
                    rec(seq, capability_id=hook_cap, status="fail", summary=hook_summary, thought=hook_summary)
                    emit("result", step=seq, capability_id=hook_cap, status="fail",
                         result_status="fail", summary=hook_summary, thought=hook_summary)
                    _log_turn_end(writer, cap=hook_cap, status="fail")
                    return PriorFlow(leave=leave(status="fail", summary=hook_summary, reason_code=str(hook.get("error_kind") or "program_hook_failed")))
                hook_status = "wait" if hook.get("defer") else str(hook.get("status") or "fail")
                rec(seq, capability_id=hook_cap, status=hook_status, summary=hook_summary, thought=hook_summary)
                emit("result", step=seq, capability_id=hook_cap, status=hook_status,
                     result_status=hook_status, summary=hook_summary, thought=hook_summary)
                if writer:
                    writer.append("review/program_handoff", {
                        "milestone_id": str((next_focus or {}).get("id") or ""),
                        "capability_id": hook_cap, "same_turn": True,
                        "placeholder_dispatched": False, "status": hook_status,
                    })
                if hook.get("ok"):
                    from mino_nexus.loop.step_phase_fsm import try_transition_after_tool_pass

                    transition = try_transition_after_tool_pass(
                        cursor, ctx, writer=writer, in_prep=in_prep, in_check=in_check,
                    )
                    if transition == "check_to_case_done":
                        return PriorFlow(leave_case=True)
                _log_turn_end(writer, cap=hook_cap, status=hook_status)
                return PriorFlow(cont=True)
        _log_turn_end(writer, cap=cap_id, status="continue")
        return PriorFlow(cont=True)
    from mino_nexus.loop.visual_review import guard_input_focus_action

    input_guard = guard_input_focus_action(cursor, ctx, cap_id, writer=writer)
    if input_guard == "stop":
        return PriorFlow(leave=leave(status="blocked", summary=str(cursor.semantic_stop)[:500], reason_code="execution_contract_blocked"))
    if input_guard == "reask":
        emit(
            "think", thought=str(cursor.correction_hint), step=seq, status="pending",
            event_kind="input_policy_reask", execution_status="not_dispatched",
        )
        _log_turn_end(writer, cap="", status="pending")
        return PriorFlow(cont=True)
    # 没有任何先验要拦：本回合照常派发（cont=False 才会走到派发）。
    # 这一句必须留在函数最外层 —— 曾经和 reask 分支的 return 一起被缩进进 if，
    # 正常路径直接掉出函数返回 None，调用方 `_prior.leave` 就 AttributeError
    # （2026-10-22 cr-4fe2598f3794）。修的时候别再写成 cont=True：那是
    # 「本轮结束」的意思，会让主循环一直 continue、一次也不派发（cr-19b79fcf6263 空转）。
    return PriorFlow()
