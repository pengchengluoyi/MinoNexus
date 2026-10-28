"""校验阶段的程序化判定：脚本化检查与里程碑检查。

两个入口殊途同归：脚本化检查跑预置断言，里程碑检查按 success_criteria 逐条核对。
两条路都可能直接产出下一步决策（`assert_visual`）、直接结束用例、或什么都不做交回模型。

块内有一个内层 `for`（遍历已知行做匹配），它的 3 个 `break` 与外层 9 个 `continue`
不要混为一谈：`break` 属于内层循环，随循环一起搬；`continue` 属于主循环，改写为出口。
块内还有两个嵌套函数（`_decide` / `_decide_gated`），它们的 `return` 属于自己，
**绝对不能**改写成 `CheckFlow`——外提时踩过这个坑。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from mino_nexus.action_space.scheme import execution_scheme as action_scheme
from mino_nexus.ai.planner import decide_next_action
from mino_nexus.ai.schemas import AgentAction, AgentDecision
from mino_nexus.loop.execution.turn_log import _log_turn_end
from mino_nexus.loop.inspections import expand_named_knowledge, llm_session_block
from mino_nexus.loop.milestones import (
    apply_decision_milestones,
    apply_step_outcome,
    evaluate_milestones,
)
from mino_nexus.loop.program_device_fallback import is_vision_fallback
from mino_nexus.loop.step_pointer import StepCursor, enrich_assert_expectation


@dataclass
class CheckFlow:
    cont: bool = False
    leave_case: bool = False
    leave: dict[str, Any] | None = None
    decision: Any = None
    shot: Any = None
    thumb: Any = None


def run_check_phase(
    *,
    ctx: Any,
    cursor: Any,
    cur: Any,
    resolved_case: Any,
    history: list[Any],
    inspect_slots: dict[str, Any],
    phase_tool_kinds: list[str],
    steps: Any,
    proxy: Any,
    seq: int,
    scout_run_id: str,
    provider_id: str,
    writer: Any,
    shot: Any,
    thumb: Any,
    auto_knowledge_body: str,
    know_rows: Any,
    scripted_check: bool,
    use_milestone_check: bool,
    check_needs_visual: bool,
    rec: Callable[..., Any],
    emit: Callable[..., None],
    leave_blocked: Callable[..., dict[str, Any]],
    leave: Callable[..., dict[str, Any]],
) -> CheckFlow:
    from mino_nexus.action_space.scheme import use_visual_fallback
    from mino_nexus.loop.milestones import first_pending_hook_milestone, read_state
    from mino_nexus.loop.step_phase_fsm import try_transition_after_tool_pass
    from mino_nexus.action_space.scheme import use_visual_fallback
    decision: Any = None
    if scripted_check or (
        use_milestone_check
        and check_needs_visual
        and not cursor.step_checked
    ):
        exp = enrich_assert_expectation(cur.instruction, cur.expected)
        assert_params: dict[str, Any] = {"expectation": exp}
        ctx_bits = [auto_knowledge_body, inspect_slots.get("doc_context") or ""]
        from mino_nexus.loop.observe.session_persist import assert_session_context

        sess_ctx = assert_session_context(
            ctx, instruction=str(cur.instruction or ""), expected=str(cur.expected or "")
        )
        if sess_ctx:
            ctx_bits.append(sess_ctx)
        merged_ctx = "\n\n".join(b for b in ctx_bits if b).strip()
        if merged_ctx:
            assert_params["knowledge_context"] = merged_ctx
        from mino_nexus.loop.vision_assert import (
            run_vision_assert_if_enabled,
            assertion_phase_active,
        )

        vision_assert_handled = False
        va = None
        from mino_nexus.action_space.scheme import execution_scheme as _assert_scheme
        from mino_nexus.loop.structured_check import checkpoints_need_structured, run_dom_structured_check
        from mino_nexus.loop.vision_assert import checkpoints_from_milestones as _cps_from_ms

        _cps = _cps_from_ms(cursor)
        dom_assert = checkpoints_need_structured(_cps) and _assert_scheme(ctx) == "dom"
        if dom_assert:
            from mino_nexus.loop.check_scope import verify_dom_scope
            scope_result = verify_dom_scope(cursor, ctx, shot, provider_id=str(provider_id or "")) if resolved_case is not None else None
            if scope_result is not None and scope_result.error_kind:
                va = scope_result
            else:
                va = run_dom_structured_check(cursor, ctx, _cps, writer=writer, provider_id=str(provider_id or ""))
                if scope_result is not None:
                    va.observation_scope = scope_result.observation_scope
            if va.error_kind == "dom_check_unavailable":
                from mino_nexus.action_space.scheme import use_visual_fallback
                use_visual_fallback(ctx, reason="assertion_not_observable_in_structure", writer=writer)
                dom_assert = False
                va = None
        if assertion_phase_active() or dom_assert:
            from mino_nexus.loop.channel_observation import capture_decision_frame

            seen_frames: set[str] = set()
            if not dom_assert:
                for attempt in range(3):
                    if (
                        attempt == 0
                        and shot is not None
                        and getattr(shot, "has_image", lambda: False)()
                    ):
                        fresh_shot, fresh_thumb = shot, thumb
                    else:
                        fresh_shot, fresh_thumb = capture_decision_frame(proxy, ctx)
                    image_data = str(getattr(fresh_shot, "image_base64", "") or "")
                    import hashlib

                    frame_hash = hashlib.sha256(image_data.encode("ascii", errors="ignore")).hexdigest() if image_data else ""
                    if attempt and (not frame_hash or frame_hash in seen_frames):
                        if writer:
                            writer.append("check/retry_skipped", {
                                "reason": "unchanged_observation", "attempt": attempt + 1,
                                "raw_image_hash": frame_hash[:20],
                            })
                        break
                    if frame_hash:
                        seen_frames.add(frame_hash)
                    if fresh_shot is not None and getattr(fresh_shot, "has_image", lambda: False)():
                        shot = fresh_shot
                        thumb = fresh_thumb or thumb
                    if attempt:
                        setattr(cursor, "vision_assert_done", False)
                    got = run_vision_assert_if_enabled(
                        cursor=cursor,
                        writer=writer,
                        shot=shot,
                        expected=exp,
                        context_block=merged_ctx,
                        provider_id=str(provider_id or ""),
                        run_context=ctx,
                    )
                    if got is None:
                        break
                    va = got
                    if va.passed or va.error_kind or va.parse_warnings:
                        break
                    if writer:
                        writer.append(
                            "check/retry",
                            {
                                "attempt": attempt + 1,
                                "passed": False,
                                "summary": str(va.ai_reasoning or va.evidence or "")[:240],
                            },
                        )
            if va is not None:
                scope_bad = str(va.error_kind) in ("check_scope_mismatch", "check_observation_channel_mismatch")
                if scope_bad:
                    # 必须在 record_check 之前处理：record_check 会把 scope 失败直接抛成
                    # ContractError（resolved_case.py:276），那一步就再没机会退回操作阶段
                    # （cr-1dd866f93044 的 api-1-217 就是 check/scope 之后立刻 contract/blocked）。
                    from mino_nexus.loop.scope_bounce import bounce_to_do_after_scope_mismatch

                    if resolved_case is not None:
                        writer.append("check/scope", {
                            "case_step": cur.n,
                            "observation_scope": getattr(va, "observation_scope", {}),
                            "reason_code": va.error_kind, "business_verdict": "unknown"})
                    if bounce_to_do_after_scope_mismatch(
                        ctx,
                        cursor,
                        case_step=int(getattr(cur, "n", 0) or 0),
                        evidence=str(getattr(va, "evidence", "") or va.ai_reasoning or ""),
                        writer=writer,
                    ):
                        _log_turn_end(writer, cap="assert_visual", status="blocked")
                        return CheckFlow(cont=True)
                if resolved_case is not None and not scope_bad:
                    from mino_nexus.loop.resolved_case import record_check
                    writer.append("check/scope", {"case_step": cur.n,
                        "observation_scope": getattr(va, "observation_scope", {}),
                        "reason_code": va.error_kind, "business_verdict": "unknown" if va.error_kind else "scoped"})
                    record_check(cursor, _cps, va, writer=writer)
                if va.passed:
                    from mino_nexus.loop.milestones import apply_assert_checkpoint_results

                    if not apply_assert_checkpoint_results(cursor, va, writer=writer):
                        va.passed = False
                        va.ai_reasoning = "检查点缺少完整且有效的逐项证据"
                        va.error_kind = "checkpoint_application_invalid"
                vision_assert_handled = True
                cursor.saw_assert = True
                via = "DOM 校验" if str(getattr(va, "evidence", "") or "") == "dom" else "看图校验"
                if va.error_kind or va.parse_warnings:
                    reason = "；".join(str(item) for item in va.parse_warnings) or va.ai_reasoning
                    blocked_summary = f"步骤 {cur.n} 校验未完成，不能判定页面预期失败：{reason}"
                    rec(seq, capability_id="assert_visual", status="blocked",
                        summary=blocked_summary[:400], thought=f"{via}无法完成", thumb=thumb)
                    emit("result", thought=f"{via}无法完成", step=seq,
                         capability_id="assert_visual", status="blocked", result_status="blocked",
                         summary=blocked_summary[:400], thumb=thumb)
                    if writer:
                        writer.append("check/blocked", {
                            "error_kind": va.error_kind or "assert_output_invalid",
                            "reason": reason[:400], "business_verdict": "unknown",
                        })
                    _log_turn_end(writer, cap="assert_visual", status="blocked")
                    return CheckFlow(leave=leave(status="blocked", summary=blocked_summary, reason_code=va.error_kind or "check_evidence_incomplete"))
                if va.passed:
                    pass_summary = (
                        f"{via}通过：{va.ai_reasoning or cur.expected}"
                    ).strip()
                    rec(
                        seq,
                        capability_id="assert_visual",
                        status="pass",
                        summary=pass_summary[:400],
                        thought=f"{via}通过",
                        thumb=thumb,
                    )
                    emit(
                        "result",
                        thought=f"{via}通过",
                        step=seq,
                        capability_id="assert_visual",
                        status="pass",
                        result_status="pass",
                        summary=pass_summary[:400],
                        thumb=thumb,
                    )
                    cursor.mark_checked()
                    evaluate_milestones(cursor, ctx)
                    from mino_nexus.loop.step_phase_fsm import try_transition_after_tool_pass

                    fsm_tr = try_transition_after_tool_pass(
                        cursor,
                        ctx,
                        writer=writer,
                        in_prep=False,
                        in_check=True,
                    )
                    _log_turn_end(writer, cap="assert_visual", status="pass")
                    if fsm_tr == "check_to_case_done":
                        return CheckFlow(leave_case=True)
                    return CheckFlow(cont=True)
                else:
                    fail_summary = (
                        f"步骤 {cur.n} 预期未成立：{cur.expected}。"
                        f"{va.ai_reasoning or va.evidence or ''}"
                    ).strip()
                    rec(
                        seq,
                        capability_id="assert_visual",
                        status="fail",
                        summary=fail_summary[:400],
                        thought=(va.ai_reasoning or "")[:300],
                        thumb=thumb,
                    )
                    emit(
                        "result",
                        thought=f"{via}未通过",
                        step=seq,
                        capability_id="assert_visual",
                        status="fail",
                        result_status="fail",
                        summary=fail_summary[:400],
                        thumb=thumb,
                    )
                    return CheckFlow(leave=leave(status="fail", summary=fail_summary, reason_code="required_assertion_failed"))
        if not vision_assert_handled:
            decision = AgentDecision(
                status="continue",
                thought=f"校验步骤 {cur.n}：{cur.expected}",
                action=AgentAction(
                    capability_id="assert_visual",
                    params=assert_params,
                ),
            )
    else:
        from mino_nexus.loop.fuse.llm_screenshot_gate import (
            WITHHOLD_HINT,
            apply_allow_foreign_flag,
            clear_transient_llm_image_gate,
            parse_allow_foreign_flag,
            should_withhold_llm_image,
        )

        cp_block = cursor.prompt_block()
        from mino_nexus.loop.fuse.llm_screenshot_gate import foreign_foreground_active

        if foreign_foreground_active(ctx) and should_withhold_llm_image(ctx, shot=shot):
            cp_block = f"{cp_block}\n{WITHHOLD_HINT}"

        from mino_nexus.loop.llm_step_context import (
            history_block_scoped,
            step_scope_key,
            success_criteria_for_llm,
        )

        _cs, _ph = step_scope_key(cursor)

        def _decide(*, send_image: bool) -> AgentDecision:
            from mino_nexus.loop.vision_exec import run_vision_exec_turn, execution_phase_active

            img_b64 = shot.image_base64 if send_image and shot.has_image() else ""
            sess_blk = (
                ""
                if cursor.phase == "prep"
                else llm_session_block(ctx, inspect_slots.get("session_block") or "")
            )
            if execution_phase_active():
                return run_vision_exec_turn(
                    cursor=cursor,
                    ctx=ctx,
                    writer=writer,
                    shot=shot,
                    steps=steps,
                    inspect_slots=inspect_slots,
                    provider_id=str(provider_id or ""),
                    phase_tool_kinds=phase_tool_kinds or None,
                    session_block=sess_blk,
                    goal=cursor.decide_goal(),
                    send_image=send_image,
                )
            return decide_next_action(
                goal=cursor.decide_goal(),
                checkpoints_block=cp_block,
                run_context=ctx,
                history_block=history_block_scoped(
                    steps, case_step=_cs, phase=_ph
                ),
                width=shot.width or 1080,
                height=shot.height or 1920,
                image_base64=img_b64,
                image_mime=shot.image_mime or "image/png",
                success_criteria=success_criteria_for_llm(cursor),
                provider_id=provider_id or None,
                phase=cursor.phase,
                tool_kinds=phase_tool_kinds or None,
                session_block=sess_blk,
                hierarchy_text=inspect_slots.get("hierarchy_text") or "",
                knowledge_hint=inspect_slots.get("knowledge_hint") or "",
                knowledge_body=inspect_slots.get("knowledge_body") or "",
                nav_assist=inspect_slots.get("nav_assist") or "",
                doc_context=inspect_slots.get("doc_context") or "",
            )

        def _decide_gated() -> AgentDecision:
            send_first = not should_withhold_llm_image(ctx, shot=shot)
            dec = _decide(send_image=send_first)
            apply_allow_foreign_flag(ctx, dec)
            if not send_first and foreign_foreground_active(ctx) and parse_allow_foreign_flag(dec) is True:
                dec = _decide(send_image=True)
                apply_allow_foreign_flag(ctx, dec)
            return dec

        from mino_nexus.loop.vision_flags import planning_phase_active as _vision_plan_on
        from mino_nexus.loop.vision_exec import execution_phase_active

        decision: AgentDecision | None = None
        from mino_nexus.action_space.scheme import execution_scheme as action_scheme

        if cursor.phase == "prep" and isinstance(cursor, StepCursor):
            from mino_nexus.catalog.skill_channel import is_program_caller
            from mino_nexus.loop.flow_block_executor import try_dispatch_block_hook
            from mino_nexus.loop.milestones import first_pending_hook_milestone, read_state

            pending_hook = first_pending_hook_milestone(read_state(cursor))
            hook_cap = str((pending_hook or {}).get("hook_cap") or "").strip()
            # 程序钩子在看图之前派。前置和操作都走 try_dispatch_block_hook。
            # 这一轮已经决定交给看图时，不要再派一次并把「转看图」记成失败。
            handed_to_vision = isinstance(
                getattr(cursor, "program_vision_fallback", None), dict
            ) and bool(getattr(cursor, "program_vision_fallback", None))
            if is_program_caller(hook_cap) and not handed_to_vision:
                prep_macro = try_dispatch_block_hook(
                    proxy,
                    ctx,
                    cursor,
                    run_id=scout_run_id,
                    turn_seq=seq,
                    app_id=str(getattr(ctx, "app_id", "") or ""),
                    history_lines=list(history[-24:]),
                    writer=writer,
                )
                if prep_macro and is_vision_fallback(prep_macro):
                    from mino_nexus.loop.program_device_fallback import (
                        activate_program_vision_fallback,
                        note_vision_fallback_streak,
                    )

                    activate_program_vision_fallback(cursor, writer, prep_macro)
                    if note_vision_fallback_streak(cursor, prep_macro) >= 3:
                        last_visual = getattr(cursor, "vision_fallback_last_execution", None)
                        if isinstance(last_visual, dict) and last_visual.get("has_coordinates"):
                            fb_sum = (
                                "程序定位连续 3 次未命中；视觉动作已提供坐标，"
                                f"最近设备执行状态 {last_visual.get('status') or '未知'}。"
                            )
                        else:
                            fb_sum = "程序定位连续 3 次未命中；看图兜底未能完成这一步，已停止。"
                        rec(
                            seq,
                            capability_id=str(prep_macro.get("capability_id") or hook_cap),
                            status="fail",
                            summary=fb_sum,
                            thought=fb_sum,
                            thumb=thumb,
                        )
                        emit(
                            "result",
                            thought=fb_sum,
                            step=seq,
                            capability_id=str(prep_macro.get("capability_id") or hook_cap),
                            status="fail",
                            result_status="fail",
                            summary=fb_sum,
                            thumb=thumb,
                        )
                        return CheckFlow(leave=leave(status="fail", summary=fb_sum, reason_code="program_vision_fallback_exhausted"))
                    prep_macro = None
                if prep_macro:
                    prep_cap = str(prep_macro.get("capability_id") or hook_cap)
                    prep_sum = str(prep_macro.get("summary") or "")
                    if prep_macro.get("case_fail"):
                        if prep_macro.get("status") in {"product_fail", "unverified", "env_blocked"}:
                            reason = str(prep_macro.get("error_kind") or "login_transition_observation_error")
                            writer.append("login/completion_blocked", {"reason_code": reason, "status": prep_macro["status"]})
                            return CheckFlow(leave=leave(status=prep_macro["status"], summary=str(prep_macro.get("summary") or reason), reason_code=reason))
                        if prep_macro.get("status") == "blocked":
                            return CheckFlow(leave=leave_blocked(
                                seq, prep_cap, prep_sum,
                                str(prep_macro.get("error_kind") or "program_hook_failed"),
                            ))
                        rec(
                            seq,
                            capability_id=prep_cap,
                            status="fail",
                            summary=prep_sum[:400],
                            thought=prep_sum[:300],
                            thumb=str(prep_macro.get("thumb") or thumb),
                        )
                        emit(
                            "result",
                            thought=prep_sum[:300],
                            step=seq,
                            capability_id=prep_cap,
                            status="fail",
                            result_status="fail",
                            summary=prep_sum[:400],
                            thumb=str(prep_macro.get("thumb") or thumb),
                        )
                        return CheckFlow(leave=leave(status="fail", summary=prep_sum or "登录未成功", reason_code=str(prep_macro.get("error_kind") or "program_hook_failed")))
                    if prep_macro.get("defer"):
                        rec(
                            seq,
                            capability_id=prep_cap,
                            status="wait",
                            summary=prep_sum[:400],
                            thought=prep_sum[:300],
                            thumb=str(prep_macro.get("thumb") or thumb),
                        )
                        emit(
                            "result",
                            thought=prep_sum[:300],
                            step=seq,
                            capability_id=prep_cap,
                            status="wait",
                            result_status="wait",
                            summary=prep_sum[:400],
                            thumb=str(prep_macro.get("thumb") or thumb),
                        )
                        _log_turn_end(writer, cap=prep_cap, status="wait")
                        return CheckFlow(cont=True)
                    prep_st = "pass" if prep_macro.get("ok") else "fail"
                    rec(
                        seq,
                        capability_id=prep_cap,
                        status=prep_st,
                        summary=prep_sum[:400],
                        thought=prep_sum[:300],
                        thumb=str(prep_macro.get("thumb") or thumb),
                        executor_used="internal",
                    )
                    emit(
                        "result",
                        thought=prep_sum[:300],
                        step=seq,
                        capability_id=prep_cap,
                        status=prep_st,
                        summary=prep_sum[:400],
                        thumb=str(prep_macro.get("thumb") or thumb),
                    )
                    _log_turn_end(writer, cap=prep_cap, status=prep_st)
                    if prep_macro.get("ok"):
                        # 程序钩子把 launch_app 跑通了就是「应用已启动」。这里必须落标志，
                        # 否则 preparation_gate 会在前置出口判 prepare_launch_unverified。
                        if prep_cap in ("launch_app", "open_app", "open_url"):
                            ctx.app_launch_confirmed = True
                        from mino_nexus.loop.step_phase_fsm import try_transition_after_tool_pass

                        fsm_tr = try_transition_after_tool_pass(
                            cursor,
                            ctx,
                            writer=writer,
                            in_prep=True,
                            in_check=False,
                        )
                        if fsm_tr == "check_to_case_done":
                            return CheckFlow(leave_case=True)
                    return CheckFlow(cont=True)

        from mino_nexus.loop.milestone_orchestrator import in_progress_milestone
        from mino_nexus.loop.milestones import read_state as _read_ms

        _focus = in_progress_milestone(_read_ms(cursor))
        if (
            isinstance(_focus, dict)
            and str(_focus.get("hook_cap") or "") == "reload_page"
            and str(cursor.phase or "") == "do"
        ):
            from mino_nexus.loop.local_executors import _dispatch_device, _exec_ok

            plat = str(getattr(ctx, "platform", "") or "").lower()
            if plat and plat != "web":
                return CheckFlow(leave=leave(status="fail", summary="刷新页面只在 Web 执行", reason_code="capability_platform_mismatch"))
            reloaded = _dispatch_device(
                proxy,
                ctx=ctx,
                seq=seq,
                cap="reload_page",
                params={},
                label="刷新当前页面",
            )
            ok = _exec_ok(reloaded)
            summary = str(getattr(reloaded, "summary", "") or "")[:300]
            rec(
                seq,
                capability_id="reload_page",
                status="pass" if ok else "fail",
                summary=summary or ("已刷新页面" if ok else "刷新页面失败"),
                thought=summary,
                thumb=thumb,
            )
            if not ok:
                return CheckFlow(leave=leave(status="fail", summary=summary or "刷新页面失败", reason_code="page_refresh_failed"))
            from mino_nexus.loop.milestones import note_tool_pass_milestone

            note_tool_pass_milestone(cursor, capability_id="reload_page", writer=writer)
            _log_turn_end(writer, cap="reload_page", status="pass")
            return CheckFlow(cont=True)

        if action_scheme(ctx) == "dom":
            from mino_nexus.loop.locator_dispatch import try_locator_chain_decision

            dom_dec = try_locator_chain_decision(cursor, ctx, writer)
            if dom_dec is not None:
                cursor.dom_miss_streak = 0
                decision = dom_dec
        if decision is None and (_vision_plan_on() or execution_phase_active()):
            from mino_nexus.loop.milestone_orchestrator import run_vision_orchestrated_turn
            from mino_nexus.loop.program_device_fallback import merge_program_vision_fallback_slots

            merge_program_vision_fallback_slots(cursor, inspect_slots)
            if getattr(ctx, "router", None) is None:
                ctx.router = proxy
            send_first = not should_withhold_llm_image(ctx, shot=shot)
            orch = run_vision_orchestrated_turn(
                cursor=cursor,
                ctx=ctx,
                writer=writer,
                shot=shot,
                steps=steps,
                inspect_slots=inspect_slots,
                provider_id=str(provider_id or ""),
                phase_tool_kinds=phase_tool_kinds or None,
                send_image=send_first,
                in_prep=str(cursor.phase or "") == "prep",
                in_check=str(cursor.phase or "") == "check",
            )
            if orch.blocked_error:
                source = "screen" if orch.blocked_error.get("source") == "screen" else "model"
                kind = str(orch.blocked_error.get("fail_kind") or "request")
                return CheckFlow(leave=leave_blocked(seq, source, str(orch.blocked_error.get("error") or "模型不可用"),
                                                kind if source == "screen" else "model_" + kind))
            if orch.phase_transition:
                _log_turn_end(writer, cap="phase_transition", status="continue")
                return CheckFlow(cont=True)
            if orch.decision is not None:
                decision = orch.decision
                if "visual_observation_unavailable" in decision.parse_warnings:
                    _log_turn_end(writer, cap="", status="blocked")
                    return CheckFlow(leave=leave(status="unverified", summary=decision.thought,
                                  reason_code=str(getattr(decision, "error_kind", "") or "observation_missing")))
                if orch.plan_only and not (decision.action and decision.action.capability_id):
                    if str(decision.status or "") in ("fail", "give_up"):
                        summary = decision.thought or "里程碑失败，已停止空转"
                        return CheckFlow(leave=leave(status="fail", summary=summary[:500], reason_code="milestone_plan_incomplete"))
                    streak = int(getattr(cursor, "plan_only_streak", 0) or 0) + 1
                    cursor.plan_only_streak = streak
                    if streak >= 2:
                        return CheckFlow(leave=leave(
                            status="fail",
                            summary="连续两回合只有规划、没有可下发的动作，已停止。",
                         reason_code="model_action_missing"))
                    thought = decision.thought or "规划轮次，等待下一里程碑"
                    emit("think", thought=thought, step=seq, status="continue")
                    _log_turn_end(writer, cap="", status="continue")
                    return CheckFlow(cont=True)
                cursor.plan_only_streak = 0
        if decision is None:
            decision = _decide_gated()
        raw_llm = getattr(decision, "raw_llm", None) or {}
        model_meta = (raw_llm or {}).get("meta") or {}
        if model_meta.get("error") and not raw_llm.get("llm_output"):
            return CheckFlow(leave=leave_blocked(seq, "model", str(model_meta["error"]),
                                            "model_" + str(model_meta.get("fail_kind") or "request")))
        # 点名了知识就展开正文重决策一次。只一次 —— 展开后再点名一律忽略。
        body, named = expand_named_knowledge(decision, know_rows, ctx=ctx)
        if body:
            inspect_slots["knowledge_body"] = body
            emit(
                "think",
                thought=f"点名知识 {', '.join(named)}，展开正文后重新决策本步",
                step=seq, status="continue", knowledge_ids=named,
            )
            decision = _decide_gated()
            inspect_slots["knowledge_body"] = ""
        if decision.action and action_scheme(ctx) == "dom":
            from mino_nexus.action_space.scheme import structure_target_available, use_visual_fallback
            chain_hit = bool((decision.action.params or {}).get("locator_chain"))
            if not chain_hit and not structure_target_available(decision.action.capability_id, decision.action.params, ctx):
                use_visual_fallback(ctx, reason="model_target_not_unique_in_structure", writer=writer)
                # DOM 规划不能证明截图上没有挡屏。切换观察后先按同一截图重规划，尚未派发任何点击。
                from mino_nexus.loop.vision_plan import run_vision_plan_turn_if_enabled

                screenshot_slots = dict(inspect_slots)
                screenshot_plan = run_vision_plan_turn_if_enabled(
                    cursor=cursor, ctx=ctx, writer=writer, shot=shot, steps=steps,
                    inspect_slots=screenshot_slots, provider_id=str(provider_id or ""),
                    send_image=not should_withhold_llm_image(ctx, shot=shot),
                )
                if screenshot_plan is not None and screenshot_plan.blocked_error:
                    problem = screenshot_plan.blocked_error
                    return CheckFlow(leave=leave_blocked(
                        seq, "", str(problem.get("error") or "截图规划无法完成"),
                        str(problem.get("fail_kind") or "visual_replan_unavailable"),
                    ))
                if getattr(cursor, "milestone_hold_exec", False):
                    cursor.milestone_hold_exec = False
                    _log_turn_end(writer, cap="", status="pending")
                    return CheckFlow(cont=True)
                decision = _decide(send_image=not should_withhold_llm_image(ctx, shot=shot))
                fallback_meta = (decision.raw_llm or {}).get("meta") or {}
                if fallback_meta.get("error") and not (decision.raw_llm or {}).get("llm_output"):
                    return CheckFlow(leave=leave_blocked(seq, "model", str(fallback_meta["error"]),
                                                    "model_" + str(fallback_meta.get("fail_kind") or "request")))
        clear_transient_llm_image_gate(ctx)
        from mino_nexus.catalog.tool_schema import REPORT_SCREEN_OBSTACLE

        if decision.action and decision.action.capability_id == REPORT_SCREEN_OBSTACLE:
            from mino_nexus.loop.vision_plan import handle_execution_obstacle

            obstacle_plan = handle_execution_obstacle(cursor, ctx, decision, writer=writer, shot=shot)
            if obstacle_plan.blocked_error:
                problem = obstacle_plan.blocked_error
                return CheckFlow(leave=leave_blocked(
                    seq, REPORT_SCREEN_OBSTACLE, str(problem.get("error") or "挡屏恢复不可用"),
                    str(problem.get("fail_kind") or "screen_obstacle_unavailable"),
                ))
            # 此轮只有观察报告。下一轮执行恢复焦点，不把报告或清障当业务 pass。
            _log_turn_end(writer, cap=REPORT_SCREEN_OBSTACLE, status="pending")
            return CheckFlow(cont=True)
        from mino_nexus.loop.vision_exec import execution_phase_active

        apply_decision_milestones(
            cursor,
            decision,
            writer=writer,
            ctx=ctx,
            app_id=str(getattr(ctx, "app_id", "") or ""),
        )
        apply_step_outcome(cursor, decision, ctx, writer=writer)
        if (
            isinstance(cursor, StepCursor) and cursor.phase == 'do' and cursor.try_auto_finish_do_when_milestones_met(ctx, writer=writer)
        ):
            rec(
                seq,
                capability_id="signal_done",
                status="skipped",
                summary="登录/子里程碑已齐，自动进入校验",
                thought=thought,
                thumb=thumb,
            )
            emit(
                "result",
                thought=thought,
                step=seq,
                capability_id="signal_done",
                status="skipped",
                summary="登录/子里程碑已齐，自动进入校验",
                thumb=thumb,
            )
            _log_turn_end(writer, cap="signal_done", status="skipped")
            return CheckFlow(cont=True)
        if use_milestone_check and not cursor.step_checked:
            mv = evaluate_milestones(cursor, ctx)
            if mv.phase_complete:
                cursor.mark_checked()
    return CheckFlow(decision=decision, shot=shot, thumb=thumb)
