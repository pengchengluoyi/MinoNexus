"""预检：冷启动/打开应用、版本探针、登录提示、截止时间。

这一段的返回只有两种：继续（带回 `fixed_deadline`）或 `aborted`（由调用方 `return`）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mino_nexus.core.log import SLog
from mino_nexus.loop.channel_program_recovery import channel_supports_capability
from mino_nexus.loop.launch_grace import stamp_launch_grace
from mino_nexus.loop.execution.state import RunState
from mino_nexus.loop.login_session_hint import compile_login_session_hint
from mino_nexus.core.schemas import PlanEvent
from mino_nexus.loop.execution.page_ready import (
    _ensure_desktop_window,
    _ensure_web_page,
    _program_executor,
)
from mino_nexus.loop.milestone_orchestrator import ensure_single_in_progress
from mino_nexus.loop.recovery import ensure_target_app_foreground
from mino_nexus.resources.resource_preflight import claim_requires_clear_cache
from mino_nexus.loop.step_pointer import StepCursor
from mino_nexus.loop.web.web_env import frame_step
from mino_nexus.navigation.atlas.nav_route import load_fsm_doc
from mino_nexus.runtime.run_context import (
    execute_result_ok,
    is_web_slot,
    stamp_app_version,
    version_from_execute_result,
)

TAG = "AgentLoop"


@dataclass
class Preflight:
    aborted: dict[str, Any] | None = None
    fixed_deadline: float = 0.0
    clear_before_launch: bool = False


def run_preflight(st: RunState) -> Preflight:
    case = st.setup.case
    cursor = st.setup.cursor
    nav = st.setup.nav
    wall_budget_sec = st.setup.wall_budget_sec
    target_pkg = st.setup.target_pkg
    ctx = st.ctx
    proxy = st.proxy
    writer = st.writer
    emit = st.emit
    history = st.history
    scout_run_id = st.scout_run_id
    t0 = st.t0
    case_seq = st.case_seq
    resolved_case = st.resolved_case
    # 前置要先清数据时，preflight 不许把应用先拉起来（拉起后立刻会被 pm clear 杀掉）。
    _scene = getattr(ctx, "case_scene", None) or {}
    clear_before_launch = claim_requires_clear_cache(
        case.get("resource_key") if isinstance(case, dict) else None,
        _scene,
        str((case or {}).get("precondition") or ""),
    ) or (str(getattr(ctx, "platform", "") or "").strip().lower() == "android"
          and str((_scene or {}).get("required_session") or "") in ("guest", "logged_out"))
    login_module_case = st.login_module_case
    cancel_check = st.cancel_check
    _leave = st.leave
    rec = st.rec
    _screen_unregistered = st.screen_unregistered
    # 2026-10-22：取消「每次任务执行前先关一次、再启动一次」的开环。
    # 它和 prep 自己的 prep_launch_app 重复，而且会先启动再被 prep_clear_cache 杀掉：
    # api-1-218 实测出现 close→launch→clear→launch→launch 三次启动。
    # 现在应用由 prep_launch_app（程序里程碑）启动一次，开机态交给 prep 决定。
    opened = _ensure_desktop_window(proxy, ctx, run_id=scout_run_id, case_seq=case_seq, writer=writer)
    if opened is None:
        opened = _ensure_web_page(proxy, ctx, run_id=scout_run_id, case_seq=case_seq, writer=writer)
    if opened:
        opening_thought = str(opened.get("thought") or "打开被测应用")
        opening_params = dict(opened.get("params") or {})
        rec(
            0,
            capability_id="launch_app",
            status=str(opened.get("status") or "pass"),
            summary=str(opened.get("summary") or "打开页面"),
            thought=opening_thought,
            error=str(opened.get("error") or ""),
            executor_used=str(opened.get("executor_used") or "playwright"),
        )
        if writer:
            writer.append(
                "tool/result",
                {
                    "capability_id": "launch_app",
                    "status": str(opened.get("status") or "pass"),
                    "summary": str(opened.get("summary") or ""),
                    "error": str(opened.get("error") or ""),
                    "executor_used": str(opened.get("executor_used") or "playwright"),
                    **opened.get("execution_metadata", {}),
                },
            )
        if opened.get("fatal"):
            summary = opened.get("summary") or "打开被测应用失败"
            opening_status = "blocked" if opened.get("status") == "blocked" else "fail"
            emit("observe", thought=summary, step=0, status=opening_status)
            return Preflight(aborted=_leave(status=opening_status, summary=summary, reason_code="prepare_launch_unverified"))
        if str(opened.get("status") or "") in ("pass", "ok", "success"):
            ctx.app_launch_confirmed = True
            rec(
                0,
                capability_id="launch_app",
                status="pass",
                summary=str(opened.get("summary") or "打开页面"),
                thought=opening_thought,
                executor_used=str(opened.get("executor_used") or ""),
            )
            emit(
                "think",
                thought=opening_thought,
                step=0,
                capability_id="launch_app",
                status="continue",
            )
            emit(
                "step",
                thought=opening_thought,
                step=0,
                capability_id="launch_app",
                status="continue",
                action={
                    "capability_id": "launch_app",
                    "params": opening_params,
                },
            )
            emit(
                "result",
                thought=opening_thought,
                step=0,
                capability_id="launch_app",
                status="pass",
                result_status="pass",
                summary=str(opened.get("summary") or "打开页面"),
            )
            from mino_nexus.loop.prep_program_plan import sync_prep_internal_milestones

            if isinstance(cursor, StepCursor) and cursor.phase == "prep":
                sync_prep_internal_milestones(cursor, ctx)
                ensure_single_in_progress(cursor, writer=writer)

    from mino_nexus.loop.recovery import recover_if_needed


    preflight_shot = None
    try:
        from mino_nexus.runtime.run_context import is_web_slot

        if not is_web_slot(str(getattr(ctx, "sn", "") or ""), str(getattr(ctx, "platform", "") or "")):
            preflight_shot = proxy.observe("screenshot", force_fresh=True, diagnostic_purpose="preflight")
    except Exception:
        preflight_shot = None

    from mino_nexus.loop.input_contract import focus_input_block_reason

    _focus_block = focus_input_block_reason(ctx)
    if _focus_block:
        if writer:
            writer.append(
                "contract/blocked",
                {"code": "contract_unsupported", "reason": _focus_block[:300], "retryable": False},
            )
        return Preflight(aborted=_leave(status="blocked", summary=_focus_block, reason_code="focus_contract_unsupported"))

    def _log_preflight_recovery(out, *, source: str) -> None:
        cap_id = str(getattr(out, "rule_id", "") or "recovery")
        summary = out.summary() if callable(getattr(out, "summary", None)) else (
            getattr(out, "evidence", "") or getattr(out, "error", "") or cap_id
        )
        st = "blocked" if out.stop_run else "pass" if out.recovered else ("skipped" if out.mode == "advise" else "fail")
        rec(
            0,
            capability_id=cap_id,
            status=st,
            summary=summary,
            thought=summary,
            executor_used="recovery",
        )
        emit(
            "recovery",
            thought=summary,
            step=0,
            capability_id=cap_id,
            status=st,
            summary=summary,
        )
        if writer:
            writer.append(
                "recovery/match",
                {
                    "rule_id": cap_id,
                    "status": st,
                    "summary": summary,
                    "source": source,
                },
            )

    recovery_out = recover_if_needed(
        ctx,
        proxy,
        target_package=target_pkg,
        shot=preflight_shot,
        execute_only=True,
    )
    if recovery_out:
        _log_preflight_recovery(recovery_out, source="preflight")
        missing = _screen_unregistered(recovery_out)
        if missing:
            return Preflight(aborted=_leave(status="blocked", summary=missing, reason_code="execution_contract_blocked"))
        if recovery_out.stop_run:
            return Preflight(aborted=_leave(status="blocked", summary=recovery_out.summary(), reason_code="environment_precondition_unmet"))

    # 前置要先清数据时跳过前台确认：它会在 pm clear 之前把应用拉起来，纯属白启动。
    fg_out = None if clear_before_launch else ensure_target_app_foreground(
        ctx,
        proxy,
        target_package=target_pkg,
        shot=preflight_shot,
    )
    if fg_out:
        _log_preflight_recovery(fg_out, source="preflight_fg")
        if fg_out.stop_run:
            return Preflight(aborted=_leave(status="blocked", summary=fg_out.summary(), reason_code="environment_precondition_unmet"))

    from mino_nexus.loop.channel_program_recovery import channel_supports_capability

    version_supported = channel_supports_capability(ctx, "get_app_version")
    if target_pkg and not version_supported:
        SLog.i(TAG, f"[{scout_run_id}] get_app_version skipped platform={ctx.platform} reason=not_reported_by_device")
        if writer:
            writer.append("app/version", {"status": "skipped", "reason": "not_reported_by_device", "platform": ctx.platform})
    if not str(getattr(ctx, "app_version", "") or "").strip() and target_pkg and version_supported:
        from mino_nexus.runtime.run_context import (
            execute_result_ok,
            stamp_app_version,
            version_from_execute_result,
        )

        ver_event = PlanEvent(
            seq=0,
            capability_id="get_app_version",
            event_kind="get_app_version",
            params={"package": target_pkg},
            ai_reasoning="prep：记录被测应用版本供导航视图与采集分桶",
            label="读取应用版本",
        )
        ver_result = proxy.dispatch(ver_event, run_id=scout_run_id, step_idx=frame_step(case_seq, 1), diagnostic_purpose="app_version")
        raw_ver = ver_result.raw_response if isinstance(getattr(ver_result, "raw_response", None), dict) else {}
        ver = version_from_execute_result(raw_ver) if execute_result_ok(ver_result) else ""
        if ver:
            stamp_app_version(ctx, ver)
            sn_v = str(getattr(ctx, "sn", "") or "").strip()
            if sn_v and target_pkg:
                try:
                    from mino_nexus.resources.device_app_session_store import upsert_session

                    upsert_session(
                        sn_v,
                        target_pkg,
                        app_version=ver,
                        source="get_app_version",
                    )
                except Exception:
                    pass
            if writer:
                writer.append("app/version", {"app_version": ver, "package": target_pkg})
            history.append(
                f"0. program → info: 本任务应用版本已确认：{ver}"
                f"（包 {target_pkg}；无需看图，勿再调 get_app_version）"
            )
            if nav is not None:
                try:
                    from mino_nexus.navigation.atlas.nav_route import load_fsm_doc

                    app_id = str(getattr(ctx, "app_id", "") or "")
                    project_id = str(getattr(ctx, "project_id", "") or "")
                    if app_id:
                        fsm, _src = load_fsm_doc(
                            app_id,
                            project_id=project_id,
                            use_live=False,
                            app_version=ver,
                        )
                        if fsm:
                            nav.fsm = fsm
                except Exception:
                    pass

    if cancel_check and cancel_check():
        return Preflight(aborted=_leave(status="cancelled", summary="任务已取消", reason_code="task_cancelled"))

    if login_module_case:
        pre_hint = compile_login_session_hint(
            getattr(ctx, "case_scene", None),
            "",
        )
        if pre_hint:
            history.append(f"0. program → info: {pre_hint}")

    if wall_budget_sec > 0:
        ctx.case_deadline_ts = t0 + float(wall_budget_sec)
    fixed_deadline = resolved_case.deadline_at if resolved_case else float(case.get("attempt_deadline_at") or 0)
    if fixed_deadline:
        ctx.case_deadline_ts = min(ctx.case_deadline_ts or fixed_deadline, fixed_deadline)

    return Preflight(fixed_deadline=float(fixed_deadline or 0), clear_before_launch=bool(clear_before_launch))
