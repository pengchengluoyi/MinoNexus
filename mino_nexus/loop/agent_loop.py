"""单用例看图闭环。prep / do / check 都由 Agent 看图决策。"""
from __future__ import annotations

from mino_nexus.navigation.login.transition import update_auth_state

import hashlib
import re
import time
from pathlib import Path
from typing import Any

from mino_nexus.loop.fuse.action_fuse import fuseable_cap
from mino_nexus.loop.step_pointer import cap_clears_repeat_tap
from mino_nexus.ai.schemas import AgentAction, AgentDecision
from mino_nexus.catalog.exec_classes import MUTATE_CAPS, PROGRESS_CAPS
from mino_nexus.loop.registry import apply_force_case_expectation, run_guards
from mino_nexus.loop.inspections import (
    llm_session_block,
    match_stuck_docs,
    reconcile_session_block_with_hierarchy,
    run_inspections,
)
from mino_nexus.ai.app_intel import context_pack_for_step, log_context_pack
from mino_nexus.loop.otp_prep_hint import compile_otp_prep_hint
from mino_nexus.loop.login_session_hint import compile_login_session_hint
from mino_nexus.resources.session.scene_gate import (
    ensure_case_scene,
    is_login_module_case,
)
from mino_nexus.runtime.platform_gate import platform_skip_reason
from mino_nexus.loop.sop_runtime import merge_phase_tool_kinds, normalize_phases, phase_for_id
from mino_nexus.core.log import SLog
from mino_nexus.loop.observe.agent_stream import emit_agent_event, make_thumb
from mino_nexus.records.eventlog.writer import SessionWriter, bind_writer, open_session
from mino_nexus.loop.observe.execution_diagnostics import execution_result_metadata
from mino_nexus.loop.recovery import recover_if_needed
from mino_nexus.loop.execution.actions import _run_action
from mino_nexus.loop.execution.auto_finish import note_progress_and_auto_finish
from mino_nexus.loop.execution.preflight import run_preflight
from mino_nexus.loop.execution.setup import prepare_run
from mino_nexus.loop.execution.signal_done import Signal, handle_signal_done
from mino_nexus.loop.execution.tap_focus import bind_tap_focus
from mino_nexus.loop.execution.tool_pass import note_tool_pass
from mino_nexus.loop.execution.state import RunState
from mino_nexus.loop.execution.budget import _case_overview, _finish, _finish_case
from mino_nexus.loop.execution.facts import (
    _effect_nodes,
    _locator_blocks_action,
    _note_control_locator,
)
from mino_nexus.loop.execution.check_phase import run_check_phase
from mino_nexus.loop.execution.fail_branch import handle_fail_branch
from mino_nexus.loop.execution.foreground import ensure_foreground
from mino_nexus.loop.execution.guard_ctx import build_guard_ctx
from mino_nexus.loop.execution.guard_skip import run_guard_skip
from mino_nexus.loop.execution.login_nav import run_login_nav
from mino_nexus.loop.execution.login_tap import handle_login_tap
from mino_nexus.loop.execution.inspections_gate import run_phase_inspections
from mino_nexus.loop.execution.lease_account import note_lease_account
from mino_nexus.loop.execution.nav_bookkeeping import note_fsm_navigation
from mino_nexus.loop.execution.nav_degraded import note_nav_degraded
from mino_nexus.loop.execution.model_done import run_model_done
from mino_nexus.loop.execution.model_stop import run_model_stop
from mino_nexus.loop.execution.nav_verdict import run_nav_verdict
from mino_nexus.loop.execution.tap_params import run_tap_params
from mino_nexus.loop.execution.phase_contract import note_phase_contract
from mino_nexus.loop.execution.progress_pass import record_progress_pass
from mino_nexus.loop.execution.step_navigation import refresh_step_navigation
from mino_nexus.loop.execution.turn_outcome import resolve_turn_outcome
from mino_nexus.loop.execution.visual_prior import probe_visual_prior
from mino_nexus.loop.execution.page_ready import (
    _screen_fp,
    _sync_app_foreground_ctx,
)
from mino_nexus.loop.execution.turn_prior import apply_turn_prior
from mino_nexus.loop.execution.turn_log import (
    _BUDGET_CREDIT_CAP_SEC,
    _llm_trace,
    _log_context_slots,
    _log_turn_end,
    _record,
)
from mino_nexus.loop.router_proxy import RouterProxy
from mino_nexus.loop.observe.skill_trace import envelope as skill_envelope
from mino_nexus.loop.step_pointer import (
    StepCursor,
    build_seq_nodes,
)
from mino_nexus.core.protocol import EventStatus
from mino_nexus.casework.run_store import report_run_id
from mino_nexus.runtime.run_context import bind_case_project, build_run_context
from mino_nexus.loop.web.web_env import cleanup_after_case, reset_before_case
from mino_nexus.core.schemas import PlanEvent

def _execution_revision() -> str:
    """执行体指纹：编排器 + `loop/execution/` 全部模块，任一改动都必须换 revision。"""
    h = hashlib.sha256()
    for path in [Path(__file__)] + sorted(Path(__file__).parent.joinpath("execution").glob("*.py")):
        h.update(path.name.encode())
        h.update(path.read_bytes())
    return "sha256:" + h.hexdigest()


EXECUTION_REVISION = _execution_revision()

TAG = "AgentLoop"
_MAX_STEPS = 24
_TURN_SAFETY_CAP = 500



def run_case(
    *,
    run_id: str,
    case: dict[str, Any],
    sn: str,
    app_id: str,
    package: str = "",
    provider_id: str = "",
    playbook: dict | None = None,
    app_name: str = "",
    cancel_check=None,
    case_seq: int = 0,
    playwright_headless: bool = True,
    parent_session_id: str = "",
    fork_state: dict[str, Any] | None = None,
    run_env_brief: str = "",
    next_case: dict[str, Any] | None = None,
    resource_attempt: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from mino_nexus.platform.llm import dispatch_log as dispatch

    from mino_nexus.loop.step_contract import bind_run_case
    from mino_nexus.knowledge.control_registry import registry_runtime

    binding_case = case  # Preserve compiled source before target-specific runtime copies.
    bind_run_case(case if isinstance(case, dict) else None)
    cid = str(case.get("case_id") or "")
    stream_id = str(case.get("report_run_id") or "").strip() or (
        report_run_id(run_id, cid, sn=str(sn or ""), coverage=str(case.get("coverage") or "once"))
        if cid
        else run_id
    )
    is_explore_case = str(case.get("source") or "") == "explore"
    overview = (
        str(case.get("steps_raw") or (case.get("steps") or ["应用探索"])[0]).strip()
        if is_explore_case
        else _case_overview(case)
    )
    t0 = time.time()
    ctx = build_run_context(
        sn,
        run_id=run_id,
        app_id=app_id,
        provider_id=provider_id,
        target_package=package,
    )
    ctx.session_id = stream_id
    if playbook:
        ctx.playbook = playbook
    scout_run_id = stream_id
    from mino_nexus.action_space.scheme import normalize_action_scheme

    ctx.scout_run_id = scout_run_id
    ctx.case_seq = case_seq
    env_brief = str(run_env_brief or "").strip()
    if env_brief:
        ctx.env_label = env_brief
        ctx.env_fact = {"brief": env_brief, "confirmed": True}
    run_type = "manual"
    expected_build = str(case.get("expected_build") or "")
    try:
        from mino_nexus.casework import run_store as _rs

        _doc = _rs.get(run_id)
        if _doc:
            expected_build = str(_doc.get("expected_build") or expected_build)
            ctx.env_profile = str(_doc.get("env_profile") or ctx.env_profile or "test")
            ctx.env_surface = str(_doc.get("env_surface") or getattr(ctx, "env_surface", "") or "")
            ctx.action_scheme = (
                normalize_action_scheme(_doc.get("action_scheme"))
            )
            ctx.plugin_user_id = str(_doc.get("plugin_user_id") or getattr(ctx, "plugin_user_id", "") or "")
            # GuardGate stop 时 ask_human 还是 give_up 取决于它（设计稿 §11.5）
            run_type = str(_doc.get("run_type") or "manual").lower()
            if run_type == "explore":
                is_explore_case = True
    except Exception:
        pass
    bind_run_case(case)
    ctx.case_id = cid
    project_scope_error = ""
    try:
        bind_case_project(ctx, case)
    except ValueError as exc:
        project_scope_error = str(exc)
    try:
        from mino_nexus.resources.accounts.lease import restore_lease_for_run

        restored_row, _ = restore_lease_for_run(ctx, run_id) if not project_scope_error else (None, "")
        if restored_row or str(
            (getattr(ctx, "resource_lease", None) or {}).get("account_id") or ""
        ).strip():
            ctx.prep_lease_account_done = True
    except Exception:
        pass
    proxy = RouterProxy(
        sn,
        run_id=scout_run_id,
        task_id=run_id,
        target_package=package,
        playwright_headless=playwright_headless,
        run_context=ctx,
    )
    proxy.action_scheme = normalize_action_scheme(getattr(ctx, "action_scheme", ""))
    history: list[str] = []
    steps: list[dict[str, Any]] = []

    def emit(phase: str, **extra: Any) -> None:
        if phase == "result":
            cap = str(extra.get("capability_id") or "")
            from mino_nexus.catalog.exec_classes import MUTATE_CAPS

            # 不在这里再向 Scout 要一张图。打开页面后立刻截图会卡满 8 秒，
            # 下一回合的决策截图拿不到 RESULT，整条用例报 scout timeout。
            # 结果图用下一回合已经拍下的决策图，挂回这一步。
            if cap in MUTATE_CAPS | {"launch_app", "open_url", "open_app"}:
                ctx.pending_result_step = extra.get("step")
                ctx.pending_result_cap = cap
        payload = {
            "run_id": stream_id,
            "task_id": run_id,
            "case_id": cid,
            "app_id": app_id,
            "phase": phase,
            **extra,
        }
        emit_agent_event(payload)

    tok = dispatch.bind(
        trigger="app_explore" if is_explore_case else "case_run",
        source="explore_run" if is_explore_case else "case_run",
        role="test-engineer",
        skill="explore-app" if is_explore_case else "run-case",
        app_id=app_id,
        app_name=app_name,
        pipeline_id=stream_id,
        task_id=run_id, case_id=cid, session_id=stream_id,
        case_step_index=0,
    )
    sop_orchestration: dict[str, Any] = {}
    if not is_explore_case:
        from mino_nexus.ai.skill_store import get_skill

        from mino_nexus.loop.sop_runtime import normalize_phases
        from mino_nexus.loop.vision_flags import sop_orchestration_snapshot

        _skill_row = get_skill("run-case") or {}
        _sop = _skill_row.get("sop") if isinstance(_skill_row.get("sop"), dict) else {}
        sop_orchestration = sop_orchestration_snapshot(normalize_phases(_sop.get("phases")))
    ordinary_contract_enabled = not is_explore_case and ctx.execution_mode == "agent" and ctx.platform in {"android", ""}
    from mino_nexus.records.eventlog.writer import SessionPersistenceError
    from mino_nexus.loop.resolved_case import ContractError

    try:
        writer = open_session(
            session_id=stream_id,
            run_id=run_id,
            case_id=cid,
            app_id=app_id,
            provider_id=provider_id,
            sn=sn,
            sop_id="explore-app" if is_explore_case else "run-case",
            extra={
                "goal": overview,
                "seq_node_count": len(build_seq_nodes(case)),
                "sop_orchestration": sop_orchestration,
                "project_id": ctx.project_id,
                "device_platform": ctx.platform,
                "device_os": ctx.desktop_os,
                "env_surface": ctx.env_surface,
                "case_platform": str(case.get("platform") or ""),
                "execution_mode": ctx.execution_mode,
                **({"parent_session_id": parent_session_id} if parent_session_id else {}),
                **({"fork_from_turn": fork_state.get("fork_from_turn")} if fork_state else {}),
            },
        )
    except SessionPersistenceError as exc:
        # 尚未进入设备循环，只释放恢复的未使用租约；不做设备清理。
        from mino_nexus.resources.accounts.lease import release_ctx_lease

        release_ctx_lease(ctx)
        dispatch.reset(tok)
        return {"status": "unverified" if ordinary_contract_enabled else "blocked", "summary": str(exc), "steps": steps,
                "reason_code": exc.reason_code, "failure_stage": exc.failure_stage,
                "execution_started": "no"}
    storage_failed = False
    execution_admitted = False
    if fork_state and writer:
        writer.append("session/fork", dict(fork_state))
    outcome: dict[str, Any] = {"status": "fail", "summary": "未开始", "steps": steps}
    scene_error = ""
    try:
        ctx.case_scene = ensure_case_scene(case, getattr(ctx, "case_scene", None))
    except ValueError as exc:
        scene_error = str(exc)
    from mino_nexus.core.platform_contract import target_platform
    platform_error = ""
    try:
        target_platform(ctx.platform)
    except ValueError as exc:
        platform_error = str(exc)
    plat_skip = platform_skip_reason(case, ctx.platform, device_os=ctx.desktop_os) if not platform_error else None
    try:
        with bind_writer(writer), registry_runtime(ctx.execution_mode):
            if isinstance(case.get("execution_target"), dict):
                writer.append("platform/bind", dict(case["execution_target"]))
            if project_scope_error:
                _record(steps, history, 0, capability_id="project_scope", status="blocked",
                        summary=project_scope_error, thought=project_scope_error)
                writer.append("contract/blocked", {
                    "capability_id": "project_scope", "error_kind": "project_scope_mismatch",
                    "reason_code": "project_scope_mismatch", "reason": project_scope_error,
                    "retryable": False,
                })
                outcome = _finish(emit, status="tool_blocked" if ordinary_contract_enabled else "blocked", summary=project_scope_error, steps=steps, t0=t0)
                outcome["reason_code"] = "project_scope_mismatch"
            elif platform_error:
                writer.append("contract/blocked", {"reason_code": "device_platform_invalid",
                    "reason": platform_error, "execution_started": "no", "retryable": False})
                outcome = _finish(emit, status="env_blocked" if ordinary_contract_enabled else "blocked", summary="设备平台缺失或无效，请检查节点上报", steps=steps, t0=t0)
                outcome.update(reason_code="device_platform_invalid", execution_started="no")
            elif scene_error:
                writer.append("contract/blocked", {"reason_code":"case_scene_invalid","reason":scene_error,"retryable":False})
                outcome = _finish(emit,status="tool_blocked" if ordinary_contract_enabled else "blocked",summary=f"场景合同无效，需重新编译：{scene_error}",steps=steps,t0=t0)
                outcome["reason_code"] = "case_scene_invalid"
            elif plat_skip:
                def _skip_emit(phase: str, **extra: Any) -> None:
                    payload = {
                        "run_id": stream_id,
                        "task_id": run_id,
                        "case_id": cid,
                        "app_id": app_id,
                        "phase": phase,
                        **extra,
                    }
                    emit_agent_event(payload)

                _record(steps, history, 0, capability_id="signal_skip", status="skip", summary=plat_skip, thought=plat_skip)
                writer.append(
                    "platform/skip",
                    {"reason": plat_skip, "device_platform": ctx.platform,
                     "device_os": ctx.desktop_os, "case_platform": case.get("platform"),
                     "env_surface": ctx.env_surface},
                )
                _skip_emit("start", thought="渠道校验", step=0, goal=overview)
                outcome = _finish(_skip_emit, status="env_blocked" if ordinary_contract_enabled else "skip", summary=plat_skip, steps=steps, t0=t0)
                outcome["reason_code"] = "platform_not_applicable"
            else:
                writer.append("scope/bind", {
                    "app_id": ctx.app_id, "project_id": ctx.project_id,
                    "case_project_id": str(case.get("project_id") or ""),
                    "source": "app_project", "status": "bound",
                })
                writer.require_persisted()
                from mino_nexus.casework.execution_binding_runtime import BindingAdmissionError
                from mino_nexus.casework.execution_requirements import ordinary_requirements
                try:
                    if not is_explore_case:
                        requirements = ordinary_requirements(binding_case, app_id, platform=ctx.platform,
                            action_scheme=ctx.action_scheme, provider_id=provider_id)
                        proxy.bind_execution(requirements, session_id=stream_id,
                            env_profile=ctx.env_profile, env_surface=ctx.env_surface)
                    if ctx.execution_contract_version != "execution_binding.v1" or not ctx.execution_binding:
                        raise BindingAdmissionError("binding_not_committed")
                    if ordinary_contract_enabled:
                        from mino_nexus.loop.case_runner import resolve_admitted_case
                        from mino_nexus.loop.resolved_case import persist_contract
                        ctx.resolved_case = resolve_admitted_case(case, ctx, expected_build=expected_build,
                            started_at=t0, max_steps=_TURN_SAFETY_CAP)
                        persist_contract(ctx.resolved_case, writer)
                        writer.append("execution/runtime", {"revision": EXECUTION_REVISION})
                    writer.set_phase("prep")
                    execution_admitted = True
                except BindingAdmissionError as exc:
                    writer.append("execution/blocked", {"reason_code": exc.reason_code,
                        "diagnostics": exc.diagnostics, "execution_started": "no"})
                    writer.require_persisted()
                    outcome = _finish(emit, status="tool_blocked" if ordinary_contract_enabled else "blocked", summary=exc.reason_code, steps=steps, t0=t0)
                    outcome.update(reason_code=exc.reason_code, failure_stage="execution_binding", execution_started="no")
                    return outcome
                outcome = _run_loop(
                    emit=emit,
                    ctx=ctx,
                    proxy=proxy,
                    case=None if ctx.resolved_case else case,
                    resolved_case=ctx.resolved_case,
                    cid=cid,
                    overview=overview,
                    provider_id=provider_id,
                    cancel_check=cancel_check,
                    history=history,
                    steps=steps,
                    t0=t0,
                    scout_run_id=scout_run_id,
                    case_seq=case_seq,
                    writer=writer,
                    fork_state=fork_state,
                    run_env_brief=env_brief,
                    run_type=run_type,
                )
            return outcome
    except ContractError as exc:
        failed_cursor = getattr(ctx, "_step_cursor", None)
        failed_node = failed_cursor.current() if failed_cursor is not None else None
        outcome = {"status": exc.status, "summary": exc.reason_code, "reason_code": exc.reason_code,
                   "steps": steps, "execution_started": "no" if not execution_admitted else "unknown",
                   "failure_stage": getattr(failed_cursor, "phase", "admission"),
                   "case_step": 0 if getattr(failed_cursor, "phase", "") == "prep" else getattr(failed_node, "n", 0)}
        blocked_node = failed_node if isinstance(failed_node, dict) else {}
        writer.append("contract/blocked", {"reason_code": exc.reason_code, "error_kind": exc.reason_code,
            "phase": str(getattr(failed_cursor, "phase", "") or ""),
            "case_step": outcome["case_step"],
            "capability_id": str(blocked_node.get("capability_id") or blocked_node.get("device_cap")
                or blocked_node.get("hook_cap") or blocked_node.get("event_id") or ""),
            "step_id": str(blocked_node.get("id") or blocked_node.get("step_id") or "")})
        return outcome
    except SessionPersistenceError as exc:
        storage_failed = True
        outcome = {"status": "blocked", "summary": str(exc), "steps": steps,
                   "reason_code": exc.reason_code, "failure_stage": exc.failure_stage}
        return outcome
    except Exception as exc:
        import traceback

        summary = f"{type(exc).__name__}: {exc}"[:240] or "执行异常"
        tb = traceback.format_exc()
        SLog.e(TAG, f"run_case failed run={run_id} case={cid}: {summary}\n{tb}")
        # 控制台日志排查时拿不到（IDE 会滚掉），把栈写进 session 轨迹：
        # 只留一层消息的 summary 不够定位到具体行（2026-10-22 的 'NoneType'.leave 就是这样）。
        try:
            writer.append("run/exception", {"error": summary, "traceback": tb[-6000:]})
        except Exception:
            pass
        outcome = {
            "status": "fail",
            "summary": summary,
            "steps": steps,
        }
        return outcome
    finally:
        from mino_nexus.casework.account_facet_commit import commit_case_facet_effects

        from mino_nexus.casework.run_write_safety import protected

        storage_failed = storage_failed or bool(writer._store_failure)
        isolated = protected(run_id, sn=sn)
        if isolated:
            outcome.update(status="blocked", summary="写动作结果未核验，设备与账号资源仍隔离", resource_isolated=True)
        if execution_admitted and not project_scope_error and not storage_failed and not isolated:
            proxy._binding_phase_override = "cleanup"
            ctx.execution_cleanup_active = True
            try:
                with bind_writer(writer):
                    cleanup_after_case(
                        proxy,
                        ctx,
                        run_id=scout_run_id,
                        case_seq=case_seq,
                        case=case,
                        next_case=next_case,
                    )
            except Exception as exc:
                outcome.update(status="unverified" if ordinary_contract_enabled else "blocked",
                               reason_code="cleanup_failed", summary=f"清理未完成：{type(exc).__name__}")
                writer.append("execution/cleanup_failed", {"reason_code": "cleanup_failed", "error_type": type(exc).__name__})
        isolated = protected(run_id, sn=sn)
        if isolated:
            outcome.update(status="blocked", summary="写动作结果未核验，设备与账号资源仍隔离", resource_isolated=True,
                           reason_code="write_outcome_unknown")
        if storage_failed:
            outcome.update(status="blocked", summary="会话记录未能持久化，已阻止后续界面写动作",
                           reason_code="session_store_unavailable")
        if ordinary_contract_enabled:
            from mino_nexus.loop.execution_outcome import TERMINALS, terminal_for_reason
            if cancel_check and cancel_check():
                outcome.update(status='unverified', reason_code='task_cancelled', summary='任务已取消，业务结果未核验')
            reason = str(outcome.get("reason_code") or "execution_exception")
            if outcome.get("resource_isolated"):
                reason = "write_outcome_unknown"
            if storage_failed:
                reason = "session_store_unavailable"
            if outcome.get("status") not in TERMINALS or outcome.get("resource_isolated") or storage_failed:
                outcome.update(status=terminal_for_reason(reason), reason_code=reason)
        try:
            if not writer._closed and not storage_failed:
                step_cursor = getattr(ctx, "_step_cursor", None)
                if step_cursor is not None:
                    from mino_nexus.loop.observe.ops_guard_diag import session_end_guard_summary

                    gs = session_end_guard_summary(step_cursor)
                    if gs.get("guard_block_counts") or gs.get("last_guard_block"):
                        writer.append("ops/guard_summary", gs)
                if ordinary_contract_enabled:
                    outcome.setdefault("failure_stage", getattr(step_cursor, "phase", "admission") if outcome.get("status") != "pass" else "")
                    current_node = step_cursor.current() if step_cursor is not None else None
                    outcome.setdefault("case_step", 0 if getattr(step_cursor, "phase", "") == "prep" else getattr(current_node, "n", 0))
                    writer.append("execution/exit", {"status": outcome.get("status"),
                        "reason_code": outcome.get("reason_code", ""),
                        "exit_path": "finalizer", "phase": getattr(step_cursor, "phase", "admission"),
                        "failure_stage": outcome.get("failure_stage", ""), "case_step": outcome.get("case_step", 0)})
                writer.close(
                    status=str(outcome.get("status") or "fail"),
                    summary=str(outcome.get("summary") or ""),
                    step_count=len(outcome.get("steps") or []),
                    reason_code=str(outcome.get("reason_code") or ""),
                    failure_stage=str(outcome.get("failure_stage") or ""),
                    case_step=int(outcome.get("case_step") or 0),
                )
                if ordinary_contract_enabled and getattr(writer, 'final_outcome', None):
                    outcome.update(writer.final_outcome)
        except SessionPersistenceError:
            storage_failed = True
        if writer._store_failure:
            outcome.update(status="blocked", summary="会话终结记录未能持久化，结果未核验",
                           reason_code="session_store_unavailable")
        if execution_admitted and not project_scope_error and not storage_failed and not writer._store_failure and not isolated:
            try:
                commit_case_facet_effects(
                    ctx,
                    case,
                    status=str(outcome.get("status") or ""),
                    outcome=outcome,
                )
            except Exception as exc:
                # Facet projection must not skip the durable-result resource exit.
                SLog.e(TAG, f"case facet projection failed: {type(exc).__name__}")
        from mino_nexus.resources.accounts.lease import release_ctx_lease

        try:
            if ordinary_contract_enabled:
                from mino_nexus.loop.execution_outcome import finish_resources
                # Mark ownership before release, including a partial release failure.
                outcome["resources_owned_by_exit"] = True
                if writer._store_failure:
                    outcome.update(status="unverified", reason_code="session_store_unavailable")
                try:
                    outcome["resources_finalized"] = finish_resources(ctx, attempt=resource_attempt,
                        storage_failed=storage_failed or bool(writer._store_failure))
                except Exception as exc:
                    outcome.update(resources_finalized=False, resource_release_error=type(exc).__name__)
                    SLog.e(TAG, f"case resource release failed: {type(exc).__name__}")
            elif not isolated:
                release_ctx_lease(ctx)
        finally:
            dispatch.reset(tok)






def _run_loop(
    *,
    emit,
    ctx,
    proxy: RouterProxy,
    case: dict[str, Any] | None,
    cid: str,
    overview: str,
    provider_id: str,
    cancel_check,
    history: list[str],
    steps: list[dict[str, Any]],
    t0: float,
    scout_run_id: str,
    resolved_case=None,
    case_seq: int = 0,
    writer: SessionWriter | None = None,
    fork_state: dict[str, Any] | None = None,
    run_env_brief: str = "",
    run_type: str = "manual",
) -> dict[str, Any]:
    from mino_nexus.contract.coords import prepare_xy_params_for_execute
    from mino_nexus.loop.milestones import read_state
    from mino_nexus.loop.milestone_orchestrator import in_progress_milestone
    from mino_nexus.platform.llm import dispatch_log as dispatch
    from mino_nexus.runtime.menu import available_menu_brief

    setup = prepare_run(
        case=case,
        resolved_case=resolved_case,
        run_type=run_type,
        ctx=ctx,
        provider_id=provider_id,
        scout_run_id=scout_run_id,
        overview=overview,
    )
    case = setup.case
    is_explore = setup.is_explore
    skill = setup.skill
    sop = setup.sop
    phases = setup.phases
    inspections = setup.inspections
    wall_budget_sec = setup.wall_budget_sec
    max_steps = setup.max_steps
    cursor = setup.cursor
    inspect_slots = setup.inspect_slots
    doc_stuck_used = setup.doc_stuck_used
    doc_stuck_budget = setup.doc_stuck_budget
    nav = setup.nav
    target_pkg = setup.target_pkg

    def _leave(*, status: str, summary: str, pack=None, reason_code: str = ""):
        if resolved_case is not None:
            from mino_nexus.loop.execution_outcome import terminal_for_reason
            from mino_nexus.loop.resolved_case import case_pass_reason
            if status == "pass":
                summary = case_pass_reason(cursor)
                reason_code = "required_assertions_consumed"
            else:
                status = terminal_for_reason(reason_code, status)
            writer.append("execution/leave", {"status": status, "reason_code": reason_code,
                "exit_path": "leave", "phase": cursor.phase,
                "case_step": 0 if cursor.phase == "prep" else getattr(cursor.current(), "n", 0)})
        if nav is not None:
            nav.shutdown(writer=writer, status=status, summary=summary)
        result = _finish(emit, status=status, summary=summary, steps=steps, t0=t0, pack=pack or _pack)
        if reason_code:
            result["reason_code"] = reason_code
        result["failure_stage"] = cursor.phase if status != "pass" else ""
        result["case_step"] = 0 if cursor.phase == "prep" else getattr(cursor.current(), "n", 0)
        return result

    def _leave_case(cursor):
        if resolved_case is not None:
            from mino_nexus.loop.resolved_case import case_pass_reason
            return _leave(status="pass", summary=case_pass_reason(cursor),
                          reason_code="required_assertions_consumed")
        result = _finish_case(emit, cursor=cursor, steps=steps, t0=t0, pack=_pack)
        if nav is not None:
            nav.shutdown(
                writer=writer,
                status=str(result.get("status") or ""),
                summary=str(result.get("summary") or ""),
            )
        return result

    ran_case_start_inspection = False
    last_phase_seen = ""
    try:
        ctx.case_scene = ensure_case_scene(case, getattr(ctx, "case_scene", None))
    except ValueError as exc:
        writer.append("contract/blocked", {"reason_code":"case_scene_invalid", "reason":str(exc), "retryable":False})
        return _leave(status="blocked", summary=f"场景合同无效，需重新编译用例：{exc}", reason_code="case_scene_invalid")
    ctx.case = case
    if isinstance(cursor, StepCursor):
        setattr(cursor, "case", case)
    from mino_nexus.loop.observe.session_scope import begin_case_session_scope

    begin_case_session_scope(ctx, case_id=str(cid or ""), scene=ctx.case_scene)
    from mino_nexus.loop.fuse.llm_screenshot_gate import reset_case_llm_image_gate

    reset_case_llm_image_gate(ctx)
    login_module_case = is_login_module_case(case=case, scene=ctx.case_scene)
    if isinstance(cursor, StepCursor):
        cursor.login_module_prompt = login_module_case
    from mino_nexus.resources.session.ensure import ensure_case_account

    ensure_case_account(ctx, case)
    try:
        from mino_nexus.resources import project_store as _ps
        from mino_nexus.resources.otp_resolve import format_run_credential_hint

        _app = _ps.require_app(str(getattr(ctx, "app_id", "") or ""))
        _pid = str(_app.get("project_id") or "").strip()
        if _pid:
            _hint = format_run_credential_hint(
                _ps.project_env(_pid),
                env_profile=str(getattr(ctx, "env_profile", "") or "test"),
                env_surface=str(getattr(ctx, "env_surface", "") or ""),
                plugin_user_id=str(getattr(ctx, "plugin_user_id", "") or ""),
            )
            if _hint:
                history.append(f"0. program → info: {_hint}")
    except Exception:
        pass
    task_env_brief = str(run_env_brief or getattr(ctx, "env_label", "") or "").strip()
    if task_env_brief:
        history.append(f"0. program → info: 本任务运行环境已确认：{task_env_brief}")
    if not is_explore and wall_budget_sec > 0:
        history.append(
            f"0. program → info: 本用例墙钟预算 {wall_budget_sec} 秒"
            f"（前置每条 30 秒、操作每步 60 秒、校验每步 45 秒）；耗尽即失败，不再按「N 步」截断。"
        )
    cursor.progress_gate.reset_milestone(
        cursor.phase,
        0 if cursor.phase == "prep" else (cursor.current().n if cursor.current() else 0),
    )

    def _phase_cfg() -> dict[str, Any]:
        return phase_for_id(phases, cursor.phase)

    def _pack(**extra: Any) -> dict[str, Any]:
        env = skill_envelope(skill=skill, cursor=cursor, steps=steps)
        return {**env, **extra}

    raw_emit = emit
    turn_mark = {"t0": time.time()}

    def emit(phase: str, **extra: Any) -> None:
        if phase == "result":
            tool_ms = int(extra.get("elapsed_ms") or 0)
            if tool_ms > 0:
                extra["tool_elapsed_ms"] = tool_ms
            extra["elapsed_ms"] = max(1, int((time.time() - float(turn_mark["t0"])) * 1000))
            turn_mark["t0"] = time.time()
        env = skill_envelope(skill=skill, cursor=cursor, steps=steps)
        extra = {
            "skill_id": env.get("skill_id"),
            "view_id": env.get("view_id"),
            "slots": env.get("slots"),
            **extra,
        }
        extra.setdefault("loop_phase", cursor.phase)
        node = cursor.current()
        extra.setdefault("case_step", 0 if cursor.phase == "prep" else (node.n if node else 0))
        raw_emit(phase, **extra)

    def rec(seq: int, **kw: Any) -> dict[str, Any]:
        extra = dict(kw.pop("extra", None) or {})
        extra["loop_phase"] = cursor.phase
        node = cursor.current()
        extra["case_step"] = 0 if cursor.phase == "prep" else (node.n if node else 0)
        extra["case_step_index"] = extra["case_step"]
        kw["extra"] = extra
        return _record(steps, history, seq, **kw)

    def _leave_execution_blocked(seq: int, cap: str, summary: str, error_kind: str):
        rec(seq, capability_id=cap, status="blocked", summary=summary, thought=summary)
        emit("result", step=seq, capability_id=cap, status="blocked",
             result_status="blocked", summary=summary, thought=summary)
        if writer:
            writer.append("contract/blocked", {
                "capability_id": cap, "error_kind": error_kind,
                "reason_code": error_kind, "reason": summary, "retryable": False,
            })
        _log_turn_end(writer, cap=cap, status="blocked")
        return _leave(status="blocked", summary=summary, reason_code=error_kind)

    def _required_execution_blocker():
        from mino_nexus.loop.milestone_orchestrator import required_milestone_blocker
        from mino_nexus.loop.milestones import read_state

        return required_milestone_blocker(read_state(cursor))

    if fork_state:
        prov_override = str(fork_state.get("provider_id") or "").strip()
        if prov_override:
            provider_id = prov_override
        hist = fork_state.get("history") or []
        if isinstance(hist, list) and hist:
            history.extend(str(x) for x in hist if x)
        ph = str(fork_state.get("phase") or "").strip()
        if ph == "prep" and cursor.precondition:
            cursor.phase = "prep"
            cursor.index = 0
        elif ph == "check":
            cursor.enter_check()
        elif ph == "do":
            cursor.phase = "do"
            cursor.step_checked = False

    emit("start", thought="开始看图执行", step=0, goal=overview)

    if not ctx.has_control_channel:
        summary = "没有可用设备通道。请确认 Scout 在线并已上报这台设备。"
        return _leave(status="fail", summary=summary, reason_code="device_channel_unavailable")

    if not cursor.nodes:
        summary = "用例没有可执行的步骤。"
        return _leave(status="fail", summary=summary, reason_code="case_steps_missing")

    reset_before_case(proxy, ctx, run_id=scout_run_id, case_seq=case_seq, case=case)
    from mino_nexus.resources.resource_preflight import claim_requires_clear_cache

    def _screen_unregistered(out) -> str:
        if out is None:
            return ""
        text = str(getattr(out, "error", "") or "")
        summary = out.summary() if callable(getattr(out, "summary", None)) else ""
        text = f"{text} {summary}"
        if "capability_unregistered" in text or "能力目录里没有" in text:
            return text.strip()[:240]
        return ""

    state = RunState(
        setup=setup,
        ctx=ctx,
        proxy=proxy,
        writer=writer,
        emit=emit,
        history=history,
        scout_run_id=scout_run_id,
        t0=t0,
        case_seq=case_seq,
        resolved_case=resolved_case,
        login_module_case=login_module_case,
        cancel_check=cancel_check,
        leave=_leave,
        rec=rec,
        screen_unregistered=_screen_unregistered,
    )
    preflight = run_preflight(state)
    if preflight.aborted:
        return preflight.aborted
    fixed_deadline = preflight.fixed_deadline
    _clear_before_launch = preflight.clear_before_launch
    for seq in range(1, max_steps + 1):
        if cancel_check and cancel_check():
            return _leave(status="cancelled", summary="任务已取消", reason_code="task_cancelled")
        credit_sec = 0.0 if resolved_case else min(float(getattr(ctx, "case_budget_credit_sec", 0) or 0), _BUDGET_CREDIT_CAP_SEC)
        if wall_budget_sec > 0:
            ctx.case_deadline_ts = min(t0 + float(wall_budget_sec) + credit_sec, fixed_deadline or float("inf"))
        if fixed_deadline and time.time() >= fixed_deadline:
            return _leave(status="blocked", summary="持久执行期限已到，未派发后续动作", reason_code="case_budget_exhausted")
        if not is_explore and wall_budget_sec > 0 and (time.time() - t0 - credit_sec) >= wall_budget_sec:
            summary = f"超过 {wall_budget_sec} 秒仍未完成" + (
                f"（已抵扣模型空工具调用重试 {credit_sec:.0f} 秒）" if credit_sec else ""
            )
            return _leave(status="fail", summary=summary, reason_code="case_budget_exhausted")
        turn_mark["t0"] = time.time()
        turn_decision_cap = ""
        turn_decision_status = ""
        cur_node = cursor.current()
        dispatch.bind(case_step_index=0 if cursor.phase == "prep" else (cur_node.n if cur_node else 0), turn=seq)
        if writer:
            writer.set_turn(seq)
            writer.set_phase(cursor.phase)
            writer.append(
                "turn/start",
                {
                    "history_lines": history[-12:],
                    "cursor_phase": cursor.phase,
                    "case_step": 0 if cursor.phase == "prep" else (cur_node.n if cur_node else 0),
                },
            )

        if isinstance(cursor, StepCursor):
            cursor.bind_finish_context(ctx, history)
            if cursor.phase == "prep":
                ctx.prep_signal_done_history = list(history[-48:])
        if int(getattr(ctx, "otp_fetch_fail_streak", 0) or 0) >= 2:
            _log_turn_end(writer, cap="get_otp", status="fail")
            return _leave(
                status="blocked",
                summary="验证码邮件超时，环境未返回验证码，本步已停止。",
             reason_code="otp_environment_unavailable")

        if cursor.done:
            return _leave_case(cursor)

        ctx.decision_screenshot_hash = ""
        from mino_nexus.records.eventlog.writer import set_screenshot_hash

        set_screenshot_hash("")
        ctx.viewport_wh = (0, 0)
        shot = proxy.observe("screenshot", force_fresh=True)
        err = str(getattr(shot, "error", "") or "")
        # 「EXECUTE screenshot」是等 RESULT 超时：Scout 线程可能还占着通道，立刻再截会叠成下一次超时。
        if (
            not getattr(shot, "has_image", lambda: False)()
            and "timeout" in err
            and "EXECUTE" not in err
        ):
            time.sleep(0.6)
            shot = proxy.observe("screenshot", force_fresh=True)
        if int(getattr(shot, "width", 0) or 0) > 0 and int(getattr(shot, "height", 0) or 0) > 0:
            ctx.viewport_wh = (int(shot.width), int(shot.height))
        thumb = make_thumb(shot.image_base64) if shot.has_image() else ""
        if thumb:
            from mino_nexus.loop.observe.screen_store import publish_screen

            ref, shot_hash = publish_screen(
                str(getattr(writer, "session_id", "") or scout_run_id),
                f"t{seq}-decision",
                thumb,
            )
            if ref:
                thumb = ref
                ctx.decision_screenshot_hash = shot_hash
                from mino_nexus.records.eventlog.writer import set_screenshot_hash

                set_screenshot_hash(shot_hash)
        if writer and shot.has_image():
            import hashlib

            writer.append(
                "observe/screen",
                {
                    "w": shot.width or 0,
                    "h": shot.height or 0,
                    "mime": "image/jpeg",
                    "thumb": thumb,
                    "thumb_len": len(thumb or ""),
                    "screenshot_hash": str(getattr(ctx, "decision_screenshot_hash", "") or ""),
                    "raw_image_hash": hashlib.sha256(str(shot.image_base64).encode("ascii", errors="ignore")).hexdigest()[:20],
                    **{k: str((shot.remote_detail or {}).get(k) or "")[:128] for k in ("capture_id", "session_generation", "snapshot_id", "capture_scope")},
                    **{k: (shot.remote_detail or {})[k] for k in ("capture_width", "capture_height", "compress_ratio",
                                                                "pid", "window_id", "native_width", "native_height", "screenshot_scale")
                       if isinstance((shot.remote_detail or {}).get(k), (int, float))},
                    "source": shot.source,
                    "original_mime": shot.image_mime,
                },
            )
        pending_step = getattr(ctx, "pending_result_step", None)
        if thumb and pending_step is not None and str(pending_step) != str(seq):
            emit(
                "thumb",
                step=pending_step,
                capability_id=str(getattr(ctx, "pending_result_cap", "") or ""),
                result_thumb=thumb,
                thumb_note="沿用下一回合的决策图",
            )
            ctx.pending_result_step = None
        if not shot.has_image():
            summary = shot.error or "Scout 截图失败"
            if "任务已取消" in summary:
                _log_turn_end(writer, cap="", status="cancelled")
                return _leave(status="cancelled", summary=summary, reason_code="task_cancelled")
            if "没有打开的页面" in summary:
                summary = "浏览器还没有打开页面。请确认应用填了 Web 地址，或在前置里先打开网址。"
            emit("observe", thought=summary, step=seq, status="fail")
            _log_turn_end(writer, cap="", status="fail")
            return _leave(status="fail", summary=summary, reason_code="observation_missing")

        from mino_nexus.loop.device_display_guard import platform_needs_display_guard
        from mino_nexus.loop.screen_capture import analyze_shot

        if platform_needs_display_guard(ctx):
            facts = analyze_shot(shot)
            if facts.get("capture_ok") == "no" or facts.get("capture_black") == "yes":
                screen_out = recover_if_needed(
                    ctx, proxy, target_package=target_pkg, shot=shot, execute_only=True,
                )
                missing = _screen_unregistered(screen_out)
                if missing:
                    _log_turn_end(writer, cap="wake_screen", status="blocked")
                    return _leave(status="blocked", summary=missing, reason_code="execution_contract_blocked")
                if screen_out is not None and (screen_out.applied or screen_out.recovered):
                    shot = proxy.observe("screenshot", force_fresh=True)

        from mino_nexus.action_space.scheme import action_scheme as _action_scheme, select_observation

        if _action_scheme(ctx) == "visual":
            inspect_slots["hierarchy_text"] = ""
            inspect_slots["nav_assist"] = ""
            ctx.nav_hierarchy_nodes = []
            if writer:
                writer.append(
                    "nav/localize",
                    {"turn_id": seq, "skipped": "visual", "result": "skip"},
                )
        elif nav is not None:
            nav.observe(
                proxy,
                ctx=ctx,
                turn_id=seq,
                slot_sink=inspect_slots,
                session_block=inspect_slots.get("session_block") or "",
                screenshot_turn_id=seq,
                writer=writer,
                screenshot=shot,
            )
            _snap_wf = getattr(getattr(nav, "snapshot", None), "web_focus", None)
            if isinstance(_snap_wf, dict) and _snap_wf:
                ctx.web_focus = dict(_snap_wf)
            _sync_app_foreground_ctx(ctx, nav, target_pkg, proxy)
        elif not is_explore:
            _sync_app_foreground_ctx(ctx, nav, target_pkg, proxy)

        select_observation(ctx, cursor=cursor, writer=writer)
        proxy.observation_mode = ctx.observation_mode
        if isinstance(cursor, StepCursor):
            from mino_nexus.navigation.login.transition import remember_submit_frame
            remember_submit_frame(cursor, ctx, shot, writer=writer)

        if isinstance(cursor, StepCursor):
            from mino_nexus.loop.recovery_permission import try_recover_pending_system_permission

            permission_recovery = try_recover_pending_system_permission(
                proxy, ctx, cursor, turn_seq=seq, writer=writer,
            )
            if permission_recovery:
                recovery_summary = str(permission_recovery.get("summary") or "系统权限恢复")
                if not permission_recovery.get("ok"):
                    return _leave_execution_blocked(seq, "tap_element", recovery_summary,
                                                    "system_permission_recovery_unresolved")
                rec(seq, capability_id="tap_element", status="wait", summary=recovery_summary,
                    thought="独立系统权限恢复；原业务动作仍待回看", executor_used="router_proxy")
                emit("result", step=seq, capability_id="tap_element", status="wait",
                     summary=recovery_summary, thought="系统权限动作已派发；下一回合重新观察原业务结果",
                     event_kind="system_permission_recovery")
                _log_turn_end(writer, cap="tap_element", status="defer")
                continue

        if isinstance(cursor, StepCursor):
            from mino_nexus.loop.visual_review import settle_pending_review

            from mino_nexus.loop.login_evidence_navigation import observe_navigation_step
            if observe_navigation_step(cursor, ctx, shot, writer=writer):
                _log_turn_end(writer, cap="", status="pending")
                continue

            # 持久策略 dom_preferred 不代表本轮仍是 DOM；截图待回看必须交给视觉裁定。
            from mino_nexus.action_space.scheme import execution_scheme as _review_scheme

            if _review_scheme(ctx) != 'visual':
                review = settle_pending_review(
                    cursor,
                    ctx,
                    writer=writer,
                    in_prep=str(cursor.phase or "") == "prep",
                    in_check=str(cursor.phase or "") == "check",
                )
                stop = str(getattr(cursor, "semantic_stop", "") or "").strip()
                if review == "stop" or stop:
                    return _leave(status="fail", summary=(stop or "回看未通过")[:500], reason_code="action_result_unverified")

        ran_case_start_inspection = run_phase_inspections(
            ctx=ctx,
            cursor=cursor,
            inspections=inspections,
            shot=shot,
            provider_id=provider_id,
            case=case,
            inspect_slots=inspect_slots,
            login_module_case=login_module_case,
            ran_case_start_inspection=ran_case_start_inspection,
            last_phase_seen=last_phase_seen,
            run_inspections=run_inspections,
        )

        if cursor.phase != last_phase_seen:
            if writer:
                writer.append(
                    "phase/change",
                    {"from": last_phase_seen, "to": cursor.phase, "trigger": "phase_enter"},
                )
                writer.set_phase(cursor.phase)
            last_phase_seen = cursor.phase

        from mino_nexus.loop.observe.session_persist import effective_session_block, execution_context_session_line

        if isinstance(cursor, StepCursor) and cursor.check_session_refresh:
            cursor.check_session_refresh = False
        inspect_slots["session_block"] = effective_session_block(
            ctx, str(inspect_slots.get("session_block") or "")
        )
        reconcile_session_block_with_hierarchy(ctx, inspect_slots, nav, case)
        inspect_slots["session_block"] = effective_session_block(
            ctx, str(inspect_slots.get("session_block") or "")
        )
        inspect_slots["session_execution"] = execution_context_session_line(ctx)
        from mino_nexus.casework.account_facet_commit import record_session_probe

        record_session_probe(ctx, str(inspect_slots.get("session_block") or ""))

        if not is_explore and str(ctx.platform).lower() == "android" and cursor.phase == "do":
            from mino_nexus.resources.session.scene_gate import required_session
            from mino_nexus.navigation.login.evidence import can_commit
            if required_session(scene=ctx.case_scene) == "logged_in" and not can_commit(ctx):
                writer.append("login/completion_blocked", {
                    "reason_code": "login_identity_unverified", "status": "env_blocked",
                })
                return _leave(status="env_blocked", reason_code="login_identity_unverified",
                              summary="业务前置要求已登录，但当前租约账号身份尚未核实")

        cursor.login_session_hint = compile_login_session_hint(
            getattr(ctx, "case_scene", None),
            inspect_slots.get("session_block") or "",
        )

        if nav is not None:
            if is_explore:
                if nav.snapshot.usable():
                    cursor.note_observation(nav.snapshot.nodes)
                else:
                    cursor.note_observation([])
                if cursor.done:
                    return _leave(status="pass", summary=cursor.summary(), pack=_pack(), reason_code="required_assertions_consumed")
            blocked = nav.preflight_block()
            if blocked:
                reason = str(blocked.get("reason") or "租号初态与用例要求不一致")
                if writer:
                    writer.append("turn/end", {"decision_cap": "nav_precondition", "decision_status": "blocked"})
                return _leave(status="blocked", summary=reason, reason_code="execution_contract_blocked")
        elif is_explore:
            cursor.note_observation([])
            if cursor.done:
                return _leave(status="pass", summary=cursor.summary(), pack=_pack(), reason_code="required_assertions_consumed")

        if not is_explore and cursor.phase in ("prep", "do"):
            _fg = ensure_foreground(
                ctx=ctx,
                proxy=proxy,
                cursor=cursor,
                nav=nav,
                is_explore=is_explore,
                clear_before_launch=bool(_clear_before_launch),
                target_pkg=target_pkg,
                scout_run_id=scout_run_id,
                case_seq=case_seq,
                seq=seq,
                shot=shot,
                thumb=thumb,
                writer=writer,
                rec=rec,
                emit=emit,
                leave=_leave,
            )
            if _fg.leave is not None:
                return _fg.leave
            if _fg.cont:
                continue

        screen_fp_turn = _screen_fp(
            shot, inspect_slots.get("hierarchy_text") or "", ctx=ctx
        )
        if not is_explore and isinstance(cursor, StepCursor):
            probe_visual_prior(
                is_explore=is_explore,
                cursor=cursor,
                screen_fp_turn=screen_fp_turn,
                nav=nav,
                ctx=ctx,
                seq=seq,
                cid=cid,
                scout_run_id=scout_run_id,
            )

        cur = cursor.current()
        if cur is None:
            if is_explore:
                return _leave(status="pass", summary=cursor.summary(), pack=_pack(), reason_code="required_assertions_consumed")
            return _leave_case(cursor)

        if not is_explore and cur is not None:
            prev_step_n = int(getattr(ctx, "_run_case_step_n", 0) or 0)
            if int(cur.n) != prev_step_n:
                ctx._run_case_step_n = int(cur.n)
                update_auth_state(ctx, login_flow_macro_done=set())
                ctx.login_macro_yielded = None
                setattr(cursor, "vision_assert_done", False)

        phase_cfg = _phase_cfg()
        from mino_nexus.loop.vision_flags import bind_sop_phase_cfg

        bind_sop_phase_cfg(phase_cfg)
        phase_tool_kinds = merge_phase_tool_kinds(phase_cfg, sop)
        menu = available_menu_brief(
            ctx,
            kind="agent",
            phase=cursor.phase,
            platform=str(getattr(ctx, "platform", "") or ""),
            tool_kinds=phase_tool_kinds or None,
        )
        if (
            not is_explore
            and isinstance(cursor, StepCursor)
            and cursor.phase == "prep"
        ):
            from mino_nexus.resources.resource_preflight import claim_requires_clear_cache

            prec_menu = str(getattr(cursor, "precondition", "") or case.get("precondition") or "")
            scene_prep = dict(getattr(ctx, "case_scene", None) or {})
            rk_prep = case.get("resource_key") if isinstance(case.get("resource_key"), dict) else None
            if claim_requires_clear_cache(rk_prep, scene_prep, prec_menu):
                cleared = bool(getattr(ctx, "prep_clear_done", False))
                if not cleared:
                    menu = [
                        c
                        for c in menu
                        if str(c.get("id") or "") not in ("launch_app", "open_app", "open_url")
                    ]
                    cursor.correction_hint = (
                        cursor.correction_hint
                        or "【前置顺序】本条须先 clear_app_cache 成功，再 launch_app；"
                        "未清缓存前菜单已隐藏打开应用，避免落在桌面/设置。"
                    )
        if nav is not None:
            ctx.nav_localized_state = str(nav.localized.get("chosen") or "")
            ctx.nav_localized_confidence = float(nav.localized.get("confidence") or 0.0)
            ctx.nav_localized = dict(nav.localized or {})
            ctx.nav_project_id = str(nav.project_id or "")
            snap = getattr(nav, "snapshot", None)
            nodes = list(getattr(snap, "nodes", None) or []) if snap is not None else []
            ctx.nav_hierarchy_nodes = nodes
        from mino_nexus.loop.nav_onboarding_open_loop import (
            clear_open_loop_if_tabs_visible,
            format_open_loop_hint,
        )

        clear_open_loop_if_tabs_visible(ctx)
        if (
            not is_explore
            and isinstance(cursor, StepCursor)
            and cursor.phase == "do"
            and cur
        ):
            _prec_open = str(
                getattr(cursor, "precondition", "") or case.get("precondition") or ""
            )
            _ol = format_open_loop_hint(
                ctx=ctx,
                precondition=_prec_open,
                instruction=str(cur.instruction or ""),
            )
            if _ol:
                cursor.correction_hint = (
                    f"{cursor.correction_hint}\n{_ol}".strip()
                    if cursor.correction_hint
                    else _ol
                )
        if (
            not is_explore
            and isinstance(cursor, StepCursor)
            and cursor.phase == "do"
            and cur
        ):
            refresh_step_navigation(
                is_explore=is_explore,
                cursor=cursor,
                cur=cur,
                ctx=ctx,
                case=case,
            )
        if nav is None or not nav.active:
            menu = [
                c for c in menu
                if str(c.get("id") or "") not in ("fsm_navigate", "recover_fsm_navigate")
            ]
        if str(getattr(ctx, "app_version", "") or "").strip():
            menu = [c for c in menu if str(c.get("id") or "") != "get_app_version"]
        from mino_nexus.loop.recovery import resolve_app_foreground_guard

        fg_menu = resolve_app_foreground_guard(
            ctx,
            proxy,
            nodes=list(getattr(ctx, "nav_hierarchy_nodes", None) or []),
            target_package=str(getattr(ctx, "target_package", "") or target_pkg),
        )
        ctx.system_overlay = str(fg_menu.get("system_overlay") or "")
        ctx.app_foreground = str(fg_menu.get("app_foreground") or "")
        if str(fg_menu.get("app_foreground") or "") == "yes":
            menu = [
                c for c in menu
                if str(c.get("id") or "")
                not in (
                    "launch_app",
                    "open_app",
                    "open_url",
                    "run_flow_block",
                )
            ]
        from mino_nexus.loop.step_contract import instruction_allows_login_flow

        _allows_login_step = instruction_allows_login_flow(
            str(cur.instruction or "") if cur else "",
            login_module_case=bool(login_module_case),
        )
        if _allows_login_step or getattr(ctx, "login_flow_interrupt", False):
            _macro_hide = {"request_sms_code", "accept_legal_consent"}
            from mino_nexus.loop.milestones import (
                exclusive_hook_caps_from_state,
                read_state,
            )

            _macro_hide |= exclusive_hook_caps_from_state(read_state(cursor))
            menu = [c for c in menu if str(c.get("id") or "") not in _macro_hide]
        if writer:
            writer.append(
                "context/menu",
                {
                    "phase": cursor.phase,
                    "tool_kinds": list(phase_tool_kinds or []),
                    "cap_ids": [str(c.get("id") or "") for c in menu if c.get("id")],
                },
            )
        cursor.otp_prep_hint = ""
        if cursor.phase in ("prep", "do"):
            prep_hints: list[str] = []
            otp_line = compile_otp_prep_hint(
                accounts_brief=str(getattr(ctx, "accounts_brief", "") or ""),
                session_block=str(inspect_slots.get("session_block") or ""),
                hierarchy_text=str(inspect_slots.get("hierarchy_text") or ""),
                has_get_otp=any(str(c.get("id") or "") == "get_otp" for c in menu),
                has_hitl=any(str(c.get("id") or "").startswith("human_") for c in menu),
            )
            if otp_line:
                prep_hints.append(otp_line)
            from mino_nexus.contract.execution.ui import ui_channel_from_ctx, ui_channel_label

            from mino_nexus.loop.login_verification import compile_verification_send_menu_hint

            sms_line = compile_verification_send_menu_hint(
                ctx=ctx,
                accounts_brief=str(getattr(ctx, "accounts_brief", "") or ""),
                hierarchy_nodes=list(getattr(ctx, "nav_hierarchy_nodes", None) or []),
                has_tap_element=any(str(c.get("id") or "") == "tap_element" for c in menu),
                ui_channel=ui_channel_label(ui_channel_from_ctx(ctx)),
            )
            if sms_line:
                prep_hints.append(sms_line)
            cursor.otp_prep_hint = "\n".join(prep_hints)

        if not is_explore and isinstance(cursor, StepCursor):
            from mino_nexus.loop.step_flow_scope import login_flow_allowed

            _ins = str(cur.instruction or "") if cur else ""
            _exp = str(cur.expected or "") if cur else ""
            _lf_hint_ok, _lf_hint_msg = login_flow_allowed(
                cursor=cursor,
                phase=cursor.phase,
                instruction=_ins,
                expected=_exp,
            )
            if _lf_hint_msg:
                # 一般范围提示不能覆盖上一动作的合同错误/补答原因；每轮去重，不增长历史。
                previous_hint = str(getattr(cursor, "correction_hint", "") or "").strip()
                if _lf_hint_msg not in previous_hint:
                    cursor.correction_hint = "\n".join(part for part in (previous_hint, _lf_hint_msg) if part)

        if not is_explore and isinstance(cursor, StepCursor) and cursor.phase in ("prep", "do") and cur:
            _login = run_login_nav(
                ctx=ctx,
                cursor=cursor,
                cur=cur,
                case=case,
                history=history,
                proxy=proxy,
                seq=seq,
                writer=writer,
                shot=shot,
                thumb=thumb,
                scout_run_id=scout_run_id,
                target_pkg=target_pkg,
                case_seq=case_seq,
                login_module_case=login_module_case,
                screen_fp_turn=screen_fp_turn,
                allows_login_step=_allows_login_step,
                cancel_check=cancel_check,
                required_execution_blocker=_required_execution_blocker,
                rec=rec,
                emit=emit,
                leave_blocked=_leave_execution_blocked,
                leave=_leave,
            )
            thumb = _login.thumb
            if _login.leave is not None:
                return _login.leave
            if _login.leave_case:
                return _leave_case(cursor)
            if _login.cont:
                continue

        in_prep = cursor.phase == "prep"
        in_check = cursor.phase == "check"
        from mino_nexus.loop.milestones import (
            apply_decision_milestones,
            apply_step_outcome,
            ensure_milestones_before_decide,
            evaluate_milestones,
        )

        check_needs_visual = False
        if in_check and cur and str(cur.expected or "").strip() and not cursor.step_checked:
            # 校验只看图。层级节点用来在前置/操作里点控件，不拿预期原文做终裁。
            check_needs_visual = True
        use_milestone_check = bool(
            True
            and in_check
            and cur
            and str(cur.expected or "").strip()
        )
        scripted_check = bool(
            in_check
            and cur
            and str(cur.expected or "").strip()
            and not cursor.step_checked
            and not use_milestone_check
        )
        intel_pack = context_pack_for_step(
            ctx=ctx,
            case=case,
            cursor=cursor,
            history=history,
            steps=steps,
            hierarchy_text=inspect_slots.get("hierarchy_text") or "",
            state_id=str(getattr(ctx, "nav_localized_state", "") or ""),
            policy="wiki_first",
            scripted_check=scripted_check,
        )
        inspect_slots.update(intel_pack.to_slots())
        know_rows = intel_pack.know_rows
        auto_knowledge_body = intel_pack.knowledge_body
        log_context_pack(writer, intel_pack)
        pg = getattr(cursor, "progress_gate", None)
        stuck_signal = False
        if pg is not None:
            if int(getattr(pg, "fuse_block_streak", 0) or 0) > 0:
                stuck_signal = True
            if int(getattr(pg, "no_progress_streak", 0) or 0) >= 2:
                stuck_signal = True
        if stuck_signal and doc_stuck_used < doc_stuck_budget:
            stuck_block = match_stuck_docs(
                ctx=ctx,
                case=case,
                cursor=cursor,
                history=history,
                steps=steps,
                hierarchy_text=inspect_slots.get("hierarchy_text") or "",
            )
            if stuck_block:
                doc_stuck_used += 1
                base = inspect_slots.get("doc_context") or ""
                merged = f"{base}\n\n【遇阻文档提示】\n{stuck_block}".strip() if base else stuck_block
                inspect_slots["doc_context"] = merged[:2400]

        from mino_nexus.loop.prep_program_plan import (
            seed_prep_milestones_from_claim,
            sync_prep_internal_milestones,
        )

        if str(getattr(cursor, "phase", "") or "").strip().lower() == "prep":
            seed_prep_milestones_from_claim(cursor, ctx, case, writer=writer)
            sync_prep_internal_milestones(cursor, ctx)
        elif str(getattr(cursor, "phase", "") or "").strip().lower() == "do":
            from mino_nexus.loop.do_program_plan import seed_do_milestones_from_plan

            seed_do_milestones_from_plan(
                cursor,
                ctx,
                case,
                writer=writer,
                app_id=str(getattr(ctx, "app_id", "") or ""),
            )
        ensure_milestones_before_decide(
            cursor,
            ctx,
            app_id=str(getattr(ctx, "app_id", "") or ""),
            writer=writer,
            case=case,
        )
        blocked_step = _required_execution_blocker()
        if blocked_step:
            return _leave_execution_blocked(
                seq, str(blocked_step.get("hook_cap") or blocked_step.get("device_cap") or "program"),
                f"{blocked_step.get('title') or blocked_step.get('id')}：{blocked_step.get('evidence')}",
                str(blocked_step.get("error_kind") or "capability_unavailable"),
            )

        _log_context_slots(
            writer,
            cursor=cursor,
            menu=menu,
            phase_tool_kinds=phase_tool_kinds,
            inspect_slots=inspect_slots,
            history=history,
            steps=steps,
            ctx=ctx,
        )
        if cancel_check and cancel_check():
            return _leave(status="cancelled", summary="任务已取消", reason_code="task_cancelled")
        # 决策与校验是同一条流水线的两个分支（校验分支判定脚本/里程碑断言，else 分支让模型决策）。
        # 抽取到 check_phase 时把 if/else 一起搬了进去，这里必须无条件调用，否则 prep/do 回合根本
        # 不会产生 decision，走到下面的 decision.thought 就是 UnboundLocalError（2026-10-22）。
        decision = None
        _check = run_check_phase(
            ctx=ctx,
            cursor=cursor,
            cur=cur,
            resolved_case=resolved_case,
            history=history,
            inspect_slots=inspect_slots,
            phase_tool_kinds=phase_tool_kinds,
            steps=steps,
            proxy=proxy,
            seq=seq,
            scout_run_id=scout_run_id,
            provider_id=provider_id,
            writer=writer,
            shot=shot,
            thumb=thumb,
            auto_knowledge_body=auto_knowledge_body,
            know_rows=know_rows,
            scripted_check=scripted_check,
            use_milestone_check=use_milestone_check,
            check_needs_visual=check_needs_visual,
            rec=rec,
            emit=emit,
            leave_blocked=_leave_execution_blocked,
            leave=_leave,
        )
        if _check.decision is not None:
            decision = _check.decision
        shot = _check.shot
        thumb = _check.thumb
        if _check.leave is not None:
            return _check.leave
        if _check.leave_case:
            return _leave_case(cursor)
        if _check.cont:
            continue
        if decision is None:
            return _leave_execution_blocked(seq, "model", "本轮没有产生可执行决策", "decision_missing")
        thought = decision.thought or ""
        trace = _llm_trace(decision)
        _meta_llm = ((decision.raw_llm or {}).get("meta") if isinstance(decision.raw_llm, dict) else None) or {}
        _wasted = int(_meta_llm.get("wasted_ms") or 0) if isinstance(_meta_llm, dict) else 0
        if _wasted > 0:
            ctx.case_budget_credit_sec = float(getattr(ctx, "case_budget_credit_sec", 0) or 0) + _wasted / 1000.0
            if writer:
                writer.append("budget/credit", {
                    "reason": "llm_empty_tool_calls_retry", "wasted_ms": _wasted,
                    "credit_sec": round(float(getattr(ctx, "case_budget_credit_sec", 0) or 0), 1),
                    "cap_sec": _BUDGET_CREDIT_CAP_SEC,
                })
        emit(
            "think",
            thought=thought, step=seq, goal=cursor.decide_goal(),
            status=decision.status, thumb=thumb,
            confidence=decision.confidence,
            event_kind="decision",
            execution_status="not_dispatched",
            **trace,
        )
        if nav is not None and decision.screen_layout:
            nav.attach_turn_layout(seq, decision.screen_layout)
        vlm_h = decision.vlm_hierarchy if isinstance(decision.vlm_hierarchy, dict) else {}
        if vlm_h.get("nodes"):
            ctx.nav_vlm_hierarchy = vlm_h
            if nav is not None:
                nav._pending_vlm_hierarchy = vlm_h
                nav.attach_turn_vlm_hierarchy(seq, vlm_h)
            if writer:
                tops = [
                    str(n.get("text") or n.get("content_desc") or "")[:24]
                    for n in (vlm_h.get("nodes") or [])[:6]
                    if isinstance(n, dict)
                ]
                writer.append(
                    "llm/decision",
                    {
                        "vlm_hierarchy_nodes": len(vlm_h.get("nodes") or []),
                        "vlm_hierarchy_top": [t for t in tops if t],
                        "degraded_scout": bool(vlm_h.get("degraded_scout")),
                    },
                )

        screen_fp = _screen_fp(shot, inspect_slots.get("hierarchy_text") or "", ctx=ctx)

        if decision.status in ("give_up", "ask_human", "skip"):
            _stop = run_model_stop(
                ctx=ctx,
                cursor=cursor,
                cur=cur,
                decision=decision,
                trace=trace,
                is_explore=is_explore,
                seq=seq,
                thought=thought,
                thumb=thumb,
                writer=writer,
                rec=rec,
                emit=emit,
                leave=_leave,
            )
            if _stop.leave is not None:
                return _stop.leave

        action = decision.action
        cap_id = str(action.capability_id) if action and action.capability_id else ""
        if cap_id == "signal_done":
            decision.status = "done"
        if str(decision.status or "") == "done":
            cap_id = "signal_done"
            params = {}
            action = AgentAction(capability_id="signal_done", params={})
        turn_decision_cap = cap_id or ("signal_done" if decision.status == "done" else "")
        turn_decision_status = str(decision.status or "")
        params = dict(action.params or {}) if action else {}
        from mino_nexus.catalog.tool_schema import fill_target_package
        from mino_nexus.resources.credential_fill import fill_input_text_from_ctx

        params = fill_target_package(
            params,
            cap_id=cap_id,
            target_package=str(getattr(ctx, "target_package", "") or target_pkg),
        )
        params = fill_input_text_from_ctx(params, cap_id=cap_id, ctx=ctx)
        from mino_nexus.loop.device_execute_params import prepare_device_execute_params

        params = prepare_device_execute_params(cap_id, params, ctx)
        if isinstance(cursor, StepCursor):
            _prior = apply_turn_prior(
                ctx=ctx,
                cursor=cursor,
                decision=decision,
                writer=writer,
                cap_id=cap_id,
                seq=seq,
                in_prep=in_prep,
                in_check=in_check,
                proxy=proxy,
                scout_run_id=scout_run_id,
                history=history,
                cancel_check=cancel_check,
                rec=rec,
                emit=emit,
                leave_blocked=_leave_execution_blocked,
                leave=_leave,
            )
            if _prior.leave is not None:
                return _prior.leave
            if _prior.leave_case:
                return _leave_case(cursor)
            if _prior.cont:
                continue
        if cap_id == "tap_element" and isinstance(cursor, StepCursor):
            from mino_nexus.action_space.scheme import (
                action_scheme as configured_action_scheme,
                execution_scheme as execution_scheme,
            )
            from mino_nexus.loop.milestone_orchestrator import in_progress_milestone
            from mino_nexus.loop.milestones import read_state

            consent_focus = in_progress_milestone(read_state(cursor)) or {}
            if (configured_action_scheme(ctx) == "dom_preferred" and execution_scheme(ctx) == "visual"
                    and consent_focus.get("id") == "legal_consent"
                    and consent_focus.get("converted_from_hook") == "operation.check_box"):
                from mino_nexus.navigation.geometry.ui_consent import find_consent_control, tap_params_for_control

                consent_nodes = list(getattr(ctx, "nav_hierarchy_nodes", None) or [])
                consent_control = find_consent_control(consent_nodes, require_unique=True)
                if consent_control is not None:
                    # 无语义锚点的自绘复选框仍有当前几何边界。只校正本次尚未派发的协议落点；
                    # 后续新截图回看继续裁定勾选结果，不把坐标命中或 tap pass 当作完成。
                    consent_point = tap_params_for_control(consent_control, consent_nodes)
                    params.update({key: consent_point[key] for key in ("x", "y", "fallback_xy")})
                    if writer:
                        writer.append("consent/target", {
                            "milestone_id": "legal_consent", "source": "current_observation_geometry",
                            "bounds": list(consent_control.get("bounds") or []),
                            "x": params["x"], "y": params["y"], "execution_started": False,
                            "persisted": False, "review_required": True,
                        })
        if cap_id == "swipe_direction" and params:
            from mino_nexus.contract.coords import prepare_xy_params_for_execute

            prepare_xy_params_for_execute(
                params,
                int(getattr(shot, "width", 0) or 0),
                int(getattr(shot, "height", 0) or 0),
            )
        from mino_nexus.loop.step_phase_fsm import milestone_phase_active
        from mino_nexus.loop.fuse.thought_done import should_coerce_mutate_to_signal_done

        if (
            not milestone_phase_active()
            and should_coerce_mutate_to_signal_done(
                thought=thought,
                cap_id=cap_id,
                phase=str(cursor.phase or ""),
            )
        ):
            if writer:
                writer.append(
                    "decision/thought_done_coerce",
                    {"from_cap": cap_id, "thought": str(thought or "")[:240]},
                )
            cap_id = "signal_done"
            params = {}
            action = AgentAction(capability_id="signal_done", params={})
            turn_decision_cap = "signal_done"
            decision.status = "done"
            if isinstance(cursor, StepCursor) and cur:
                from mino_nexus.loop import step_effect as step_effect_mod

                _vlm_coerce = (
                    decision.vlm_hierarchy if isinstance(decision.vlm_hierarchy, dict) else None
                )
                step_effect_mod.maybe_mark_deferred_nav_tab(
                    cursor,
                    instruction=str(cur.instruction or ""),
                    expected=str(cur.expected or ""),
                    localized=dict(getattr(ctx, "nav_localized", None) or {}),
                    nodes=_effect_nodes(nav, ctx, extra_vlm=_vlm_coerce),
                    thought=thought,
                    coerce_done=True,
                )
        fg_nodes: list[dict[str, Any]] = []
        if nav is not None and getattr(nav, "snapshot", None) is not None:
            snap_nodes = getattr(nav.snapshot, "nodes", None) or []
            if snap_nodes:
                fg_nodes = list(snap_nodes)
        from mino_nexus.loop.recovery import resolve_app_foreground_guard

        fg_ctx = resolve_app_foreground_guard(
            ctx,
            proxy,
            nodes=fg_nodes,
            target_package=target_pkg,
        )
        guard_ctx = build_guard_ctx(
            ctx=ctx,
            cursor=cursor,
            cap_id=cap_id,
            params=params,
            cur=cur,
            screen_fp=screen_fp,
            phase_cfg=phase_cfg,
            history=history,
            menu=menu,
            task_env_brief=task_env_brief,
            fg_ctx=fg_ctx,
            fg_menu=fg_menu,
            inspect_slots=inspect_slots,
            case=case,
            thought=thought,
            login_module_case=login_module_case,
            target_pkg=target_pkg,
            llm_session_block=llm_session_block,
        )
        note_phase_contract(writer=writer, cap_id=cap_id, phase=cursor.phase)
        params = apply_force_case_expectation(params, guard_ctx)
        if cap_id == "tap_element":
            tap_nodes: list[dict[str, Any]] = []
            if nav is not None and getattr(nav, "snapshot", None) is not None:
                tap_nodes = [n for n in (nav.snapshot.nodes or []) if isinstance(n, dict)]
            if not tap_nodes:
                tap_nodes = [n for n in (getattr(ctx, "nav_hierarchy_nodes", None) or []) if isinstance(n, dict)]
            vlm = getattr(ctx, "nav_vlm_hierarchy", None)
            extra_nodes = list((vlm or {}).get("nodes") or []) if isinstance(vlm, dict) else []
            hint = " ".join(
                x
                for x in (
                    thought,
                    str(cur.instruction or "") if cur else "",
                    str((cur.expected if cur else "") or ""),
                )
                if str(x or "").strip()
            )
            from mino_nexus.contract.coords import prepare_xy_params_for_execute

            if params.get("intent_dom"):
                from mino_nexus.contract.coords import lift_selector_target
                from mino_nexus.loop.tap_enrich import enrich_tap_params

                params = enrich_tap_params(params, tap_nodes, hint=hint, extra_nodes=extra_nodes)
                lift_selector_target(params)
            prepare_xy_params_for_execute(
                params,
                int(getattr(shot, "width", 0) or 0),
                int(getattr(shot, "height", 0) or 0),
            )
        pending_mutate = bool(decision.status == "done" and cap_id in MUTATE_CAPS and action)

        if decision.status == "done" and not pending_mutate:
            _done = run_model_done(
                ctx=ctx,
                cursor=cursor,
                cur=cur,
                case=case,
                decision=decision,
                guard_ctx=guard_ctx,
                phase_cfg=phase_cfg,
                history=history,
                nav=nav,
                cap_id=cap_id,
                case_id=cid,
                seq=seq,
                thought=thought,
                thumb=thumb,
                scout_run_id=scout_run_id,
                writer=writer,
                in_prep=in_prep,
                in_check=in_check,
                is_explore=is_explore,
                run_guards=run_guards,
                rec=rec,
                emit=emit,
                pack=_pack,
                leave=_leave,
            )
            if _done.leave is not None:
                return _done.leave
            if _done.leave_case:
                return _leave_case(cursor)
            if _done.cont:
                continue

        if not cap_id:
            summary = thought or "模型没有给出下一步动作"
            return _leave(status="fail", summary=summary, reason_code="model_action_missing")

        # GuardGate 与 ProgressGate 并行独立计数（§8.2），所以放在 phase guards 之前单独判。
        if nav is not None:
            _navv = run_nav_verdict(
                ctx=ctx,
                cursor=cursor,
                cur=cur,
                nav=nav,
                cap_id=cap_id,
                params=params,
                thought=thought,
                thumb=thumb,
                writer=writer,
                seq=seq,
                run_type=run_type,
                rec=rec,
                emit=emit,
                leave=_leave,
            )
            if _navv.leave is not None:
                return _navv.leave
            if _navv.cont:
                continue

        from mino_nexus.loop.fuse.dispatch_gate import GuardVerdict
        from mino_nexus.loop.flow_block_guards import (
            evaluate_flow_block_guards,
            filter_registry_guards_for_milestones,
        )
        from mino_nexus.loop.registry import run_guards_verdict

        from mino_nexus.loop.focus_turn import try_satisfy_focus_instead_of_repeat

        if isinstance(cursor, StepCursor) and try_satisfy_focus_instead_of_repeat(
            cursor, cap_id, guard_ctx, writer
        ):
            _log_turn_end(writer, cap=cap_id, status="pass")
            continue
        _guard_names = filter_registry_guards_for_milestones(
            list(phase_cfg.get("guards") or []),
            cursor,
        )
        guard_vision_remediate: AgentDecision | None = None
        _fb_guard = evaluate_flow_block_guards(guard_ctx, cursor)
        if _fb_guard is not None and not _fb_guard.allowed:
            guard_verdict = GuardVerdict(
                False,
                _fb_guard.guard_id,
                _fb_guard.dispatch_gate_code or _fb_guard.code,
                _fb_guard.reason,
            )
        else:
            guard_verdict = run_guards_verdict(_guard_names, guard_ctx)
        skip_reason = guard_verdict.reason if not guard_verdict.allowed else None
        if skip_reason:
            _skip = run_guard_skip(
                ctx=ctx,
                cursor=cursor,
                cur=cur,
                case=case,
                guard_ctx=guard_ctx,
                guard_verdict=guard_verdict,
                phase_tool_kinds=phase_tool_kinds,
                history=history,
                inspect_slots=inspect_slots,
                login_module_case=login_module_case,
                steps=steps,
                proxy=proxy,
                cap_id=cap_id,
                seq=seq,
                thought=thought,
                thumb=thumb,
                screen_fp=screen_fp,
                shot=shot,
                scout_run_id=scout_run_id,
                provider_id=provider_id,
                writer=writer,
                rec=rec,
                emit=emit,
                leave=_leave,
            )
            guard_vision_remediate = _skip.remediate
            if _skip.leave is not None:
                return _skip.leave
            if _skip.leave_case:
                return _leave_case(cursor)
            if _skip.cont:
                continue

        if guard_vision_remediate is not None:
            decision = guard_vision_remediate
            from mino_nexus.catalog.tool_schema import REPORT_SCREEN_OBSTACLE

            if decision.action and decision.action.capability_id == REPORT_SCREEN_OBSTACLE:
                from mino_nexus.loop.vision_plan import handle_execution_obstacle

                obstacle_plan = handle_execution_obstacle(cursor, ctx, decision, writer=writer, shot=shot)
                if obstacle_plan.blocked_error:
                    problem = obstacle_plan.blocked_error
                    return _leave_execution_blocked(
                        seq, REPORT_SCREEN_OBSTACLE, str(problem.get("error") or "挡屏恢复不可用"),
                        str(problem.get("fail_kind") or "screen_obstacle_unavailable"),
                    )
                _log_turn_end(writer, cap=REPORT_SCREEN_OBSTACLE, status="pending")
                continue
            thought = str(decision.thought or thought)
            action = decision.action
            cap_id = str(action.capability_id) if action and action.capability_id else ""
            params = dict(action.params or {}) if action else {}
            from mino_nexus.catalog.tool_schema import fill_target_package
            from mino_nexus.resources.credential_fill import fill_input_text_from_ctx
            from mino_nexus.loop.device_execute_params import prepare_device_execute_params

            params = fill_target_package(
                params,
                cap_id=cap_id,
                target_package=str(getattr(ctx, "target_package", "") or target_pkg),
            )
            params = fill_input_text_from_ctx(params, cap_id=cap_id, ctx=ctx)
            params = prepare_device_execute_params(cap_id, params, ctx)
            trace = _llm_trace(decision)

        if cap_id == "press_key" and not (isinstance(params.get("key") or params.get("keycode"), str)
                and str(params.get("key") or params.get("keycode") or "").strip()):
            rejected = int(getattr(cursor, "missing_key_reasks", 0) or 0) + 1
            cursor.missing_key_reasks = rejected
            cursor.correction_hint = "press_key 缺少必需 params.key；本次未执行。根据当前图选择正确导航并给出完整参数。"
            if writer:
                writer.append("contract/reask" if rejected == 1 else "contract/blocked", {
                    "reason_code": "missing_key_parameter", "capability_id": cap_id,
                    "execution_started": False, "attempt": rejected})
            if rejected > 1:
                return _leave_execution_blocked(seq, cap_id, "连续缺少按键参数，未执行设备动作", "invalid_action_params")
            _log_turn_end(writer, cap=cap_id, status="skipped")
            continue
        if cap_id == "press_key":
            cursor.missing_key_reasks = 0
        if cap_id in (
            "tap_element",
            "long_press_element",
            "multi_tap",
            "input_text",
            "swipe_direction",
            "swipe_element_to_element",
        ):
            _tp = run_tap_params(
                ctx=ctx,
                cursor=cursor,
                cap_id=cap_id,
                params=params,
                shot=shot,
                seq=seq,
                writer=writer,
                emit=emit,
                leave=_leave,
            )
            params = _tp.params
            if _tp.leave is not None:
                return _tp.leave
            if _tp.cont:
                continue
        locator_block = _locator_blocks_action(ctx, cap_id, params if isinstance(params, dict) else {}, cursor)
        if locator_block:
            if writer:
                writer.append("control/locator", {"status": "pending", "summary": locator_block, "capability_id": cap_id})
            return _leave(status="fail", summary=locator_block[:500], reason_code="target_resolution_failed")
        if cap_id:
            from mino_nexus.loop.business_contract import business_contract_block

            business_block = business_contract_block(
                cursor,
                app_id=str(getattr(ctx, "app_id", "") or case.get("app_id") or ""),
                project_id=str(getattr(ctx, "project_id", "") or case.get("project_id") or ""),
                case_step=0 if in_prep else int(cur.n),
                capability_id=cap_id,
            )
            if business_block:
                if writer:
                    writer.append("contract/blocked", business_block)
                missing = ", ".join(business_block["missing_milestones"])
                return _leave(status="blocked", summary=f"业务规则前置未满足：{missing}（来源 {business_block['source_id']}）", reason_code="environment_precondition_unmet")
        emit(
            "step",
            thought=thought, step=seq, status=decision.status, thumb=thumb,
            action={"capability_id": cap_id, "params": params},
            capability_id=cap_id,
            **trace,
        )
        event = PlanEvent(
            seq=seq,
            capability_id=cap_id,
            event_kind=cap_id,
            params=params,
            ai_reasoning=thought,
            label=thought[:80],
            case_step_index=None if in_prep else cur.n,
        )
        if writer:
            writer.append(
                "tool/call",
                {"capability_id": cap_id, "params": params},
            )
        recovery_action = False
        if isinstance(cursor, StepCursor):
            from mino_nexus.loop.fuse.interrupt_stack import stack_depth
            from mino_nexus.loop.milestone_orchestrator import in_progress_milestone
            from mino_nexus.loop.milestones import read_state

            action_focus = in_progress_milestone(read_state(cursor)) or {}
            recovery_action = stack_depth(cursor) > 0 or action_focus.get("source") == "screen_recovery"
        result = _run_action(
            event=event,
            shot=shot,
            proxy=proxy,
            ctx=ctx,
            scout_run_id=scout_run_id,
            case_seq=case_seq,
            seq=seq,
        )
        recovery_extra: dict[str, Any] = {}
        outcome = resolve_turn_outcome(
            result=result,
            cap_id=cap_id,
            params=params,
            cursor=cursor,
            recovery_extra=recovery_extra,
        )
        status_val = outcome.status_val
        summary = outcome.summary
        elapsed_ms = outcome.elapsed_ms
        error = outcome.error
        executor_used = outcome.executor_used

        raw_resp = getattr(result, "raw_response", None)
        if isinstance(raw_resp, dict):
            note_nav_degraded(
                ctx=ctx,
                cursor=cursor,
                cur=cur,
                params=params,
                case=case,
                raw_resp=raw_resp,
                writer=writer,
            )

        if cap_id in ("fsm_navigate", "recover_fsm_navigate"):
            note_fsm_navigation(
                ctx=ctx,
                cursor=cursor,
                cur=cur,
                cap_id=cap_id,
                params=params,
                status_val=status_val,
                summary=summary,
                screen_fp_turn=screen_fp_turn,
                decision=decision,
                nav=nav,
                thought=thought,
                raw_resp=raw_resp,
            )

        if status_val == "pass" and cap_id == "tap_element" and isinstance(params, dict):
            from mino_nexus.loop.device_execute_params import email_tap_lands_on_otp
            from mino_nexus.loop.login_verification import is_web_email_login

            from mino_nexus.loop.visual_review import input_focus_field

            tap_input_field = input_focus_field(cursor, ctx) or str(
                params.get("login_field") or params.get("field") or ""
            ).strip().lower()
            if (
                tap_input_field in ("email", "login_email")
                and is_web_email_login(ctx)
                and email_tap_lands_on_otp(ctx, params)
            ):
                status_val = "fail"
                error = "点在了验证码框上。请改点邮箱输入框，再填写邮箱。"
                summary = error
                if isinstance(cursor, StepCursor):
                    cursor.correction_hint = error
        if writer:
            payload = {
                "capability_id": cap_id,
                "status": status_val,
                "summary": summary,
                "error": error,
                "executor_used": executor_used,
                "elapsed_ms": elapsed_ms,
                **execution_result_metadata(result),
            }
            if isinstance(raw_resp, dict) and raw_resp.get("nav_attempt"):
                payload["nav_attempt"] = raw_resp.get("nav_attempt")
            if recovery_extra:
                payload.update(recovery_extra)
            writer.append("tool/result", payload)
            _note_control_locator(writer, ctx, cap_id, params if isinstance(params, dict) else {}, cursor)
        if writer and hasattr(result, "recovered"):
            writer.append(
                "recovery/match",
                {
                    "rule_id": cap_id,
                    "status": status_val,
                    "summary": summary,
                    "source": "tool",
                    "recovered": bool(getattr(result, "recovered", False)),
                },
            )
        extra: dict[str, Any] = {}
        if cap_id == "assert_visual":
            extra = {"case_step_index": cur.n, "expectation": cur.expected}
        elif cap_id == "tap_element":
            sel = str(params.get("selector_text") or "").strip()
            if sel:
                extra["selector_text"] = sel
        if recovery_extra:
            extra = {**(extra or {}), **recovery_extra}
        if isinstance(raw_resp, dict) and raw_resp.get("nav_attempt"):
            extra = {**(extra or {}), "nav_attempt": raw_resp.get("nav_attempt")}
        redo_turn = bool(getattr(cursor, "visual_turn_pending", False))
        verification_pending = isinstance(raw_resp, dict) and raw_resp.get("verification_required") is True
        shown_status = "pending" if (redo_turn or verification_pending) and status_val == "pass" else status_val
        rec(
            seq,
            capability_id=event.capability_id, status=shown_status,
            summary=summary, error=error,
            executor_used=executor_used,
            elapsed_ms=elapsed_ms, thought=thought, thumb=thumb, extra=extra or None,
        )
        emit(
            "result",
            thought=thought, step=seq, action={"capability_id": cap_id, "params": params},
            capability_id=cap_id, status=shown_status, result_status=status_val,
            summary=summary, elapsed_ms=elapsed_ms, thumb=thumb,
            executor_used=executor_used,
            device_dispatched=True,
            execution_kind="device",
        )
        if status_val == "pass" and cap_id == "input_text":
            from mino_nexus.loop.program_device_fallback import clear_program_vision_fallback

            clear_program_vision_fallback(cursor)
        if cap_id == "input_text" and isinstance(getattr(cursor, "program_vision_fallback", None), dict):
            cursor.vision_fallback_last_execution = {
                "status": status_val,
                "has_coordinates": params.get("x") is not None and params.get("y") is not None,
            }

        if getattr(result, "stop_run", False) or hasattr(result, "status") and result.status in (EventStatus.BLOCKED,):
            if writer:
                writer.append(
                    "hitl/blocked",
                    {
                        "capability_id": cap_id,
                        "summary": error or summary or "被阻塞",
                        "executor_used": executor_used,
                    },
                )
            return _leave(status="blocked", summary=error or summary or "被阻塞", reason_code="execution_contract_blocked")
        if hasattr(result, "status") and result.status in (EventStatus.FAIL, EventStatus.DECLINED):
            _fail_flow = handle_fail_branch(
                ctx=ctx,
                cursor=cursor,
                cur=cur,
                cap_id=cap_id,
                params=params,
                summary=summary,
                error=error,
                status_val=status_val,
                result=result,
                event=event,
                in_check=in_check,
                writer=writer,
                seq=seq,
                scout_run_id=scout_run_id,
                leave=_leave,
            )
            if _fail_flow.leave is not None:
                return _fail_flow.leave
            if _fail_flow.cont:
                continue

        if nav is not None:
            nav.after_execute(cap_id=cap_id, params=params, status=status_val, writer=writer)
            if cap_id == "assert_visual":
                layout = dict((getattr(result, "vlm_meta", None) or {}).get("screen_layout") or {})
                if layout:
                    nav.attach_turn_layout(seq, layout)

        if (
            status_val == "pass"
            and cap_clears_repeat_tap(cap_id)
        ):
            cursor.clear_repeat_tap()
        from mino_nexus.loop.recovery import invalidate_evidence_cache

        invalidate_evidence_cache(ctx)  # 动作后设备状态已变，旧取证不可复用
        if status_val == "pass" and cap_id == "tap_element":
            bind_tap_focus(
                status_val=status_val,
                cap_id=cap_id,
                ctx=ctx,
                cursor=cursor,
                params=params,
                result=result,
                writer=writer,
                scout_run_id=scout_run_id,
            )
        if status_val == "pass" and cap_id in (
            "launch_app", "open_url", "open_app", "fsm_navigate", "reload_page", "back", "switch_window",
        ):
            ctx.focus_action_ref = ""
            ctx.focus_action_field = ""
            ctx.focus_action_scheme = ""
        if cap_id in ("tap_element", "multi_tap", "long_press_element", "swipe_direction",
                      "swipe_element_to_element", "press_key", "input_text") and not (
            cap_id == "tap_element" and status_val == "pass"
        ):
            # 点击失败、其他触摸/键盘动作以及一次写入后均不能沿用旧焦点。
            ctx.focus_action_ref = ""
            ctx.focus_action_field = ""
            ctx.focus_action_scheme = ""

        if isinstance(cursor, StepCursor):
            from mino_nexus.loop.milestone_orchestrator import in_progress_milestone_id
            from mino_nexus.navigation.login.policy import clear_submission
            binding_focus = in_progress_milestone_id(cursor)
            if ((binding_focus == "account_fill" and cap_id == "input_text")
                    or (binding_focus == "submit_login" and status_val != "pass")
                    or cap_id == "clear_app_cache"):
                clear_submission(ctx)
        if status_val == "pass" and cap_id == "clear_app_cache":
            ctx.prep_clear_done = True
        # 必须在 note_tool_pass 之前：它可能因阶段迁移 cont=True 直接 continue，
        # 跳过这里就会让 preparation_gate 判 prepare_launch_unverified（2026-10-22 实测）。
        if status_val == "pass" and cap_id in ("launch_app", "open_app", "open_url"):
            ctx.app_launch_confirmed = True
            from mino_nexus.loop.launch_grace import stamp_launch_grace
            from mino_nexus.runtime.run_context import is_web_slot
            from mino_nexus.navigation.atlas.nav_target_scope import _looks_like_url

            stamp_launch_grace(ctx)
            if is_web_slot(str(getattr(ctx, "sn", "") or ""), str(getattr(ctx, "platform", "") or "")):
                tgt = str(getattr(ctx, "target_package", "") or "").strip()
                if tgt and _looks_like_url(tgt):
                    ctx.app_foreground = "yes"

        if status_val == 'pass' and isinstance(cursor, StepCursor):
            _pass_flow = note_tool_pass(
                ctx=ctx,
                cursor=cursor,
                proxy=proxy,
                cap_id=cap_id,
                params=params,
                summary=summary,
                status_val=status_val,
                writer=writer,
                redo_turn=redo_turn,
                in_prep=in_prep,
                in_check=in_check,
                leave=_leave,
            )
            if _pass_flow.leave is not None:
                return _pass_flow.leave
            if _pass_flow.leave_case:
                return _leave_case(cursor)
            if _pass_flow.cont:
                continue

        if (
            status_val == "pass"
            and cap_id in PROGRESS_CAPS
            and not recovery_action
            and not str(cap_id).startswith("recover_") and cap_id != "run_flow_block"
        ):
            if note_progress_and_auto_finish(
                ctx=ctx,
                cursor=cursor,
                cap_id=cap_id,
                params=params,
                status_val=status_val,
                summary=summary,
                result=result,
                recovery_action=recovery_action,
                history=history,
                seq=seq,
                writer=writer,
                thumb=thumb,
                emit=emit,
                rec=rec,
            ):
                continue
        if (
            status_val == "pass"
            and cap_id == "lease_account"
            and isinstance(cursor, StepCursor)
            and cursor.phase == "prep"
        ):
            note_lease_account(
                ctx=ctx,
                cursor=cursor,
                cap_id=cap_id,
                status_val=status_val,
                in_prep=in_prep,
                inspect_slots=inspect_slots,
            )
        if status_val == "pass" and cap_id == "press_key":
            key = str((params or {}).get("key") or "").strip().upper()
            if key in ("BACK", "BACK_KEY"):
                ctx.recovery_allow_back = False

        if status_val == "pass" and cap_id == "tap_element":
            from mino_nexus.loop.nav_onboarding_open_loop import clear_open_loop_if_tabs_visible

            clear_open_loop_if_tabs_visible(ctx)
        post_fp = ""
        from mino_nexus.action_space.scheme import execution_scheme

        visual_scheme = execution_scheme(ctx) == "visual"
        if status_val == "pass" and (fuseable_cap(cap_id) or cap_id in ("fsm_navigate", "recover_fsm_navigate")):
            record_progress_pass(
                ctx=ctx,
                proxy=proxy,
                cursor=cursor,
                cur=cur,
                cap_id=cap_id,
                params=params,
                status_val=status_val,
                screen_fp=screen_fp,
                visual_scheme=visual_scheme,
                login_module_case=login_module_case,
                inspect_slots=inspect_slots,
            )

        if (
            status_val == "pass"
            and cap_id == "request_sms_code"
            and isinstance(cursor, StepCursor)
            and cursor.phase == "do"
            and cur
        ):
            cursor.login_post_sms_pending = True
            cursor.login_post_sms_stall = 0
            cursor.step_intents_done.add("sms_send")
            update_auth_state(ctx, login_auto_submit_attempts=0)
            cursor.correction_hint = (
                "验证码已请求发送；下一轮程序先取码，再点验证码框，点中后按该落点输入。"
                "本 turn 勿重复发码。"
            )

        if (
            status_val == "pass"
            and cap_id == "tap_element"
            and isinstance(cursor, StepCursor)
            and cursor.phase == "do"
            and cur
        ):
            _tap_flow = handle_login_tap(
                ctx=ctx,
                proxy=proxy,
                cursor=cursor,
                cur=cur,
                case=case,
                cap_id=cap_id,
                params=params,
                status_val=status_val,
                summary=summary,
                post_fp=post_fp,
                screen_fp=screen_fp,
                history=history,
                seq=seq,
                writer=writer,
                thumb=thumb,
                login_module_case=login_module_case,
                rec=rec,
                emit=emit,
                leave=_leave,
            )
            if _tap_flow.leave is not None:
                return _tap_flow.leave
            if _tap_flow.cont:
                continue

        if cap_id == "tap_element" and getattr(result, "status", None) == EventStatus.PASS:
            if not post_fp and not visual_scheme:
                post_shot = proxy.observe("screenshot", force_fresh=True)
                post_fp = _screen_fp(
                    post_shot, inspect_slots.get("hierarchy_text") or "", ctx=ctx
                )
            cursor.remember_tap(
                params,
                screen_fp=post_fp,
                selector_text=str(params.get("selector_text") or ""),
            )
            cursor.mark_guest_entry(summary)

        if cap_id == "assert_visual" and status_val == "pass":
            cursor.mark_checked()
            if not cursor.advance():
                return _leave_case(cursor)
            _log_turn_end(writer, cap=cap_id, status=status_val)
            continue

        _sd = handle_signal_done(
            ctx=ctx,
            cursor=cursor,
            cur=cur,
            cap_id=cap_id,
            status_val=status_val,
            decision=decision,
            nav=nav,
            writer=writer,
            thought=thought,
        )
        if _sd is Signal.LEAVE_CASE:
            return _leave_case(cursor)
        if _sd is Signal.CONTINUE:
            continue

        if decision.status == "done":
            if is_explore:
                cursor.mark_explore_done()
                return _leave(status="pass", summary=cursor.summary(), pack=_pack(), reason_code="required_assertions_consumed")
            if in_prep:
                cursor.finish_prep()
            elif in_check:
                if cursor.step_checked and not cursor.advance():
                    return _leave_case(cursor)
            else:
                cursor.enter_check()
            _log_turn_end(writer, cap=turn_decision_cap or cap_id, status=turn_decision_status or status_val)
            continue

        _log_turn_end(writer, cap=cap_id, status=status_val)

    if is_explore:
        return _leave(status="pass", summary=cursor.summary(), pack=_pack(), reason_code="required_assertions_consumed")
    time_exhausted = wall_budget_sec > 0 and time.time() >= float(getattr(ctx, "case_deadline_ts", 0) or float("inf"))
    if time_exhausted:
        summary = f"超过 {wall_budget_sec} 秒仍未完成"
    else:
        summary = f"超过 {max_steps} 步仍未完成"
    return _leave(status="fail", summary=summary,
                  reason_code="case_budget_exhausted" if time_exhausted else "case_step_budget_exhausted")
