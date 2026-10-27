"""模型说"这一步做完了"之后的全部判定：能不能收尾。

这段是循环里最长的一条判定链，顺序是硬的：

1. 里程碑阶段下的 `signal_done` 前置校验（未满足就转阶段或记 blocked）
2. prep / check 两段各自的收尾规则
3. `require_do_work` 守卫：确实没做过实质动作时不许进 check
4. 资源闸门、视觉先验、探测命中判定
5. 都通过才 `enter_check()` 并结束本轮

**每一步都可能直接结束回合**，所以 14 个出口全部在派发之前。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from mino_nexus.loop.execution.facts import _effect_nodes
from mino_nexus.loop.execution.turn_log import _log_turn_end
from mino_nexus.loop.milestones import evaluate_milestones
from mino_nexus.loop.step_pointer import StepCursor, _expected_defers_to_check


@dataclass
class DoneFlow:
    cont: bool = False
    leave_case: bool = False
    leave: dict[str, Any] | None = None


def run_model_done(
    *,
    ctx: Any,
    cursor: Any,
    cur: Any,
    case: Any,
    decision: Any,
    guard_ctx: dict[str, Any],
    phase_cfg: dict[str, Any],
    history: list[Any],
    nav: Any,
    cap_id: str,
    case_id: str,
    seq: int,
    thought: str,
    thumb: Any,
    scout_run_id: str,
    writer: Any,
    in_prep: bool,
    in_check: bool,
    run_guards: Callable[..., Any],
    rec: Callable[..., Any],
    emit: Callable[..., None],
    pack: Callable[..., dict[str, Any]],
    leave: Callable[..., dict[str, Any]],
) -> DoneFlow:
    from mino_nexus.loop.step_intent import format_intent_progress
    from mino_nexus.loop import step_effect as step_effect_mod
    from mino_nexus.loop.step_phase_fsm import (
        handle_model_signal_done_blocked,
        milestone_phase_active,
    )

    if (
        milestone_phase_active()
        and isinstance(cursor, StepCursor)
    ):
        fsm_act, fsm_tr = handle_model_signal_done_blocked(
            cursor,
            ctx,
            writer=writer,
            in_prep=in_prep,
            in_check=in_check,
        )
        if fsm_act == "transition":
            summary = thought or (fsm_tr or "里程碑已齐，程序流转阶段")
            rec(
                seq,
                capability_id="signal_done",
                status="skipped",
                summary=summary,
                thought=thought,
                thumb=thumb,
            )
            emit(
                "result",
                thought=thought,
                step=seq,
                capability_id="signal_done",
                status="skipped",
                summary=summary,
                thumb=thumb,
            )
            _log_turn_end(writer, cap="signal_done", status="skipped")
            if fsm_tr == "check_to_case_done":
                return DoneFlow(leave_case=True)
            return DoneFlow(cont=True)
        if fsm_act == "failed_review":
            hint = "里程碑失败，下回合由看图规划复核"
            rec(
                seq,
                capability_id="signal_done",
                status="skipped",
                summary=hint,
                thought=thought,
                thumb=thumb,
            )
            emit(
                "result",
                thought=thought,
                step=seq,
                capability_id="signal_done",
                status="skipped",
                summary=hint,
                thumb=thumb,
            )
            _log_turn_end(writer, cap="signal_done", status="skipped")
            return DoneFlow(cont=True)
        if fsm_act == "pending":
            mv = evaluate_milestones(cursor, ctx)
            hint = mv.summary or "里程碑未齐，已忽略 signal_done"
            rec(
                seq,
                capability_id="signal_done",
                status="skipped",
                summary=hint,
                thought=thought,
                thumb=thumb,
            )
            emit(
                "result",
                thought=thought,
                step=seq,
                capability_id="signal_done",
                status="skipped",
                summary=hint,
                thumb=thumb,
            )
            _log_turn_end(writer, cap="signal_done", status="skipped")
            return DoneFlow(cont=True)
    if in_prep:
        from mino_nexus.resources.resource_preflight import prep_resource_gate_issues

        ctx.prep_signal_done_history = list(history[-48:])
        resource_issues = prep_resource_gate_issues(
            ctx, case, history_lines=list(history[-48:])
        )
        if resource_issues:
            gate_msg = resource_issues[0]
            if isinstance(cursor, StepCursor):
                cursor.correction_hint = gate_msg
                streak = int(getattr(cursor, "prep_resource_gate_streak", 0) or 0) + 1
                cursor.prep_resource_gate_streak = streak
                if streak >= 5:
                    return DoneFlow(leave=leave(
                        status="blocked",
                        summary=(
                            f"{gate_msg} "
                            "测试资源 Claim 多次未满足，任务已停止。"
                        ),
                     reason_code="environment_precondition_unmet"))
            rec(
                seq,
                capability_id="resource_claim_gate",
                status="skipped",
                summary=gate_msg,
                thought=thought,
                thumb=thumb,
            )
            emit(
                "result",
                thought=thought,
                step=seq,
                capability_id="resource_claim_gate",
                status="skipped",
                summary=gate_msg,
                thumb=thumb,
            )
            _log_turn_end(writer, cap="resource_claim_gate", status="skipped")
            return DoneFlow(cont=True)
        if isinstance(cursor, StepCursor):
            cursor.prep_resource_gate_streak = 0
        summary = thought or "前置检查完成，进入操作步骤"
        rec(seq, capability_id="signal_done", status="skipped",
                summary=summary, thought=thought, thumb=thumb)
        emit("result", thought=thought, step=seq, capability_id="signal_done",
             status="skipped", summary=summary, thumb=thumb)
        cursor.finish_prep()
        _log_turn_end(writer, cap="signal_done", status="skipped")
        return DoneFlow(cont=True)
    if in_check:
        if not cursor.step_checked:
            hint = "请先完成校验里程碑后再 signal_done"
            mv = evaluate_milestones(cursor, ctx)
            if mv.phase_complete:
                cursor.mark_checked()
            else:
                hint = mv.summary or hint
            if not cursor.step_checked:
                rec(
                    seq,
                    capability_id="signal_done",
                    status="skipped",
                    summary="校验尚未通过，继续本步",
                    thought=thought,
                    thumb=thumb,
                )
                emit(
                    "result",
                    thought=thought,
                    step=seq,
                    capability_id="signal_done",
                    status="skipped",
                    summary=hint,
                    thumb=thumb,
                )
                _log_turn_end(writer, cap="signal_done", status="skipped")
                return DoneFlow(cont=True)
        rec(seq, capability_id="signal_done", status="skipped",
                summary=thought or f"步骤 {cur.n} 校验结束", thought=thought, thumb=thumb)
        emit("result", thought=thought, step=seq, capability_id="signal_done",
             status="skipped", summary=f"步骤 {cur.n} 校验通过", thumb=thumb)
        if not cursor.advance():
            return DoneFlow(leave_case=True)
        _log_turn_end(writer, cap="signal_done", status="skipped")
        return DoneFlow(cont=True)
    do_work_reason = None
    if "require_do_work" in list(phase_cfg.get("guards") or []):
        from mino_nexus.loop import step_effect as step_effect_mod

        loc_done = dict(getattr(ctx, "nav_localized", None) or {})
        extra_vlm = decision.vlm_hierarchy if isinstance(decision.vlm_hierarchy, dict) else None
        probe_nodes_guard = _effect_nodes(nav, ctx, extra_vlm=extra_vlm)
        defer_do = _expected_defers_to_check(str(cur.expected or ""))
        probe_diag_guard = step_effect_mod.probe_diagnostic(
            str(cur.expected or ""),
            probe_nodes_guard,
            instruction=str(cur.instruction or ""),
            defer_expected_to_check=defer_do,
        )
        probe_hit_guard = bool(probe_diag_guard.get("hit"))
        ctx._last_probe_diag = probe_diag_guard
        loc_hit_guard = step_effect_mod.localized_matches_step(
            loc_done,
            instruction=str(cur.instruction or ""),
            expected="" if defer_do else str(cur.expected or ""),
        )
        step_effect_mod.maybe_mark_deferred_nav_tab(
            cursor,
            instruction=str(cur.instruction or ""),
            expected=str(cur.expected or ""),
            localized=loc_done,
            nodes=probe_nodes_guard,
            thought=thought,
            coerce_done=bool(
                str(decision.status or "") == "done"
                or str(cap_id or "") == "signal_done"
            ),
        )
        do_work_reason = run_guards(
            ["require_do_work"],
            {
                **guard_ctx,
                "intent": "signal_done",
                "step_goal_met": bool(probe_hit_guard or loc_hit_guard),
            },
        )
    if do_work_reason:
        if isinstance(cursor, StepCursor):
            from mino_nexus.loop.step_intent import format_intent_progress

            streak = int(getattr(cursor, "require_do_work_streak", 0) or 0) + 1
            cursor.require_do_work_streak = streak
            if streak >= 8:
                if probe_hit_guard or loc_hit_guard:
                    do_work_reason = None
                else:
                    cur_n = int(cur.n) if cur else 0
                    return DoneFlow(leave=leave(
                        status="fail",
                        summary=(
                            f"步骤 {cur_n} 连续 {streak} 次无法收工："
                            f"{str(do_work_reason or '')[:160]}"
                        ),
                     reason_code="required_operation_unverified"))
        if do_work_reason and isinstance(cursor, StepCursor):
            from mino_nexus.loop.step_intent import format_intent_progress

            prog = format_intent_progress(
                instruction=str(cur.instruction or ""),
                intents_done=cursor.step_intents_done,
            )
            mode = str(probe_diag_guard.get("mode") or "")
            extra = (
                f" 探针={mode} hit={probe_hit_guard}。"
                if mode
                else ""
            )
            cursor.correction_hint = (
                f"{do_work_reason}"
                + (f" {prog}" if prog else "")
                + extra
                + " 若屏上已达成本步目标，请补全未完成意图对应操作后再 signal_done。"
            )
        if do_work_reason:
            if writer:
                from mino_nexus.loop.observe.ops_guard_diag import enrich_guard_block_payload

                writer.append(
                    "guard/block",
                    enrich_guard_block_payload(
                        {
                            "capability_id": "signal_done",
                            "guard_id": "require_do_work",
                            "reason": do_work_reason,
                            "dispatch_gate_code": "require_do_work",
                        },
                        guard_ctx=guard_ctx,
                        cur=cur,
                        probe_diag=probe_diag_guard,
                    ),
                )
            rec(
                seq,
                capability_id="require_do_work",
                status="skipped",
                summary=do_work_reason,
                thought=thought,
                thumb=thumb,
            )
            emit(
                "result",
                thought=thought,
                step=seq,
                capability_id="require_do_work",
                status="skipped",
                summary=do_work_reason,
                thumb=thumb,
            )
            _log_turn_end(writer, cap="require_do_work", status="skipped")
            return DoneFlow(cont=True)
    from mino_nexus.loop import step_effect as step_effect_mod
    from mino_nexus.navigation.atlas import nav_telemetry

    extra_vlm_done = decision.vlm_hierarchy if isinstance(decision.vlm_hierarchy, dict) else None
    probe_nodes_done = _effect_nodes(nav, ctx, extra_vlm=extra_vlm_done)
    probe_hit = False
    kw_done: list[str] = []
    defer_done = _expected_defers_to_check(str(cur.expected or ""))
    if str(cur.expected or "").strip() or str(cur.instruction or "").strip():
        probe_hit, kw_done = step_effect_mod.probe_expected_for_do(
            str(cur.expected or ""),
            probe_nodes_done,
            instruction=str(cur.instruction or ""),
            defer_expected_to_check=defer_done,
        )
    nav_telemetry.step_effect(
        turn_id=seq,
        run_id=scout_run_id,
        case_id=case_id,
        step_n=cur.n,
        probe_hit=probe_hit,
        model_done=True,
        keywords=kw_done,
    )
    rec(seq, capability_id="signal_done", status="skipped",
            summary=thought or f"步骤 {cur.n} 操作结束，进入校验", thought=thought, thumb=thumb)
    emit("result", thought=thought, step=seq, capability_id="signal_done",
         status="skipped", summary=f"步骤 {cur.n} 操作结束，进入校验", thumb=thumb)
    cursor.enter_check()
    _log_turn_end(writer, cap="signal_done", status="skipped")
    return DoneFlow(cont=True)
    return DoneFlow()
