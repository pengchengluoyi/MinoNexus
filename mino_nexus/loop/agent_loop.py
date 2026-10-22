"""单用例看图闭环。prep / do / check 都由 Agent 看图决策。"""
from __future__ import annotations

from mino_nexus.loop.login_transition import update_auth_state

import hashlib
import re
import time
from pathlib import Path
from typing import Any, Optional

from mino_nexus.loop.fuse.action_fuse import fuseable_cap
from mino_nexus.loop.step_pointer import cap_clears_repeat_tap
from mino_nexus.ai.planner import decide_next_action
from mino_nexus.ai.schemas import AgentAction, AgentDecision
from mino_nexus.catalog.exec_classes import MUTATE_CAPS, PROGRESS_CAPS
from mino_nexus.loop.registry import apply_force_case_expectation, run_guards
from mino_nexus.loop.inspections import (
    expand_named_knowledge,
    llm_session_block,
    match_stuck_docs,
    reconcile_session_block_with_hierarchy,
    run_inspections,
)
from mino_nexus.services.app_intel import context_pack_for_step, log_context_pack
from mino_nexus.runtime.session_gate import (
    compile_login_session_hint,
    compile_otp_prep_hint,
    ensure_case_scene,
    is_login_module_case,
)
from mino_nexus.runtime.platform_gate import platform_skip_reason
from mino_nexus.loop.sop_runtime import merge_phase_tool_kinds, normalize_inspections, normalize_phases, phase_for_id
from mino_nexus.core.log import SLog
from mino_nexus.loop.observe.agent_stream import emit_agent_event, make_thumb
from mino_nexus.loop.observe.session_log import SessionWriter, bind_writer, open_session
from mino_nexus.loop.observe.execution_diagnostics import execution_result_metadata
from mino_nexus.loop.local_executors import dispatch_local, is_local_cap
from mino_nexus.loop.nav_runtime import NavRuntime
from mino_nexus.loop.recovery import apply_rule, ensure_target_app_foreground, recover_if_needed, RuleMatch
from mino_nexus.loop.router_proxy import RouterProxy
from mino_nexus.loop.observe.skill_trace import envelope as skill_envelope
from mino_nexus.loop.step_pointer import (
    StepCursor,
    _expected_defers_to_check,
    build_seq_nodes,
    compute_case_wall_budget_sec,
    enrich_assert_expectation,
    tap_params_guard_summary,
)
from mino_nexus.catalog import registry as catalog
from mino_nexus.core.protocol import EventStatus
from mino_nexus.services.run_store import line_text, report_run_id
from mino_nexus.runtime.run_context import bind_case_project, build_run_context
from mino_nexus.loop.web.web_env import agent_step_idx, cleanup_after_case, frame_step, reset_before_case
from mino_nexus.loop.android.app_env import reset_native_app_before_case
from mino_nexus.loop.channel_program_recovery import maybe_recover_foreground
from mino_nexus.core.schemas import EventResult, PlanEvent

EXECUTION_REVISION = "sha256:" + hashlib.sha256(Path(__file__).read_bytes()).hexdigest()

TAG = "AgentLoop"
_MAX_STEPS = 24
_TURN_SAFETY_CAP = 500
RECOVER_PREFIX = "recover_"
_NON_FATAL_LOCAL_REASONS = frozenset(
    {"local_cap_misrouted", "cap_not_in_catalog", "no_impl_for_device", "fsm_degraded"}
)


def _case_overview(case: dict[str, Any]) -> str:
    name = str(case.get("name") or case.get("case_id") or "用例")
    steps = case.get("steps") if isinstance(case.get("steps"), list) else []
    body = "\n".join(t for t in (line_text(x) for x in steps) if t) or str(case.get("steps_raw") or "")
    bits = [name]
    if body:
        bits.append(f"步骤：{body}")
    pre = str(case.get("precondition") or "").strip()
    if pre:
        bits.append(f"前置：{pre}")
    return "\n".join(bits)


def _history(rows: list[str]) -> str:
    return "\n".join(rows[-12:]) if rows else "（还没有动作）"


def _history_line(
    seq: int,
    capability_id: str,
    status: str,
    summary: str,
    error: str,
    extra: Optional[dict[str, Any]] = None,
) -> str:
    text = str(summary or error or "")
    sel = str((extra or {}).get("selector_text") or "").strip()
    if capability_id == "tap_element" and sel and ("「?」" in text or sel not in text):
        tail = text
        if tail.startswith("点击 "):
            tail = tail[3:].lstrip()
        return f"{seq}. {capability_id} → {status}: 点击「{sel}」→ {tail}"
    return f"{seq}. {capability_id} → {status}: {text}"


def _sync_app_foreground_ctx(
    ctx,
    nav: Any,
    target_pkg: str,
    router: Any = None,
) -> dict[str, Any]:
    nodes: list[Any] = []
    if nav is not None:
        snap = getattr(nav, "snapshot", None)
        if snap is not None:
            nodes = list(getattr(snap, "nodes", None) or [])
    setattr(ctx, "nav_hierarchy_nodes", nodes)
    setattr(ctx, "observation_contract", dict(getattr(getattr(nav, "snapshot", None), "observation", None) or {}))
    from mino_nexus.loop.recovery import resolve_app_foreground_guard

    fg = dict(getattr(nav, "foreground", None) or {}) or resolve_app_foreground_guard(
        ctx,
        router,
        nodes=nodes,
        target_package=str(target_pkg or getattr(ctx, "target_package", "") or ""),
    )
    setattr(ctx, "system_overlay", str(fg.get("system_overlay") or ""))
    setattr(ctx, "app_foreground", str(fg.get("app_foreground") or ""))
    return fg


def _screen_fp(shot, hierarchy_text: str = "", *, ctx: Any = None) -> str:
    from mino_nexus.loop.channel_observation import progress_key

    return progress_key(
        ctx,
        nodes=list(getattr(ctx, "nav_hierarchy_nodes", None) or []) if ctx is not None else None,
        hierarchy_text=str(hierarchy_text or ""),
        image_base64=getattr(shot, "image_base64", "") or "",
        width=int(getattr(shot, "width", 0) or 0),
        height=int(getattr(shot, "height", 0) or 0),
    )


def _effect_nodes(nav, ctx, extra_vlm: Any = None) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    snap = getattr(nav, "snapshot", None) if nav is not None else None
    if snap is not None:
        nodes = [n for n in (getattr(snap, "nodes", None) or []) if isinstance(n, dict)]
    vlm = extra_vlm if isinstance(extra_vlm, dict) else getattr(ctx, "nav_vlm_hierarchy", None)
    vlm_nodes = list((vlm or {}).get("nodes") or []) if isinstance(vlm, dict) else []
    if not vlm_nodes:
        return nodes
    try:
        from mino_nexus.services.nav_vlm_hierarchy import merge_vlm_into_nodes

        merged = merge_vlm_into_nodes(nodes, vlm_nodes)
        return [n for n in merged if isinstance(n, dict)]
    except Exception as exc:  # noqa: BLE001
        SLog.w(TAG, f"merge vlm hierarchy failed: {type(exc).__name__}: {exc}")
        return nodes


def _elapsed(t0: float) -> int:
    return int((time.time() - t0) * 1000)


def _log_turn_end(
    writer: SessionWriter | None,
    *,
    cap: str,
    status: str,
    **extra: Any,
) -> None:
    if writer is None:
        return
    payload: dict[str, Any] = {"decision_cap": cap, "decision_status": status}
    payload.update(extra)
    writer.append("turn/end", payload)


def _locator_blocks_action(ctx: Any, cap_id: str, params: dict[str, Any], cursor: Any) -> str:
    # 整页滑动和刷新不以某个控件为目标，不能套控件定位门槛。
    if cap_id not in ("tap_element", "input_text"):
        return ""
    control_id, target_desc = _control_from_turn(cap_id, params, cursor)
    if not control_id:
        return ""
    from mino_nexus.action_space.scheme import execution_scheme as action_scheme
    from mino_nexus.services.control_registry import locator_stop_reason, runtime_readonly

    platform = str(getattr(ctx, "platform", "") or getattr(ctx, "device_platform", "") or "")
    ordinary = not runtime_readonly()
    if action_scheme(ctx) == "dom" and ordinary and cap_id in ("tap_element", "input_text"):
        from mino_nexus.action_space.scheme import structure_target_available
        from mino_nexus.services.control_registry import get_control

        project_id = str(getattr(ctx, "project_id", "") or "")
        control = get_control(project_id, control_id)
        scoped = bool(control) and str(control.get("project_id") or "") == project_id
        typed_locator = bool(control) and bool(control.get("locator_spec"))
        if scoped and not typed_locator and structure_target_available(cap_id, params, ctx):
            # 普通任务的本轮唯一结构锚点可以执行；不把文本/ref 冒充已保存的 CSS 定位。
            return ""
    dom_locator = str(params.get("selector") or params.get("css") or params.get("xpath") or params.get("resource_id") or "")
    return locator_stop_reason(
        project_id=str(getattr(ctx, "project_id", "") or ""),
        control_id=control_id,
        scheme=action_scheme(ctx),
        platform=platform,
        dom_locator=dom_locator,
        target_desc=target_desc,
    )


def _control_from_turn(cap_id: str, params: dict[str, Any], cursor: Any) -> tuple[str, str]:
    control_id = str(params.get("control_id") or "")
    target_desc = str(params.get("selector_text") or params.get("text") or "")
    from mino_nexus.loop.milestone_orchestrator import in_progress_milestone_id

    focus_id = in_progress_milestone_id(cursor)
    plan = getattr(cursor, "do_program_plan", None)
    if isinstance(plan, dict):
        for step in plan.get("steps") or []:
            if not isinstance(step, dict):
                continue
            if not focus_id or str(step.get("id") or "") != focus_id:
                continue
            if str(step.get("kind") or "") == "flow_block":
                # 逻辑块作者步骤的控件身份不能套到发码、输入、勾选等独立子事件。
                continue
            same = str(step.get("hook_cap") or "") == cap_id or (
                cap_id == "tap_element" and str(step.get("action_type") or "") in ("click", "check")
            )
            if not same:
                continue
            control_id = control_id or str(step.get("control_id") or "")
            target_desc = target_desc or str(step.get("target_desc") or "")
            break
    return control_id, target_desc


def _note_control_locator(writer: Any, ctx: Any, cap_id: str, params: dict[str, Any], cursor: Any) -> None:
    """按执行渠道和设备渠道记下用了哪一格定位。看图不把坐标写进控件。"""
    if writer is None or cap_id not in ("tap_element", "input_text"):
        return
    control_id, target_desc = _control_from_turn(cap_id, params, cursor)
    if not control_id:
        return
    from mino_nexus.action_space.scheme import execution_scheme as action_scheme
    from mino_nexus.services.control_registry import note_execution_locator

    platform = str(getattr(ctx, "platform", "") or getattr(ctx, "device_platform", "") or "")
    if action_scheme(ctx) == "dom":
        from mino_nexus.action_space.scheme import structure_target_available

        if structure_target_available(cap_id, params, ctx) and cap_id in ("tap_element", "input_text"):
            writer.append("control/locator", {
                "control_id": control_id, "scheme": "dom", "platform": platform,
                "resolved": True, "source": "current_observation", "persisted": False,
            })
            return
    dom_locator = str(params.get("selector") or params.get("css") or params.get("xpath") or "")
    note_execution_locator(
        writer,
        project_id=str(getattr(ctx, "project_id", "") or ""),
        control_id=control_id,
        scheme=action_scheme(ctx),
        platform=platform,
        dom_locator=dom_locator,
        target_desc=target_desc,
    )


def _program_executor(ctx: Any) -> str:
    from mino_nexus.contract.execution.ui import UiChannel, ui_channel_from_ctx

    channel = ui_channel_from_ctx(ctx)
    if channel == UiChannel.WEB:
        return "playwright"
    if channel == UiChannel.DESKTOP:
        return "desktop"
    if channel == UiChannel.IOS:
        return "ios_wda"
    return "adb"


def _log_program_tool(
    writer: SessionWriter | None,
    *,
    capability_id: str,
    params: dict[str, Any],
    status: str,
    summary: str,
    executor_used: str = "program",
    source: str = "program",
    execution_metadata: dict[str, Any] | None = None,
) -> None:
    if writer is None:
        return
    writer.append(
        "tool/call",
        {"capability_id": capability_id, "params": dict(params or {}), "source": source},
    )
    writer.append(
        "tool/result",
        {
            "capability_id": capability_id,
            "status": status,
            "summary": summary,
            "error": "",
            "executor_used": executor_used,
            "source": source,
            **(execution_metadata or {}),
        },
    )


# 模型空工具调用重试可抵扣的墙钟上限：防止 provider 持续异常时无限续命。
_BUDGET_CREDIT_CAP_SEC = 90.0


def _llm_trace(decision: AgentDecision) -> dict[str, Any]:
    raw = decision.raw_llm if isinstance(decision.raw_llm, dict) else {}
    meta = raw.get("meta") if isinstance(raw.get("meta"), dict) else {}
    out: dict[str, Any] = {}
    did = str(meta.get("dispatch_id") or "").strip()
    if did:
        out["dispatch_id"] = did
    if raw.get("llm_input") is not None:
        out["llm_input"] = raw.get("llm_input")
    if raw.get("llm_output") is not None:
        out["llm_output"] = raw.get("llm_output")
    if meta:
        out["llm_meta"] = meta
    return out


def _log_context_slots(
    writer: SessionWriter | None,
    *,
    cursor: StepCursor,
    menu: list[dict[str, Any]],
    phase_tool_kinds: list[str] | None,
    inspect_slots: dict[str, str],
    history: list[str],
    steps: list[dict[str, Any]],
    ctx: Any,
) -> None:
    if writer is None:
        return
    from mino_nexus.loop.llm_step_context import (
        build_accounts_json,
        build_session_json,
        history_block_scoped,
        log_context_trace,
        step_scope_key,
    )

    cur_node = cursor.current()
    case_step, phase = step_scope_key(cursor)
    hist_scoped = history_block_scoped(steps, case_step=case_step, phase=phase)
    sess_json = build_session_json(ctx, str(inspect_slots.get("session_block") or ""))
    acc_json = build_accounts_json(ctx)
    from mino_nexus.loop.llm_step_context import success_criteria_for_llm

    sc_obj = success_criteria_for_llm(cursor)
    llm_injection = {
        "goal": cursor.decide_goal(),
        "checkpoints_block": cursor.prompt_block(),
        "success_criteria": sc_obj,
        "session_json": sess_json,
        "accounts_json": acc_json,
        "history_block": hist_scoped,
        "hierarchy_text": inspect_slots.get("hierarchy_text") or "",
        "knowledge_hint": inspect_slots.get("knowledge_hint") or "",
        "knowledge_body": inspect_slots.get("knowledge_body") or "",
        "nav_assist": inspect_slots.get("nav_assist") or "",
        "doc_context": inspect_slots.get("doc_context") or "",
    }
    writer.append(
        "context/slots",
        {
            "slots": {
                "phase": cursor.phase,
                "case_step": case_step,
                **llm_injection,
                "tool_kinds": list(phase_tool_kinds or []),
                "menu_count": len(menu),
                "cap_ids": [str(c.get("id") or "") for c in menu if c.get("id")][:80],
            },
        },
    )
    log_context_trace(
        writer,
        cursor=cursor,
        ctx=ctx,
        menu=menu,
        history=history,
        steps=steps,
        inspect_slots=inspect_slots,
        phase_tool_kinds=phase_tool_kinds,
        llm_injection=llm_injection,
        checkpoints_block_full=cursor.prompt_block_full(),
    )


def _finish(emit, *, status: str, summary: str, steps: list, t0: float, pack=None) -> dict[str, Any]:
    extra = pack(status=status, summary=summary) if callable(pack) else {}
    emit("done", overall=status, status=status, summary=summary)
    from mino_nexus.loop.observe.session_project import _stamp_ui_status

    _stamp_ui_status(steps)
    out = {"status": status, "summary": summary, "steps": steps, "elapsed_ms": _elapsed(t0)}
    if extra:
        out.update({k: extra[k] for k in ("skill_id", "view_id", "slots") if k in extra})
    return out


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
    from mino_nexus.ai import dispatch_log as dispatch

    from mino_nexus.loop.step_contract import bind_run_case
    from mino_nexus.services.control_registry import registry_runtime

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
        from mino_nexus.services import run_store as _rs

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
        from mino_nexus.services.account_lease import restore_lease_for_run

        restored_row, _ = restore_lease_for_run(ctx, run_id) if not project_scope_error else (None, "")
        if restored_row or str(
            (getattr(ctx, "resource_lease", None) or {}).get("account_id") or ""
        ).strip():
            setattr(ctx, "prep_lease_account_done", True)
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
                setattr(ctx, "pending_result_step", extra.get("step"))
                setattr(ctx, "pending_result_cap", cap)
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
        from mino_nexus.services.skill_store import get_skill

        from mino_nexus.loop.sop_runtime import normalize_phases
        from mino_nexus.loop.vision_flags import sop_orchestration_snapshot

        _skill_row = get_skill("run-case") or {}
        _sop = _skill_row.get("sop") if isinstance(_skill_row.get("sop"), dict) else {}
        sop_orchestration = sop_orchestration_snapshot(normalize_phases(_sop.get("phases")))
    ordinary_contract_enabled = not is_explore_case and ctx.execution_mode == "agent" and ctx.platform in {"android", ""}
    from mino_nexus.loop.observe.session_log import SessionPersistenceError
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
        from mino_nexus.services.account_lease import release_ctx_lease

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
                from mino_nexus.services.execution_binding_runtime import BindingAdmissionError
                from mino_nexus.services.execution_requirements import ordinary_requirements
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
        SLog.e(TAG, f"run_case failed run={run_id} case={cid}: {summary}\n{traceback.format_exc()}")
        outcome = {
            "status": "fail",
            "summary": summary,
            "steps": steps,
        }
        return outcome
    finally:
        from mino_nexus.services.account_facet_commit import commit_case_facet_effects

        from mino_nexus.services.run_write_safety import protected

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
        from mino_nexus.services.account_lease import release_ctx_lease

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


def _record(
    steps: list[dict[str, Any]],
    history: list[str],
    seq: int,
    *,
    capability_id: str,
    status: str,
    summary: str,
    thought: str = "",
    error: str = "",
    executor_used: str = "",
    elapsed_ms: int = 0,
    thumb: str = "",
    extra: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    row = {
        "seq": seq,
        "step": seq,
        "capability_id": capability_id,
        "status": status,
        "summary": summary,
        "error": error,
        "executor_used": executor_used,
        "elapsed_ms": elapsed_ms,
        "thought": thought,
        "thumb": str(thumb or ""),
    }
    hist_extra = dict(extra or {})
    if extra:
        row.update(extra)
    steps.append(row)
    history.append(
        _history_line(
            seq,
            capability_id,
            status,
            summary,
            error,
            hist_extra,
        )
    )
    return row


def _progress_gate_web_kwargs(ctx: Any, proxy: Any) -> dict[str, Any]:
    from mino_nexus.contract.execution.ui import UiChannel, ui_channel_from_ctx
    from mino_nexus.loop.web.web_progress import refresh_web_focus, web_focus_fingerprint

    if ui_channel_from_ctx(ctx) != UiChannel.WEB:
        return {"web_channel": False, "web_focus_fp": ""}
    if proxy is not None:
        refresh_web_focus(ctx, proxy)
    return {
        "web_channel": True,
        "web_focus_fp": web_focus_fingerprint(getattr(ctx, "web_focus", None)),
    }


def _consume_login_chain(
    chain: list[dict[str, Any]],
    *,
    seq: int,
    cursor: StepCursor,
    rec,
    emit,
    thumb: str,
    screen_fp: str = "",
    login_flow_step: bool = False,
    fuse_web: dict[str, Any] | None = None,
    writer: Any = None,
) -> tuple[str, str]:
    last_cap, last_st = "", "fail"
    from mino_nexus.loop.fuse.action_fuse import fuseable_cap
    from mino_nexus.loop.observe.program_tool_log import log_program_tool
    from mino_nexus.loop.router_proxy import is_local_cap

    intents = set(getattr(cursor, "step_intents_done", None) or set())
    for item in chain:
        cap = str(item.get("capability_id") or "noop")
        st = str(item.get("status") or "fail")
        summ = str(item.get("summary") or "")
        if writer is not None and item.get("ran_get_otp"):
            log_program_tool(
                writer,
                capability_id="get_otp",
                params={},
                status="pass" if item.get("ok") else "fail",
                summary="程序链内 get_otp",
                executor_used="internal",
                source="login_chain",
            )
        if writer is not None and cap and not is_local_cap(cap):
            log_program_tool(
                writer,
                capability_id=cap,
                params=dict(item.get("params") or {}),
                status=st,
                summary=summ,
                executor_used="device",
                source="login_chain",
            )
        if item.get("ran_get_otp"):
            cursor.record_step_op("get_otp")
        if cap == "input_text" and item.get("ok"):
            fld = str((item.get("params") or {}).get("field") or "sms_code")
            cursor.record_step_op("input_text", params={"field": fld})
            if fld == "email":
                cursor.step_intents_done.add("login_email")
                setattr(cursor, "email_tab_guard_streak", 0)
        if item.get("step_id") == "send_code" and item.get("ok"):
            cursor.step_intents_done.add("sms_send")
        if cap == "tap_element" and item.get("ok"):
            cursor.record_step_op("tap_element", params={"selector_text": "登录"})
        if item.get("ok") and fuseable_cap(cap):
            params: dict[str, Any] = dict(item.get("params") or {})
            if cap == "input_text" and not params.get("field"):
                params = {"field": "sms_code"}
            elif cap == "tap_element":
                params = {"selector_text": "登录"}
            cursor.progress_gate.record_pass(
                cap_id=cap,
                params=params,
                pre_fp=screen_fp,
                post_fp=screen_fp,
                intents_done=set(getattr(cursor, "step_intents_done", None) or intents),
                login_flow_step=login_flow_step,
                **(fuse_web or {"web_channel": False, "web_focus_fp": ""}),
            )
        rec(seq, capability_id=cap, status=st, summary=summ, thought=summ, thumb=thumb)
        emit(
            "result",
            thought=summ,
            step=seq,
            capability_id=cap,
            status=st,
            summary=summ,
            thumb=thumb,
        )
        last_cap, last_st = cap, st
    return last_cap, last_st


def _login_chain_guard(
    chain: list[dict[str, Any]],
    *,
    cursor: StepCursor,
) -> str:
    """程序登录链全失败时累计 streak；误走本地 executor 时停用链。非空则 agent 应 fail 退出。"""
    if not chain:
        return ""
    if any(bool(x.get("ok")) for x in chain):
        cursor.login_chain_fail_streak = 0
        return ""
    streak = int(getattr(cursor, "login_chain_fail_streak", 0) or 0) + 1
    cursor.login_chain_fail_streak = streak
    if any("未知本地能力" in str(x.get("summary") or "") for x in chain):
        setattr(cursor, "login_chain_broken", True)
        cursor.correction_hint = (
            "程序登录链曾误将 input_text/tap 当本地能力执行；已停用程序链，请由模型正常派发设备能力。"
        )
        return ""
    if streak >= 8:
        head = str(chain[0].get("summary") or chain[0].get("capability_id") or "login_chain")
        return f"登录程序链连续失败 {streak} 轮：{head}"[:400]
    return ""


def _ensure_web_page(proxy: RouterProxy, ctx, *, run_id: str, case_seq: int = 0, writer=None) -> Optional[dict[str, Any]]:
    from mino_nexus.runtime.run_context import is_web_slot

    if not is_web_slot(str(getattr(ctx, "sn", "") or ""), str(getattr(ctx, "platform", "") or "")):
        return None
    url = str(getattr(ctx, "target_package", "") or "").strip()
    event = PlanEvent(
        seq=0,
        capability_id="launch_app",
        event_kind="launch_app",
        params={"url": url, "package": url} if url else {},
        ai_reasoning="Web 槽截图前先打开被测页",
        label="打开页面",
        expected_executor="playwright",
    )
    if writer:
        writer.append("tool/call", {"capability_id": event.capability_id, "params": event.params})
    result = proxy.dispatch(event, run_id=run_id, step_idx=frame_step(case_seq, 1))
    status_val = result.status.value if hasattr(result.status, "value") else str(result.status)
    err = result.error or result.summary or ""
    raw = result.raw_response if isinstance(getattr(result, "raw_response", None), dict) else {}
    local_reason = str(raw.get("local_reason") or "")
    fatal = status_val in ("fail", "failed", "declined", "blocked") and local_reason not in _NON_FATAL_LOCAL_REASONS
    return {
        "status": status_val,
        "summary": result.summary or (f"打开 {url}" if url else "打开空白页"),
        "error": result.error or "",
        "executor_used": result.executor_used or "playwright",
        "fatal": fatal,
        "execution_metadata": execution_result_metadata(result),
        "params": event.params,
        "thought": event.ai_reasoning,
    }


def _ensure_desktop_window(proxy: RouterProxy, ctx, *, run_id: str, case_seq: int = 0, writer=None) -> Optional[dict[str, Any]]:
    if _program_executor(ctx) != "desktop":
        return None
    package = str(getattr(ctx, "target_package", "") or "").strip()
    event = PlanEvent(
        seq=0, capability_id="launch_app", event_kind="launch_app", params={"package": package},
        ai_reasoning="PC 首张截图前先启动被测应用并确认目标窗口绑定", label="准备被测窗口",
        expected_executor="desktop",
    )
    if writer:
        writer.append("tool/call", {"capability_id": event.capability_id, "params": event.params})
    # Slot 4 is distinct from reset/cold-start (1..3) and app-version (1).
    result = proxy.dispatch(event, run_id=run_id, step_idx=frame_step(case_seq, 4))
    status = result.status.value if hasattr(result.status, "value") else str(result.status)
    return {
        "status": status, "summary": result.summary or "准备被测窗口",
        "error": result.error or "", "executor_used": result.executor_used or "desktop",
        "fatal": status != "pass", "execution_metadata": execution_result_metadata(result),
        "params": event.params, "thought": event.ai_reasoning,
    }



def _run_action(*, event: PlanEvent, shot, proxy: RouterProxy, ctx, scout_run_id: str, case_seq: int, seq: int) -> Any:
    from mino_nexus.loop.resolved_case import dispatch_gate
    dispatch_gate(ctx, event.capability_id)
    cap = event.capability_id
    from mino_nexus.catalog.skill_channel import capability_dispatch_ok

    if not capability_dispatch_ok(cap):
        return EventResult(
            seq=event.seq,
            capability_id=cap,
            event_kind=cap,
            status=EventStatus.FAIL,
            executor_used="local",
            summary=f"能力未启用或当前不可覆盖: {cap}",
            error="capability_disabled",
        )
    if cap.startswith(RECOVER_PREFIX):
        rule_id = cap[len(RECOVER_PREFIX):]
        rule = catalog.get_recovery_rule(rule_id)
        if rule is None:
            return EventResult(
                seq=event.seq,
                capability_id=cap,
                event_kind=cap,
                status=EventStatus.FAIL,
                executor_used="recovery",
                summary=f"没有恢复规则 {rule_id}",
                error="missing rule",
            )
        return apply_rule(
            RuleMatch(rule=rule, reasons=["agent"]),
            ctx, proxy,
            target_package=str(getattr(ctx, "target_package", "") or ""),
            agent_turn=seq,
        )
    return proxy.dispatch(
        event,
        run_id=scout_run_id,
        step_idx=agent_step_idx(case_seq, seq),
        ctx=ctx,
        shot=shot,
    )


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
    from mino_nexus.ai import dispatch_log as dispatch
    from mino_nexus.runtime.menu import available_menu_brief
    from mino_nexus.services.skill_store import get_skill

    if resolved_case is not None:
        case = resolved_case.case
    is_explore = str(run_type).lower() == "explore" or str(case.get("source") or "") == "explore"
    skill_id = "explore-app" if is_explore else "run-case"
    skill = get_skill(skill_id) or get_skill("run-case") or {}
    sop = skill.get("sop") if isinstance(skill.get("sop"), dict) else {}
    phases = normalize_phases(sop.get("phases"))
    inspections = normalize_inspections(sop.get("inspections") if "inspections" in sop else None)
    wall_budget_sec = 0
    try:
        if is_explore:
            cap = 200
            max_steps = max(1, min(cap, int(case.get("max_steps") or sop.get("max_steps") or _MAX_STEPS)))
        else:
            wall_budget_sec = compute_case_wall_budget_sec(case)
            max_steps = _TURN_SAFETY_CAP
    except (TypeError, ValueError):
        max_steps = 200 if is_explore else _TURN_SAFETY_CAP
        if not is_explore:
            wall_budget_sec = compute_case_wall_budget_sec(case)
    if resolved_case is not None:
        wall_budget_sec = resolved_case.wall_budget_sec
        max_steps = resolved_case.max_steps
    if is_explore:
        from mino_nexus.loop.explore_cursor import ExploreCursor

        goal = str(case.get("steps_raw") or (case.get("steps") or [""])[0] or overview).strip()
        cursor = ExploreCursor(
            goal=goal,
            success_criteria=str(case.get("success_criteria") or "").strip(),
            max_steps=max_steps,
            max_idle_steps=int(case.get("max_idle_steps") or sop.get("max_idle_steps") or 15),
        )
    else:
        cursor = StepCursor(
            build_seq_nodes(case),
            precondition=str(case.get("precondition") or "").strip(),
        )
    if resolved_case is not None:
        cursor.resolved_case = resolved_case
        cursor.execution_context = ctx
        cursor.verified_check_points = {}
        cursor.phase = "prep"
    setattr(ctx, "_step_cursor", cursor)
    from mino_nexus.loop.step_contract import bind_run_cursor

    bind_run_cursor(cursor)
    inspect_slots: dict[str, str] = {
        "session_block": "", "hierarchy_text": "", "knowledge_hint": "", "knowledge_body": "",
        "nav_assist": "", "doc_context": "",
    }
    try:
        from mino_nexus.services.doc_embed import bootstrap_case_doc_query

        _app_id = str(getattr(ctx, "app_id", "") or "").strip()
        _qvec = bootstrap_case_doc_query(case=case, app_id=_app_id)
        if _qvec:
            setattr(ctx, "doc_query_vec", _qvec)
    except Exception:
        pass
    doc_stuck_used = 0
    doc_stuck_budget = 2
    # 开关关着 / 没配 NavFSM 时为 None，主循环下面三处调用全部跳过（loop/nav_runtime.py 顶部注释）
    nav = NavRuntime.for_run(
        ctx=ctx,
        case=case,
        run_type=run_type,
        run_id=scout_run_id,
        provider_id=provider_id,
    )

    target_pkg = str(getattr(ctx, "target_package", "") or "")

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
    from mino_nexus.loop.session_ensure import ensure_case_account

    ensure_case_account(ctx, case)
    try:
        from mino_nexus.services import project_store as _ps
        from mino_nexus.services.otp_resolve import format_run_credential_hint

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
    from mino_nexus.services.resource_preflight import claim_requires_clear_cache

    _rk = case.get("resource_key") if isinstance(case.get("resource_key"), dict) else None
    _clear_before_launch = claim_requires_clear_cache(
        _rk,
        getattr(ctx, "case_scene", None),
        str(case.get("precondition") or ""),
    )
    cold_started = False if _program_executor(ctx) == "desktop" else reset_native_app_before_case(
        proxy,
        ctx,
        run_id=scout_run_id,
        case_seq=case_seq,
        case=case,
        login_module=login_module_case,
        defer_launch=_clear_before_launch,
    )
    if cold_started and writer:
        writer.append(
            "app/cold_start",
            {
                "login_module": True,
                "package": str(getattr(ctx, "target_package", "") or ""),
                "defer_launch": _clear_before_launch,
            },
        )
    if cold_started:
        from mino_nexus.loop.launch_grace import stamp_launch_grace

        stamp_launch_grace(ctx)
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
            return _leave(status=opening_status, summary=summary, reason_code="prepare_launch_unverified")
        if str(opened.get("status") or "") in ("pass", "ok", "success"):
            setattr(ctx, "app_launch_confirmed", True)
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
            from mino_nexus.loop.milestone_orchestrator import ensure_single_in_progress

            if isinstance(cursor, StepCursor) and cursor.phase == "prep":
                sync_prep_internal_milestones(cursor, ctx)
                ensure_single_in_progress(cursor, writer=writer)

    from mino_nexus.loop.recovery import recover_if_needed

    def _screen_unregistered(out) -> str:
        if out is None:
            return ""
        text = str(getattr(out, "error", "") or "")
        summary = out.summary() if callable(getattr(out, "summary", None)) else ""
        text = f"{text} {summary}"
        if "capability_unregistered" in text or "能力目录里没有" in text:
            return text.strip()[:240]
        return ""

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
        return _leave(status="blocked", summary=_focus_block, reason_code="focus_contract_unsupported")

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
            return _leave(status="blocked", summary=missing, reason_code="execution_contract_blocked")
        if recovery_out.stop_run:
            return _leave(status="blocked", summary=recovery_out.summary(), reason_code="environment_precondition_unmet")

    fg_out = ensure_target_app_foreground(
        ctx,
        proxy,
        target_package=target_pkg,
        shot=preflight_shot,
    )
    if _clear_before_launch and not bool(getattr(ctx, "prep_clear_done", False)):
        fg_out = None
    if fg_out:
        _log_preflight_recovery(fg_out, source="preflight_fg")
        if fg_out.stop_run:
            return _leave(status="blocked", summary=fg_out.summary(), reason_code="environment_precondition_unmet")

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
                    from mino_nexus.services.device_app_session_store import upsert_session

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
                    from mino_nexus.services.nav_route import load_fsm_doc

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
        return _leave(status="cancelled", summary="任务已取消", reason_code="task_cancelled")

    if login_module_case:
        pre_hint = compile_login_session_hint(
            getattr(ctx, "case_scene", None),
            "",
        )
        if pre_hint:
            history.append(f"0. program → info: {pre_hint}")

    if wall_budget_sec > 0:
        setattr(ctx, "case_deadline_ts", t0 + float(wall_budget_sec))
    fixed_deadline = resolved_case.deadline_at if resolved_case else float(case.get("attempt_deadline_at") or 0)
    if fixed_deadline:
        ctx.case_deadline_ts = min(ctx.case_deadline_ts or fixed_deadline, fixed_deadline)
    for seq in range(1, max_steps + 1):
        if cancel_check and cancel_check():
            return _leave(status="cancelled", summary="任务已取消", reason_code="task_cancelled")
        credit_sec = 0.0 if resolved_case else min(float(getattr(ctx, "case_budget_credit_sec", 0) or 0), _BUDGET_CREDIT_CAP_SEC)
        if wall_budget_sec > 0:
            setattr(ctx, "case_deadline_ts", min(t0 + float(wall_budget_sec) + credit_sec, fixed_deadline or float("inf")))
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
                setattr(ctx, "prep_signal_done_history", list(history[-48:]))
        if int(getattr(ctx, "otp_fetch_fail_streak", 0) or 0) >= 2:
            _log_turn_end(writer, cap="get_otp", status="fail")
            return _leave(
                status="blocked",
                summary="验证码邮件超时，环境未返回验证码，本步已停止。",
             reason_code="otp_environment_unavailable")

        if cursor.done:
            return _leave_case(cursor)

        setattr(ctx, "decision_screenshot_hash", "")
        from mino_nexus.loop.observe.session_log import set_screenshot_hash

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
            setattr(ctx, "viewport_wh", (int(shot.width), int(shot.height)))
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
                setattr(ctx, "decision_screenshot_hash", shot_hash)
                from mino_nexus.loop.observe.session_log import set_screenshot_hash

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
            setattr(ctx, "pending_result_step", None)
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
            setattr(ctx, "nav_hierarchy_nodes", [])
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
                setattr(ctx, "web_focus", dict(_snap_wf))
            _sync_app_foreground_ctx(ctx, nav, target_pkg, proxy)
        elif not is_explore:
            _sync_app_foreground_ctx(ctx, nav, target_pkg, proxy)

        select_observation(ctx, cursor=cursor, writer=writer)
        proxy.observation_mode = ctx.observation_mode
        if isinstance(cursor, StepCursor):
            from mino_nexus.loop.login_transition import remember_submit_frame
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

        if login_module_case and cursor.phase in ("prep", "do"):
            inspect_slots["session_block"] = ""
            ran_case_start_inspection = True
        else:
            if not ran_case_start_inspection:
                if login_module_case:
                    run_inspections(
                        inspections,
                        at="case_start",
                        shot=shot,
                        ctx=ctx,
                        provider_id=provider_id,
                        slot_sink=inspect_slots,
                        case=case,
                    )
                else:
                    inspect_slots["session_block"] = ""
                ran_case_start_inspection = True

            if cursor.phase != last_phase_seen:
                _ins_at = f"phase_enter:{cursor.phase}"
                if login_module_case or cursor.phase != "prep":
                    run_inspections(
                        inspections,
                        at=_ins_at,
                        shot=shot,
                        ctx=ctx,
                        provider_id=provider_id,
                        slot_sink=inspect_slots,
                        case=case,
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
        from mino_nexus.services.account_facet_commit import record_session_probe

        record_session_probe(ctx, str(inspect_slots.get("session_block") or ""))

        if not is_explore and str(ctx.platform).lower() == "android" and cursor.phase == "do":
            from mino_nexus.runtime.session_gate import required_session
            from mino_nexus.services.login_evidence import can_commit
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
            hier_nodes: list[Any] = []
            if nav is not None:
                snap = getattr(nav, "snapshot", None)
                hier_nodes = list(getattr(snap, "nodes", None) or []) if snap is not None else []
            launched = None
            if not (
                cursor.phase == "prep"
                and _clear_before_launch
                and not bool(getattr(ctx, "prep_clear_done", False))
            ):
                launched = maybe_recover_foreground(
                    proxy,
                    ctx,
                    run_id=scout_run_id,
                    case_seq=case_seq,
                    nodes=hier_nodes,
                )
            if launched:
                from mino_nexus.loop.launch_grace import stamp_launch_grace

                stamp_launch_grace(ctx)
                st = str(launched.get("status") or "pass")
                summ = str(launched.get("summary") or "")
                _log_program_tool(
                    writer,
                    capability_id="launch_app",
                    params={"package": str(launched.get("package") or target_pkg)},
                    status=st,
                    summary=summ,
                    executor_used=_program_executor(ctx),
                )
                rec(
                    seq,
                    capability_id="launch_app",
                    status=str(launched.get("status") or "pass"),
                    summary=str(launched.get("summary") or ""),
                    thought=str(launched.get("summary") or ""),
                    thumb=thumb,
                    executor_used=_program_executor(ctx),
                )
                emit(
                    "result",
                    thought=str(launched.get("summary") or ""),
                    step=seq,
                    capability_id="launch_app",
                    status=str(launched.get("status") or "pass"),
                    summary=str(launched.get("summary") or ""),
                    thumb=thumb,
                )
                _log_turn_end(
                    writer,
                    cap="launch_app",
                    status=str(launched.get("status") or "pass"),
                )
                continue

            fg_turn = ensure_target_app_foreground(
                ctx,
                proxy,
                target_package=target_pkg,
                shot=shot,
            )
            if (
                _clear_before_launch
                and cursor.phase == "prep"
                and not bool(getattr(ctx, "prep_clear_done", False))
            ):
                fg_turn = None
            if fg_turn and fg_turn.stop_run:
                return _leave(status="blocked", summary=fg_turn.summary(), reason_code="environment_precondition_unmet")
            if fg_turn and fg_turn.applied:
                from mino_nexus.loop.launch_grace import stamp_launch_grace

                stamp_launch_grace(ctx)
                fg_st = "pass" if fg_turn.recovered else "fail"
                fg_summ = fg_turn.summary()
                _log_program_tool(
                    writer,
                    capability_id="launch_app",
                    params={"package": target_pkg},
                    status=fg_st,
                    summary=fg_summ,
                    executor_used=fg_turn.executor_used or _program_executor(ctx),
                    execution_metadata=fg_turn.execution_metadata,
                )
                rec(
                    seq,
                    capability_id="launch_app",
                    status=fg_st,
                    summary=fg_summ,
                    thought="程序将被测 App 带到前台",
                    thumb=thumb,
                    executor_used=_program_executor(ctx),
                )
                emit(
                    "result",
                    thought=fg_summ,
                    step=seq,
                    capability_id="launch_app",
                    status=fg_st,
                    summary=fg_summ,
                    thumb=thumb,
                )
                _log_turn_end(
                    writer,
                    cap="launch_app",
                    status=fg_st,
                )
                continue

        screen_fp_turn = _screen_fp(
            shot, inspect_slots.get("hierarchy_text") or "", ctx=ctx
        )
        if not is_explore and isinstance(cursor, StepCursor):
            if cursor.phase == "do" and not cursor.step_start_fp:
                cursor.refresh_step_start_fp(screen_fp_turn)
            cur_probe = cursor.current()
            from mino_nexus.loop.visual_review import has_visual_prior

            # 待回看的设备动作先按当前截图裁定；导航/文案命中不能绕过回看切阶段。
            if cursor.phase == "do" and cur_probe and not has_visual_prior(cursor) and (
                str(cur_probe.expected or "").strip() or str(cur_probe.instruction or "").strip()
            ):
                from mino_nexus.loop import step_effect as step_effect_mod
                from mino_nexus.services import nav_telemetry

                try:
                    defer_chk = _expected_defers_to_check(str(cur_probe.expected or ""))
                    probe_nodes = _effect_nodes(nav, ctx)
                    loc_now = dict(getattr(nav, "localized", None) or {}) if nav is not None else {}
                    loc_hit = step_effect_mod.localized_matches_step(
                        loc_now,
                        instruction=str(cur_probe.instruction or ""),
                        expected="" if defer_chk else str(cur_probe.expected or ""),
                    )
                    hit, keywords = False, []
                    if probe_nodes:
                        hit, keywords = step_effect_mod.probe_expected_for_do(
                            str(cur_probe.expected or ""),
                            probe_nodes,
                            instruction=str(cur_probe.instruction or ""),
                            defer_expected_to_check=defer_chk,
                        )
                    if loc_hit and not keywords:
                        keywords = [
                            str(loc_now.get("display_name") or loc_now.get("chosen") or "localized")
                        ]
                    nav_telemetry.step_effect(
                        turn_id=seq,
                        run_id=scout_run_id,
                        case_id=cid,
                        step_n=cur_probe.n,
                        probe_hit=bool(hit or loc_hit),
                        model_done=False,
                        keywords=keywords,
                    )
                    from mino_nexus.loop.step_intent import instruction_required_intents

                    if hit or loc_hit:
                        cursor.note_step_effect_hit(
                            keywords,
                            expected=str(cur_probe.expected or ""),
                        )
                        need_int_probe = instruction_required_intents(
                            str(cur_probe.instruction or "")
                        )
                        if "nav_tab" in need_int_probe and defer_chk:
                            cursor.step_intents_done.add("nav_tab")
                    else:
                        cursor.reset_step_effect_streak()
                    if probe_nodes and step_effect_mod.expected_profile_shape_config(
                        str(cur_probe.expected or "")
                    ):
                        ps_hint = step_effect_mod.profile_shape_mismatch_hint(
                            str(cur_probe.expected or ""),
                            probe_nodes,
                        )
                        if ps_hint:
                            cursor.correction_hint = ps_hint
                except Exception as exc:  # noqa: BLE001 — 探针失败不能拖垮跑批
                    SLog.w(TAG, f"step_effect probe failed: {type(exc).__name__}: {exc}")

        cur = cursor.current()
        if cur is None:
            if is_explore:
                return _leave(status="pass", summary=cursor.summary(), pack=_pack(), reason_code="required_assertions_consumed")
            return _leave_case(cursor)

        if not is_explore and cur is not None:
            prev_step_n = int(getattr(ctx, "_run_case_step_n", 0) or 0)
            if int(cur.n) != prev_step_n:
                setattr(ctx, "_run_case_step_n", int(cur.n))
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
            from mino_nexus.services.resource_preflight import claim_requires_clear_cache

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
            setattr(ctx, "nav_localized_state", str(nav.localized.get("chosen") or ""))
            setattr(ctx, "nav_localized_confidence", float(nav.localized.get("confidence") or 0.0))
            setattr(ctx, "nav_localized", dict(nav.localized or {}))
            setattr(ctx, "nav_project_id", str(nav.project_id or ""))
            snap = getattr(nav, "snapshot", None)
            nodes = list(getattr(snap, "nodes", None) or []) if snap is not None else []
            setattr(ctx, "nav_hierarchy_nodes", nodes)
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
            from mino_nexus.loop.step_nav_plan import build_step_nav_plan_hint, step_needs_nav_plan

            if int(cur.n) != int(cursor.step_nav_plan_step_n or 0):
                cursor.step_nav_plan_step_n = int(cur.n)
                update_auth_state(ctx, login_flow_macro_done=set())
                ctx.login_macro_yielded = None
                setattr(ctx, "login_flow_interrupt", False)
                update_auth_state(ctx, login_flow_macro_active=False)
                setattr(ctx, "system_dialog_macro_done", False)
                setattr(ctx, "recovery_allow_back", False)
                instr_nav = str(cur.instruction or "")
                from mino_nexus.loop.nav_session_fork import (
                    detect_guest_tab_login_fork,
                    format_guest_tab_fork_hint,
                    required_session_from_scene,
                )

                req_sess = required_session_from_scene(
                    dict(getattr(ctx, "case_scene", None) or {})
                )
                exp_nav = str(cur.expected or "") if cur else ""
                fork = detect_guest_tab_login_fork(
                    instruction=instr_nav,
                    expected=exp_nav,
                    required_session=req_sess,
                )
                setattr(ctx, "nav_guest_tab_fork", fork)
                if step_needs_nav_plan(instr_nav):
                    cursor.step_nav_plan_hint = build_step_nav_plan_hint(
                        instruction=instr_nav,
                        app_id=str(getattr(ctx, "app_id", "") or ""),
                        project_id=str(getattr(ctx, "nav_project_id", "") or ""),
                        localized=dict(getattr(ctx, "nav_localized", None) or {}),
                        app_version=str(getattr(ctx, "app_version", "") or ""),
                        expected=exp_nav,
                        required_session=req_sess,
                        precondition=str(
                            getattr(cursor, "precondition", "") or case.get("precondition") or ""
                        ),
                    )
                else:
                    cursor.step_nav_plan_hint = (
                        format_guest_tab_fork_hint(fork) if fork else ""
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
        setattr(ctx, "system_overlay", str(fg_menu.get("system_overlay") or ""))
        setattr(ctx, "app_foreground", str(fg_menu.get("app_foreground") or ""))
        if str(fg_menu.get("app_foreground") or "") == "yes":
            menu = [
                c for c in menu
                if str(c.get("id") or "")
                not in (
                    "launch_app",
                    "open_app",
                    "open_url",
                    "recover_bring_target_app_foreground",
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

        if (
            not is_explore
            and isinstance(cursor, StepCursor)
            and cursor.phase in ("prep", "do")
            and cur
        ):
            from mino_nexus.loop.milestones import (
                ensure_milestones_before_decide,
                login_flow_under_milestones,
            )

            ensure_milestones_before_decide(
                cursor,
                ctx,
                app_id=str(getattr(ctx, "app_id", "") or ""),
                writer=writer,
            )
            blocked_step = _required_execution_blocker()
            if blocked_step:
                return _leave_execution_blocked(
                    seq, str(blocked_step.get("hook_cap") or blocked_step.get("device_cap") or "program"),
                    f"{blocked_step.get('title') or blocked_step.get('id')}：{blocked_step.get('evidence')}",
                    str(blocked_step.get("error_kind") or "capability_unavailable"),
                )
            from mino_nexus.loop.flow_block_runner import (
                try_run_login_flow_macro,
                try_run_system_dialog_macro,
            )
            from mino_nexus.loop.login_flow_interrupt import login_overlay_blocks_flow
            from mino_nexus.loop.step_flow_scope import login_flow_allowed

            hist_lines = list(history[-24:])
            nodes = list(getattr(ctx, "nav_hierarchy_nodes", None) or [])
            from mino_nexus.loop.login_submit import (
                _otp_code_already_entered,
                run_login_otp_submit_chain,
                run_post_sms_login_pipeline,
            )
            from mino_nexus.loop.step_contract import instruction_allows_login_flow
            from mino_nexus.loop.step_flow_scope import login_flow_allowed

            _lf_ok, _lf_msg = login_flow_allowed(
                cursor=cursor,
                phase="do",
                instruction=str(cur.instruction or ""),
                expected=str(cur.expected or ""),
            )
            _under_login_ms = login_flow_under_milestones(cursor)
            from mino_nexus.loop.visual_review import has_visual_prior

            # An observed login form may be the pending action's expected result.
            # Review that action before any legacy login chain can take ownership.
            _login_review_pending = has_visual_prior(cursor)
            if _lf_ok and not _under_login_ms and not _login_review_pending and not getattr(cursor, "login_chain_broken", False):
                from mino_nexus.loop.login_account_lease import (
                    login_surface_detected,
                    try_lease_on_login_surface,
                )

                if login_surface_detected(ctx, nodes):
                    _ok_lease, _lease_err, _lease_brief = try_lease_on_login_surface(
                        ctx, case
                    )
                    if _ok_lease and _lease_brief:
                        history.append(
                            f"{seq}. lease_account → pass: {_lease_brief[:160]}"
                        )
                    elif not _ok_lease and _lease_err:
                        cursor.correction_hint = (
                            f"登录界面租号失败：{_lease_err}；"
                            "请检查号池或 signal_ask_human。"
                        )
                _pending_sms = bool(getattr(cursor, "login_post_sms_pending", False))
                from mino_nexus.loop.login_submit import (
                    _otp_code_already_entered,
                    run_login_otp_submit_chain,
                    run_post_sms_login_pipeline,
                )

                if _pending_sms:
                    from mino_nexus.loop.login_submit import _sms_send_done, otp_fetch_allowed

                    if not _sms_send_done(hist_lines, cursor.step_intents_done):
                        cursor.login_post_sms_pending = False
                        _pending_sms = False
                    elif not otp_fetch_allowed(
                        hist_lines, cursor.step_intents_done, ctx, cursor=cursor
                    ):
                        cursor.login_post_sms_pending = False
                        _pending_sms = False
                if _pending_sms:
                    chain = run_post_sms_login_pipeline(
                        proxy,
                        ctx,
                        turn_seq=seq,
                        instruction=str(cur.instruction or ""),
                        intents_done=cursor.step_intents_done,
                        history_lines=hist_lines,
                        hierarchy_nodes=nodes,
                        login_module_case=bool(login_module_case),
                        retries=2,
                    )
                else:
                    from mino_nexus.loop.email_login_auto import run_email_login_ui_chain

                    chain = run_email_login_ui_chain(
                        proxy,
                        ctx,
                        turn_seq=seq,
                        instruction=str(cur.instruction or ""),
                        intents_done=cursor.step_intents_done,
                        history_lines=hist_lines,
                        login_module_case=bool(login_module_case),
                    )
                    if not chain:
                        from mino_nexus.loop.email_login_auto import _login_kind as _email_kind

                        if _email_kind(ctx) != "email":
                            chain = run_login_otp_submit_chain(
                                proxy,
                                ctx,
                                turn_seq=seq,
                                instruction=str(cur.instruction or ""),
                                intents_done=cursor.step_intents_done,
                                history_lines=hist_lines,
                                hierarchy_nodes=nodes,
                                login_module_case=bool(login_module_case),
                            )
                if chain:
                    if len(chain) == 1 and str(chain[0].get("capability_id") or "") == "input_text":
                        if chain[0].get("ok") and _otp_code_already_entered(
                            hist_lines, cursor.step_intents_done
                        ):
                            chain = []
                    if chain:
                        if cancel_check and cancel_check():
                            return _leave(status="cancelled", summary="任务已取消", reason_code="task_cancelled")
                        lc, ls = _consume_login_chain(
                            chain,
                            seq=seq,
                            cursor=cursor,
                            rec=rec,
                            emit=emit,
                            thumb=thumb,
                            screen_fp=screen_fp_turn,
                            login_flow_step=bool(
                                instruction_allows_login_flow(
                                    str(cur.instruction or ""),
                                    login_module_case=bool(login_module_case),
                                )
                            ),
                            fuse_web=_progress_gate_web_kwargs(ctx, proxy),
                            writer=writer,
                        )
                        chain_all_fail = not any(bool(x.get("ok")) for x in chain)
                        pending_now = bool(
                            _pending_sms or getattr(cursor, "login_post_sms_pending", False)
                        )
                        if chain_all_fail and not pending_now:
                            cursor.login_chain_fail_streak = 0
                            cursor.correction_hint = (
                                "登录表单未就绪：请先按 instruction 点击右上角「登录」打开弹窗，"
                                "再由程序链填邮箱、发码、取码并点登录提交。"
                            )
                        else:
                            fail_msg = _login_chain_guard(chain, cursor=cursor)
                            if fail_msg:
                                return _leave(status="fail", summary=fail_msg, reason_code="login_program_incomplete")
                        if any(
                            str(x.get("step_id") or "") == "send_code"
                            and x.get("ok")
                            for x in chain
                        ):
                            cursor.login_post_sms_pending = True
                            cursor.login_post_sms_stall = 0
                            cursor.step_intents_done.add("sms_send")
                        if pending_now or getattr(cursor, "login_post_sms_pending", False):
                            if _otp_code_already_entered(
                                list(history[-24:]), cursor.step_intents_done
                            ):
                                cursor.login_post_sms_pending = False
                                cursor.login_post_sms_stall = 0
                            else:
                                cursor.login_post_sms_stall = int(
                                    getattr(cursor, "login_post_sms_stall", 0) or 0
                                ) + 1
                                if cursor.login_post_sms_stall >= 5:
                                    cursor.login_post_sms_pending = False
                        if chain_all_fail and not pending_now:
                            head = str(
                                (chain[0].get("summary") if chain else "")
                                or (chain[0].get("capability_id") if chain else "")
                                or "login_chain"
                            )
                            cursor.correction_hint = head[:280]
                            _log_turn_end(
                                writer,
                                cap=str(chain[0].get("capability_id") or "login_chain"),
                                status="fail",
                            )
                            continue
                        else:
                            _log_turn_end(writer, cap=lc or "login_chain", status=ls)
                            continue
                elif _pending_sms:
                    cursor.login_post_sms_stall = int(getattr(cursor, "login_post_sms_stall", 0) or 0) + 1
                    if cursor.login_post_sms_stall >= 5:
                        cursor.login_post_sms_pending = False
                    cursor.correction_hint = (
                        "发码后程序链未填上验证码：请稍候或检查测试环境 otp.fixed；"
                        "勿重复 get_otp 空转。"
                    )
                    _log_turn_end(writer, cap="login_chain", status="wait")
                    continue
            macro = None
            from mino_nexus.loop.recovery_permission import permission_choice_dialog_present

            from mino_nexus.loop.flow_block_executor import try_review_login_submission

            macro = try_review_login_submission(proxy, ctx, cursor, shot=shot, writer=writer)
            if not macro and permission_choice_dialog_present(nodes):
                macro = try_run_system_dialog_macro(
                    proxy,
                    ctx,
                    turn_seq=seq,
                    target_package=str(getattr(ctx, "target_package", "") or target_pkg),
                )
            if not macro:
                from mino_nexus.loop.flow_block_executor import try_dispatch_block_hook
                from mino_nexus.loop.milestones import first_pending_hook_milestone, read_state

                if first_pending_hook_milestone(read_state(cursor)):
                    hook_macro = try_dispatch_block_hook(
                        proxy,
                        ctx,
                        cursor,
                        run_id=scout_run_id,
                        turn_seq=seq,
                        app_id=str(getattr(ctx, "app_id", "") or ""),
                        history_lines=hist_lines,
                        writer=writer,
                    )
                    if hook_macro:
                        macro = hook_macro
            if not macro:
                macro = try_run_system_dialog_macro(
                    proxy,
                    ctx,
                    turn_seq=seq,
                    target_package=str(getattr(ctx, "target_package", "") or target_pkg),
                )
            if not macro and not has_visual_prior(cursor):
                from mino_nexus.loop.nav_session_fork import (
                    login_flow_interrupt_allowed_for_step,
                    required_session_from_scene,
                )

                _req_sess_macro = required_session_from_scene(
                    dict(getattr(ctx, "case_scene", None) or {})
                )
                overlay_login = login_overlay_blocks_flow(
                    hierarchy_nodes=nodes,
                    instruction=str(cur.instruction or ""),
                    login_module_case=bool(login_module_case),
                    cursor=cursor,
                    phase="do",
                    expected=str(cur.expected or ""),
                )
                if overlay_login and not login_flow_interrupt_allowed_for_step(
                    instruction=str(cur.instruction or ""),
                    expected=str(cur.expected or ""),
                    required_session=_req_sess_macro,
                ):
                    overlay_login = False
                from mino_nexus.loop.milestones import login_flow_under_milestones

                _login_owned = login_flow_under_milestones(cursor)
                if overlay_login and not _login_owned:
                    setattr(ctx, "login_flow_interrupt", True)
                    macro = try_run_login_flow_macro(
                        proxy,
                        ctx,
                        run_id=scout_run_id,
                        case_seq=case_seq,
                        turn_seq=seq,
                        app_id=str(getattr(ctx, "app_id", "") or ""),
                        hierarchy_nodes=nodes,
                        history_lines=hist_lines,
                        interrupt=True,
                    )
                else:
                    setattr(ctx, "login_flow_interrupt", False)
                    if (
                        not _login_owned
                        and _lf_ok
                        and _allows_login_step
                        and not cursor.sms_auto_attempted
                    ):
                        macro = try_run_login_flow_macro(
                            proxy,
                            ctx,
                            run_id=scout_run_id,
                            case_seq=case_seq,
                            turn_seq=seq,
                            app_id=str(getattr(ctx, "app_id", "") or ""),
                            hierarchy_nodes=nodes,
                            history_lines=hist_lines,
                            interrupt=False,
                        )
            if (
                macro
                and str(macro.get("block_id") or "") == "fb.global.system_dialog"
                and not macro.get("ok")
            ):
                macro = None
            from mino_nexus.loop.program_device_fallback import (
                activate_program_vision_fallback,
                is_vision_fallback,
                note_vision_fallback_streak,
            )

            if macro and is_vision_fallback(macro):
                activate_program_vision_fallback(cursor, writer, macro)
                fb_n = note_vision_fallback_streak(cursor, macro)
                macro = None
                if fb_n >= 3:
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
                        capability_id="input_text",
                        status="fail",
                        summary=fb_sum,
                        thought=fb_sum,
                        thumb=thumb,
                    )
                    emit(
                        "result",
                        thought=fb_sum,
                        step=seq,
                        capability_id="input_text",
                        status="fail",
                        result_status="fail",
                        summary=fb_sum,
                        thumb=thumb,
                    )
                    return _leave(status="fail", summary=fb_sum, reason_code="program_vision_fallback_exhausted")
            if macro and macro.get("thumb"):
                thumb = str(macro.get("thumb") or thumb)
            if macro and macro.get("case_fail"):
                if macro.get("status") in {"product_fail", "unverified", "env_blocked"}:
                    reason = str(macro.get("error_kind") or "login_transition_observation_error")
                    writer.append("login/completion_blocked", {"reason_code": reason, "status": macro["status"]})
                    return _leave(status=macro["status"], summary=str(macro.get("summary") or reason), reason_code=reason)
                fail_summary = str(macro.get("summary") or "登录未成功")
                if macro.get("status") == "blocked":
                    return _leave_execution_blocked(
                        seq, str(macro.get("capability_id") or "program"), fail_summary,
                        str(macro.get("error_kind") or "program_hook_failed"),
                    )
                rec(
                    seq,
                    capability_id=str(macro.get("capability_id") or "confirm_login_state"),
                    status="fail",
                    summary=fail_summary[:400],
                    thought=fail_summary[:300],
                    thumb=thumb,
                )
                emit(
                    "result",
                    thought=fail_summary[:300],
                    step=seq,
                    capability_id=str(macro.get("capability_id") or "confirm_login_state"),
                    status="fail",
                    result_status="fail",
                    summary=fail_summary[:400],
                    thumb=thumb,
                )
                return _leave(status="fail", summary=fail_summary, reason_code=str(macro.get("error_kind") or "program_hook_failed"))
            if macro and macro.get("defer"):
                wait_summary = str(macro.get("summary") or "下一回合再看")
                rec(
                    seq,
                    capability_id=str(macro.get("capability_id") or "confirm_login_state"),
                    status="wait",
                    summary=wait_summary[:400],
                    thought=wait_summary[:300],
                    thumb=thumb,
                )
                emit(
                    "result",
                    thought=wait_summary[:300],
                    step=seq,
                    capability_id=str(macro.get("capability_id") or "confirm_login_state"),
                    status="wait",
                    result_status="wait",
                    summary=wait_summary[:400],
                    thumb=thumb,
                )
                _log_turn_end(writer, cap=str(macro.get("capability_id") or "confirm_login_state"), status="wait")
                continue
            if macro:
                bid = str(macro.get("block_id") or "")
                login_block = bid == "fb.global.login" or bid.startswith("fb.global.login.")
                if not login_block or macro.get("ok"):
                    cursor.sms_auto_attempted = True
                cap_auto = str(macro.get("capability_id") or "login_flow_macro")
                sum_auto = str(macro.get("summary") or "")
                st_auto = "pass" if macro.get("ok") else "fail"
                if macro.get("ok"):
                    st_auto = str(macro.get("status") or "pass")
                    if st_auto not in ("pass", "done"):
                        st_auto = "pass"
                if macro.get("ok"):
                    from mino_nexus.loop.milestones import otp_fill_device_complete

                    if macro and macro.get("ok"):
                        if cap_auto != "input_text" or otp_fill_device_complete(cursor, hist_lines):
                            cursor.record_step_op(cap_auto)
                        from mino_nexus.loop.channel_observation import bind_fresh_observation
                        from mino_nexus.loop.milestones import note_tool_pass_milestone
                        from mino_nexus.contract.execution.ui import UiChannel, ui_channel_from_ctx

                        if cap_auto == "input_text" and ui_channel_from_ctx(ctx) == UiChannel.WEB:
                            bind_fresh_observation(ctx, proxy)
                        note_tool_pass_milestone(
                            cursor,
                            capability_id=cap_auto,
                            milestone_id=str(
                                macro.get("milestone_id") or macro.get("step_id") or ""
                            ),
                            writer=writer,
                            tool_summary=sum_auto,
                            ctx=ctx,
                        )
                        if login_block:
                            from mino_nexus.loop.flow_block_runner import maybe_emit_login_flow_complete

                            maybe_emit_login_flow_complete(
                                ctx,
                                app_id=str(getattr(ctx, "app_id", "") or ""),
                                history_lines=hist_lines,
                            )
                if macro:
                    from mino_nexus.loop.observe.program_tool_log import log_program_tool

                    if str(macro.get("session_logged") or "") != "flow_block":
                        log_program_tool(
                            writer,
                            capability_id=cap_auto,
                            params=dict(macro.get("params") or {}),
                            status=st_auto,
                            summary=sum_auto,
                            executor_used="internal",
                            source="flow_block_macro",
                        )
                    rec(
                        seq,
                        capability_id=cap_auto,
                        status=st_auto,
                        summary=sum_auto,
                        thought=sum_auto,
                        thumb=thumb,
                        executor_used="internal",
                    )
                    emit(
                        "result",
                        thought=sum_auto,
                        step=seq,
                        capability_id=cap_auto,
                        status=st_auto,
                        summary=sum_auto,
                        thumb=thumb,
                    )
                    _log_turn_end(writer, cap=cap_auto, status=st_auto)
                    if macro.get("ok"):
                        from mino_nexus.loop.step_phase_fsm import try_transition_after_tool_pass

                        fsm_tr = try_transition_after_tool_pass(
                            cursor,
                            ctx,
                            writer=writer,
                            in_prep=cursor.phase == "prep",
                            in_check=cursor.phase == "check",
                        )
                        if fsm_tr == "check_to_case_done":
                            return _leave_case(cursor)
                        continue
                _fill_mid = str(macro.get("milestone_id") or macro.get("step_id") or "") if macro else ""
                _otp_hook_fail = bool(macro and login_block and _fill_mid == "otp_fill")
                _email_hook_fail = bool(macro and login_block and _fill_mid == "account_fill" and not macro.get("ok"))
                if macro and (
                    _otp_hook_fail
                    or _email_hook_fail
                    or (
                        cap_auto == "input_text"
                        and "sms_code" in sum_auto
                        and not macro.get("ok")
                    )
                ):
                    fuse_params = dict(macro.get("params") or {})
                    if not fuse_params:
                        fuse_params = {
                            "field": "email" if _email_hook_fail else "sms_code",
                        }
                    gate = getattr(cursor, "progress_gate", None)
                    if gate is not None:
                        gate.record_fail(cap_id=cap_auto, params=fuse_params)
                        from mino_nexus.contract.execution.ui import UiChannel, ui_channel_from_ctx

                        fuse_reason = gate.check(
                            phase=str(getattr(cursor, "phase", "") or "do"),
                            cap_id=cap_auto,
                            params=fuse_params,
                            screen_fp="",
                            login_flow_step=True,
                            web_channel=ui_channel_from_ctx(ctx) == UiChannel.WEB,
                            intents_done=set(getattr(cursor, "step_intents_done", None) or set()),
                        )
                        if fuse_reason:
                            return _leave(status="fail", summary=str(fuse_reason)[:400], reason_code="action_fuse_exhausted")
                    if _email_hook_fail and "焦点未确认" in sum_auto:
                        setattr(ctx, "web_email_tap_milli", None)
                    if _email_hook_fail:
                        cursor.correction_hint = (
                            sum_auto[:280] or "账号须填在上一次点击账号框的落点。"
                        )
                    else:
                        cursor.correction_hint = (
                            sum_auto[:280]
                            or "程序填验证码失败：请先点验证码输入框，再 input_text(field=sms_code)。"
                        )
                    continue
                if macro and not macro.get("ok"):
                    # 所有程序宏失败都进入同一进展门禁；不是只有验证码/邮箱输入才计失败。
                    # defer/视觉兜底已经在前面分别处理，剩余失败不得每轮无预算地重派。
                    gate = getattr(cursor, "progress_gate", None)
                    if gate is not None:
                        failed_params = dict(macro.get("params") or {})
                        gate.record_fail(cap_id=cap_auto, params=failed_params)
                        fuse_reason = gate.check(
                            phase=str(getattr(cursor, "phase", "") or "do"),
                            cap_id=cap_auto, params=failed_params, screen_fp="",
                            web_channel=_progress_gate_web_kwargs(ctx, proxy).get("web_channel", False),
                            milestone_id=str(macro.get("milestone_id") or macro.get("step_id") or ""),
                        )
                        if fuse_reason:
                            return _leave_execution_blocked(
                                seq, cap_auto, f"程序动作连续失败：{sum_auto}。{fuse_reason}"[:500],
                                "program_macro_no_progress",
                            )
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
                    if resolved_case is not None:
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
                        return _leave(status="blocked", summary=blocked_summary, reason_code=va.error_kind or "check_evidence_incomplete")
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
                            return _leave_case(cursor)
                        continue
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
                        return _leave(status="fail", summary=fail_summary, reason_code="required_assertion_failed")
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
                            return _leave(status="fail", summary=fb_sum, reason_code="program_vision_fallback_exhausted")
                        prep_macro = None
                    if prep_macro:
                        prep_cap = str(prep_macro.get("capability_id") or hook_cap)
                        prep_sum = str(prep_macro.get("summary") or "")
                        if prep_macro.get("case_fail"):
                            if prep_macro.get("status") in {"product_fail", "unverified", "env_blocked"}:
                                reason = str(prep_macro.get("error_kind") or "login_transition_observation_error")
                                writer.append("login/completion_blocked", {"reason_code": reason, "status": prep_macro["status"]})
                                return _leave(status=prep_macro["status"], summary=str(prep_macro.get("summary") or reason), reason_code=reason)
                            if prep_macro.get("status") == "blocked":
                                return _leave_execution_blocked(
                                    seq, prep_cap, prep_sum,
                                    str(prep_macro.get("error_kind") or "program_hook_failed"),
                                )
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
                            return _leave(status="fail", summary=prep_sum or "登录未成功", reason_code=str(prep_macro.get("error_kind") or "program_hook_failed"))
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
                            continue
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
                            from mino_nexus.loop.step_phase_fsm import try_transition_after_tool_pass

                            fsm_tr = try_transition_after_tool_pass(
                                cursor,
                                ctx,
                                writer=writer,
                                in_prep=True,
                                in_check=False,
                            )
                            if fsm_tr == "check_to_case_done":
                                return _leave_case(cursor)
                        continue

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
                    return _leave(status="fail", summary="刷新页面只在 Web 执行", reason_code="capability_platform_mismatch")
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
                    return _leave(status="fail", summary=summary or "刷新页面失败", reason_code="page_refresh_failed")
                from mino_nexus.loop.milestones import note_tool_pass_milestone

                note_tool_pass_milestone(cursor, capability_id="reload_page", writer=writer)
                _log_turn_end(writer, cap="reload_page", status="pass")
                continue

            if action_scheme(ctx) == "dom":
                from mino_nexus.loop.intent_tap import dom_checkbox_tap_decision, dom_intent_tap_decision

                applicable, dom_dec = dom_checkbox_tap_decision(cursor, ctx, writer)
                if not applicable:
                    dom_dec = dom_intent_tap_decision(cursor, ctx, writer)
                if dom_dec is not None:
                    cursor.dom_miss_streak = 0
                    decision = dom_dec
                else:
                    from mino_nexus.loop.milestone_orchestrator import in_progress_milestone
                    from mino_nexus.loop.milestones import read_state
                    focus = in_progress_milestone(read_state(cursor)) or {}
                    cap = str(focus.get("device_cap") or focus.get("cap") or "")
                    if applicable or cap == "tap_element" or focus.get("kind") == "visual_tap":
                        from mino_nexus.action_space.scheme import use_visual_fallback
                        use_visual_fallback(ctx, reason="target_not_unique_in_structure", writer=writer)
            if decision is None and (_vision_plan_on() or execution_phase_active()):
                from mino_nexus.loop.milestone_orchestrator import run_vision_orchestrated_turn
                from mino_nexus.loop.program_device_fallback import merge_program_vision_fallback_slots

                merge_program_vision_fallback_slots(cursor, inspect_slots)
                if getattr(ctx, "router", None) is None:
                    setattr(ctx, "router", proxy)
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
                    return _leave_execution_blocked(seq, source, str(orch.blocked_error.get("error") or "模型不可用"),
                                                    kind if source == "screen" else "model_" + kind)
                if orch.phase_transition:
                    _log_turn_end(writer, cap="phase_transition", status="continue")
                    continue
                if orch.decision is not None:
                    decision = orch.decision
                    if "visual_observation_unavailable" in decision.parse_warnings:
                        _log_turn_end(writer, cap="", status="blocked")
                        return _leave(status="unverified", summary=decision.thought,
                                      reason_code=str(getattr(decision, "error_kind", "") or "observation_missing"))
                    if orch.plan_only and not (decision.action and decision.action.capability_id):
                        if str(decision.status or "") in ("fail", "give_up"):
                            summary = decision.thought or "里程碑失败，已停止空转"
                            return _leave(status="fail", summary=summary[:500], reason_code="milestone_plan_incomplete")
                        streak = int(getattr(cursor, "plan_only_streak", 0) or 0) + 1
                        cursor.plan_only_streak = streak
                        if streak >= 2:
                            return _leave(
                                status="fail",
                                summary="连续两回合只有规划、没有可下发的动作，已停止。",
                             reason_code="model_action_missing")
                        thought = decision.thought or "规划轮次，等待下一里程碑"
                        emit("think", thought=thought, step=seq, status="continue")
                        _log_turn_end(writer, cap="", status="continue")
                        continue
                    cursor.plan_only_streak = 0
            if decision is None:
                decision = _decide_gated()
            model_meta = (decision.raw_llm or {}).get("meta") or {}
            if model_meta.get("error") and not (decision.raw_llm or {}).get("llm_output"):
                return _leave_execution_blocked(seq, "model", str(model_meta["error"]),
                                                "model_" + str(model_meta.get("fail_kind") or "request"))
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
                if not structure_target_available(decision.action.capability_id, decision.action.params, ctx):
                    use_visual_fallback(ctx, reason="model_target_not_unique_in_structure", writer=writer)
                    # DOM 规划不能证明截图上没有挡屏。切换观察后先按同一截图重规划，尚未派发任何点击。
                    from mino_nexus.loop.vision_plan import run_vision_plan_turn_if_enabled

                    screenshot_slots = dict(inspect_slots)
                    screenshot_slots["hierarchy_text"] = ""
                    screenshot_slots["nav_assist"] = ""
                    screenshot_plan = run_vision_plan_turn_if_enabled(
                        cursor=cursor, ctx=ctx, writer=writer, shot=shot, steps=steps,
                        inspect_slots=screenshot_slots, provider_id=str(provider_id or ""),
                        send_image=not should_withhold_llm_image(ctx, shot=shot),
                    )
                    if screenshot_plan is not None and screenshot_plan.blocked_error:
                        problem = screenshot_plan.blocked_error
                        return _leave_execution_blocked(
                            seq, "", str(problem.get("error") or "截图规划无法完成"),
                            str(problem.get("fail_kind") or "visual_replan_unavailable"),
                        )
                    if getattr(cursor, "milestone_hold_exec", False):
                        cursor.milestone_hold_exec = False
                        _log_turn_end(writer, cap="", status="pending")
                        continue
                    decision = _decide(send_image=not should_withhold_llm_image(ctx, shot=shot))
                    fallback_meta = (decision.raw_llm or {}).get("meta") or {}
                    if fallback_meta.get("error") and not (decision.raw_llm or {}).get("llm_output"):
                        return _leave_execution_blocked(seq, "model", str(fallback_meta["error"]),
                                                        "model_" + str(fallback_meta.get("fail_kind") or "request"))
            clear_transient_llm_image_gate(ctx)
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
                # 此轮只有观察报告。下一轮执行恢复焦点，不把报告或清障当业务 pass。
                _log_turn_end(writer, cap=REPORT_SCREEN_OBSTACLE, status="pending")
                continue
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
                continue
            if use_milestone_check and not cursor.step_checked:
                mv = evaluate_milestones(cursor, ctx)
                if mv.phase_complete:
                    cursor.mark_checked()
        thought = decision.thought or ""
        trace = _llm_trace(decision)
        _meta_llm = ((decision.raw_llm or {}).get("meta") if isinstance(decision.raw_llm, dict) else None) or {}
        _wasted = int(_meta_llm.get("wasted_ms") or 0) if isinstance(_meta_llm, dict) else 0
        if _wasted > 0:
            setattr(ctx, "case_budget_credit_sec",
                    float(getattr(ctx, "case_budget_credit_sec", 0) or 0) + _wasted / 1000.0)
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
            setattr(ctx, "nav_vlm_hierarchy", vlm_h)
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
            if (
                decision.status == "give_up"
                and not is_explore
                and isinstance(cursor, StepCursor)
                and cursor.phase == "do"
            ):
                cur_give = cursor.current()
                if cur_give and cursor.try_auto_finish_do_when_milestones_met(
                    ctx, writer=writer
                ):
                    rec(
                        seq,
                        capability_id="signal_done",
                        status="skipped",
                        summary="子里程碑已齐，放弃无效，自动进入校验",
                        thought=thought,
                        thumb=thumb,
                    )
                    emit(
                        "result",
                        thought=thought,
                        step=seq,
                        capability_id="signal_done",
                        status="skipped",
                        summary="子里程碑已齐，放弃无效，自动进入校验",
                        thumb=thumb,
                    )
                    if writer:
                        writer.append(
                            "decision/give_up_rejected",
                            {
                                "thought": thought,
                                "case_step": cur_give.n,
                                "phase": "do",
                                "reason": "milestones_complete",
                            },
                        )
                    _log_turn_end(writer, cap="signal_done", status="skipped")
                    continue
            if decision.status == "skip":
                st = "skip"
                summary = thought or "当前渠道跳过本条用例"
                if writer:
                    writer.append("decision/skip", {"thought": thought, "phase": cursor.phase})
                    writer.append("turn/end", {"decision_cap": "signal_skip", "decision_status": st})
                return _leave(status=st, summary=summary, reason_code="model_gave_up")
            st = "blocked" if decision.status == "ask_human" else "fail"
            summary = thought or ("需要人介入" if st == "blocked" else "模型放弃")
            if writer:
                writer.append(
                    "decision/ask_human" if decision.status == "ask_human" else "decision/give_up",
                    {
                        "thought": thought,
                        "confidence": decision.confidence,
                        "phase": cursor.phase,
                        "case_step": 0 if cursor.phase == "prep" else (cur.n if cur else 0),
                        **trace,
                    },
                )
                writer.append("turn/end", {"decision_cap": "", "decision_status": st})
            reason = (str(getattr(decision, "error_kind", "") or "observation_missing")
                      if "visual_observation_unavailable" in decision.parse_warnings
                      else "model_human_required" if decision.status == "ask_human" else "model_gave_up")
            return _leave(status=st, summary=summary, reason_code=reason)

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
        from mino_nexus.catalog.tool_schema import fill_input_text_from_ctx, fill_target_package

        params = fill_target_package(
            params,
            cap_id=cap_id,
            target_package=str(getattr(ctx, "target_package", "") or target_pkg),
        )
        params = fill_input_text_from_ctx(params, cap_id=cap_id, ctx=ctx)
        from mino_nexus.loop.device_execute_params import prepare_device_execute_params

        params = prepare_device_execute_params(cap_id, params, ctx)
        if isinstance(cursor, StepCursor):
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
                return _leave_execution_blocked(seq, cap_id, stop_prior, "review_observation_missing")
            if prior_kind == "wait_stop":
                return _leave_execution_blocked(seq, cap_id, stop_prior, "review_wait_exhausted")
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
                return _leave(status="blocked", summary=summary, reason_code="execution_contract_blocked")
            if prior_kind == "stop" or (prior_kind == "redo" and stop_prior):
                return _leave(status="fail", summary=(stop_prior or "回看未通过")[:500], reason_code="action_result_unverified")
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
                continue
            if prior_kind == "redo" and not cap_id:
                emit(
                    "think",
                    thought="上一步未完成，但这次没有返回坐标，本回合不重复规划。",
                    step=seq,
                    status="pending",
                )
                _log_turn_end(writer, cap="", status="pending")
                continue
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
                                return _leave(status=hook["status"], summary=str(hook.get("summary") or reason), reason_code=reason)
                            if hook.get("status") == "blocked":
                                return _leave_execution_blocked(
                                    seq, hook_cap, hook_summary,
                                    str(hook.get("error_kind") or "program_hook_failed"),
                                )
                            rec(seq, capability_id=hook_cap, status="fail", summary=hook_summary, thought=hook_summary)
                            emit("result", step=seq, capability_id=hook_cap, status="fail",
                                 result_status="fail", summary=hook_summary, thought=hook_summary)
                            _log_turn_end(writer, cap=hook_cap, status="fail")
                            return _leave(status="fail", summary=hook_summary, reason_code=str(hook.get("error_kind") or "program_hook_failed"))
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
                                return _leave_case(cursor)
                        _log_turn_end(writer, cap=hook_cap, status=hook_status)
                        continue
                _log_turn_end(writer, cap=cap_id, status="continue")
                continue
            from mino_nexus.loop.visual_review import guard_input_focus_action

            input_guard = guard_input_focus_action(cursor, ctx, cap_id, writer=writer)
            if input_guard == "stop":
                return _leave(status="blocked", summary=str(cursor.semantic_stop)[:500], reason_code="execution_contract_blocked")
            if input_guard == "reask":
                emit(
                    "think", thought=str(cursor.correction_hint), step=seq, status="pending",
                    event_kind="input_policy_reask", execution_status="not_dispatched",
                )
                _log_turn_end(writer, cap="", status="pending")
                continue
        if cap_id == "tap_element" and isinstance(cursor, StepCursor):
            from mino_nexus.action_space.scheme import action_scheme as configured_action_scheme
            from mino_nexus.loop.milestone_orchestrator import in_progress_milestone
            from mino_nexus.loop.milestones import read_state

            consent_focus = in_progress_milestone(read_state(cursor)) or {}
            if (configured_action_scheme(ctx) == "dom_preferred" and action_scheme(ctx) == "visual"
                    and consent_focus.get("id") == "legal_consent"
                    and consent_focus.get("converted_from_hook") == "operation.check_box"):
                from mino_nexus.loop.ui_consent import find_consent_control, tap_params_for_control

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
            from mino_nexus.ai.coords import prepare_xy_params_for_execute

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
        guard_ctx = {
            "phase": cursor.phase,
            "cap_id": cap_id,
            "params": params,
            "cursor": cur,
            "step_cursor": cursor,
            "last_tap": cursor.last_tap,
            "screen_fp": screen_fp,
            "guards": list(phase_cfg.get("guards") or []),
            "history_lines": list(history[-12:]),
            "tap_summary": tap_params_guard_summary(params),
            "menu": menu,
            "accounts_brief": str(getattr(ctx, "accounts_brief", "") or ""),
            "run_env_brief": task_env_brief,
            "run_ctx": ctx,
            "ctx": ctx,
            "app_foreground": fg_ctx.get("app_foreground") or fg_menu.get("app_foreground") or "",
            "system_overlay": fg_ctx.get("system_overlay") or fg_menu.get("system_overlay") or "",
            "nav_localized": dict(getattr(ctx, "nav_localized", None) or {}),
            "app_version": str(getattr(ctx, "app_version", "") or ""),
            "case_scene": dict(getattr(ctx, "case_scene", None) or {}),
            "session_block": llm_session_block(ctx, str(inspect_slots.get("session_block") or "")),
            "prep_clear_done": bool(getattr(ctx, "prep_clear_done", False)),
            "session_fact_session": str(
                (getattr(ctx, "session_fact", None) or {}).get("session") or ""
            ).strip().lower(),
            "app_launch_confirmed": bool(getattr(ctx, "app_launch_confirmed", False)),
            "precondition": str(getattr(cursor, "precondition", "") or case.get("precondition") or ""),
            "decision_thought": thought,
            "login_module_case": bool(login_module_case),
            "login_flow_interrupt": bool(getattr(ctx, "login_flow_interrupt", False)),
            "login_flow_macro_active": bool(getattr(ctx, "login_flow_macro_active", False)),
            "recovery_allow_back": bool(getattr(ctx, "recovery_allow_back", False)),
            "nav_open_loop_active": bool(getattr(ctx, "nav_open_loop_active", False)),
            "nav_open_loop_blocked_target": str(
                getattr(ctx, "nav_open_loop_blocked_target", "") or ""
            ),
            "fsm_last_decline_key": str(getattr(ctx, "fsm_last_decline_key", "") or ""),
            "fsm_decline_repeat_streak": int(getattr(ctx, "fsm_decline_repeat_streak", 0) or 0),
            "display_guard_active": bool(getattr(ctx, "display_guard_active", False)),
            "sn": str(getattr(ctx, "sn", "") or ""),
            "target_package": str(target_pkg or ""),
            "run_ctx": ctx,
        }
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
            from mino_nexus.ai.coords import prepare_xy_params_for_execute

            if params.get("intent_dom"):
                from mino_nexus.ai.coords import lift_selector_target
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
            from mino_nexus.loop.step_phase_fsm import (
                handle_model_signal_done_blocked,
                milestone_phase_active,
            )

            if (
                milestone_phase_active()
                and not is_explore
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
                        return _leave_case(cursor)
                    continue
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
                    continue
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
                    continue
            if is_explore:
                cursor.mark_explore_done()
                rec(
                    seq,
                    capability_id="signal_done",
                    status="pass",
                    summary=thought or cursor.summary(),
                    thought=thought,
                    thumb=thumb,
                )
                emit(
                    "result",
                    thought=thought,
                    step=seq,
                    capability_id="signal_done",
                    status="pass",
                    summary=cursor.summary(),
                    thumb=thumb,
                )
                _log_turn_end(writer, cap="signal_done", status="pass")
                return _leave(status="pass", summary=cursor.summary(), pack=_pack(), reason_code="required_assertions_consumed")
            if in_prep:
                from mino_nexus.services.resource_preflight import prep_resource_gate_issues

                setattr(ctx, "prep_signal_done_history", list(history[-48:]))
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
                            return _leave(
                                status="blocked",
                                summary=(
                                    f"{gate_msg} "
                                    "测试资源 Claim 多次未满足，任务已停止。"
                                ),
                             reason_code="environment_precondition_unmet")
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
                    continue
                if isinstance(cursor, StepCursor):
                    cursor.prep_resource_gate_streak = 0
                summary = thought or "前置检查完成，进入操作步骤"
                rec(seq, capability_id="signal_done", status="skipped",
                        summary=summary, thought=thought, thumb=thumb)
                emit("result", thought=thought, step=seq, capability_id="signal_done",
                     status="skipped", summary=summary, thumb=thumb)
                cursor.finish_prep()
                _log_turn_end(writer, cap="signal_done", status="skipped")
                continue
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
                        continue
                rec(seq, capability_id="signal_done", status="skipped",
                        summary=thought or f"步骤 {cur.n} 校验结束", thought=thought, thumb=thumb)
                emit("result", thought=thought, step=seq, capability_id="signal_done",
                     status="skipped", summary=f"步骤 {cur.n} 校验通过", thumb=thumb)
                if not cursor.advance():
                    return _leave_case(cursor)
                _log_turn_end(writer, cap="signal_done", status="skipped")
                continue
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
                setattr(ctx, "_last_probe_diag", probe_diag_guard)
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
                            return _leave(
                                status="fail",
                                summary=(
                                    f"步骤 {cur_n} 连续 {streak} 次无法收工："
                                    f"{str(do_work_reason or '')[:160]}"
                                ),
                             reason_code="required_operation_unverified")
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
                    continue
            from mino_nexus.loop import step_effect as step_effect_mod
            from mino_nexus.services import nav_telemetry

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
                case_id=cid,
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
            continue

        if not cap_id:
            summary = thought or "模型没有给出下一步动作"
            return _leave(status="fail", summary=summary, reason_code="model_action_missing")

        # GuardGate 与 ProgressGate 并行独立计数（§8.2），所以放在 phase guards 之前单独判。
        if nav is not None:
            verdict = nav.check(cap_id=cap_id, params=params)
            if verdict.action == "stop":
                st = "blocked" if verdict.stop_signal == "ask_human" else "fail"
                if writer:
                    writer.append(
                        "nav/guard_stop",
                        {
                            "guard_id": verdict.guard_id,
                            "strength": verdict.strength,
                            "reason": verdict.reason,
                            "run_type": run_type,
                            "signal": verdict.stop_signal,
                        },
                    )
                    writer.append("turn/end", {"decision_cap": "nav_guard_stop", "decision_status": st})
                return _leave(status=st, summary=verdict.reason, reason_code="navigation_guard_blocked")
            if verdict.action in ("steer", "block"):
                nav_cap = f"nav_guard_{verdict.action}"
                note = nav.steer_line(verdict) if verdict.action == "steer" else verdict.reason
                if writer:
                    writer.append(
                        "nav/guard_block" if verdict.action == "block" else "nav/guard_steer",
                        {
                            "capability_id": cap_id,
                            "guard_id": verdict.guard_id,
                            "strength": verdict.strength,
                            "ladder": verdict.ladder,
                            "reason": verdict.reason,
                        },
                    )
                rec(seq, capability_id=nav_cap, status="skipped", summary=note,
                    thought=thought, thumb=thumb)
                emit("result", thought=thought, step=seq, capability_id=nav_cap,
                     status="skipped", summary=note, thumb=thumb)
                if isinstance(cursor, StepCursor):
                    from mino_nexus.loop.milestones import read_state

                    passed = tuple(sorted(
                        str(row.get("id") or "") for row in read_state(cursor).get("milestones") or []
                        if isinstance(row, dict) and str(row.get("status") or "") == "pass"
                    ))
                    nav_stop = cursor.progress_gate.record_guard_rejection(
                        case_step=int(getattr(cur, "n", 0) or 0), capability_id=cap_id,
                        guard_id=f"nav:{verdict.guard_id or verdict.action}",
                        passed_milestones=passed, reason=str(verdict.reason or note),
                    )
                    if nav_stop:
                        if writer:
                            writer.append("progress/stop", {"reason": nav_stop, "guard_id": verdict.guard_id})
                        return _leave(status="blocked", summary=nav_stop, reason_code="navigation_guard_blocked")
                _log_turn_end(writer, cap=nav_cap, status="skipped", guard_id=verdict.guard_id)
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
            skip_cap = guard_verdict.code or cap_id
            if writer:
                from mino_nexus.loop.observe.ops_guard_diag import enrich_guard_block_payload, note_guard_block

                block_payload = enrich_guard_block_payload(
                    {
                        "capability_id": cap_id,
                        "reason": skip_reason,
                        "rewritten_cap": skip_cap,
                        "dispatch_gate_code": skip_cap,
                        "guard_id": guard_verdict.guard_id,
                    },
                    guard_ctx=guard_ctx,
                    cur=cur,
                    probe_diag=dict(getattr(ctx, "_last_probe_diag", None) or {}),
                )
                writer.append("guard/block", block_payload)
                if isinstance(cursor, StepCursor):
                    note_guard_block(cursor, block_payload)
            if isinstance(cursor, StepCursor):
                from mino_nexus.catalog.guard_recovery_seed import recovery_text

                hint = recovery_text(str(guard_verdict.guard_id or ""), skip_reason)
                if hint:
                    cursor.correction_hint = hint
                    skip_reason = hint
            rec(seq, capability_id=skip_cap, status="skipped", summary=skip_reason, thought=thought, thumb=thumb)
            emit(
                "result",
                thought=thought,
                step=seq,
                capability_id=skip_cap,
                status="skipped",
                result_status="skipped",
                summary=skip_reason,
                thumb=thumb,
                device_dispatched=False,
                execution_kind="guard_skip",
            )
            stop_msg = None
            if skip_cap == "action_fuse":
                stop_msg = cursor.progress_gate.record_fuse_block(skip_reason)
            elif str(getattr(guard_verdict, "guard_id", "") or "").startswith("skip_repeat_"):
                gid = str(guard_verdict.guard_id or "")
                n_block = int((getattr(cursor, "guard_block_counts", None) or {}).get(gid) or 0)
                if n_block >= 3:
                    stop_msg = (
                        f"连续 {n_block} 次被 {gid} 拒绝仍重复同一动作，已停止。"
                        f" {str(skip_reason or '')[:160]}"
                    )
            elif skip_cap in ("limit_recovery_retry", "block_back_without_nav_back_semantics"):
                setattr(ctx, "recovery_allow_back", True)
                if isinstance(cursor, StepCursor):
                    cursor.correction_hint = skip_reason
                    if skip_cap == "limit_recovery_retry":
                        n_block = cursor.bump_recovery_block()
                        if n_block >= 3:
                            stop_msg = (
                                f"连续 {n_block} 次同类恢复被拒绝后仍重复尝试，判定陷入死循环。"
                                f"{skip_reason}"
                            )
            elif skip_cap in (
                "block_repeat_get_otp_when_ready",
                "flow_block:otp_ready_need_input",
            ) and cursor.phase in ("prep", "do"):
                cursor.correction_hint = (
                    f"{skip_reason} "
                    "请 input_text(field=sms_code) 或先 tap「6-Digit Code」；勿再 get_otp。"
                )
                cursor.otp_ready_guard_streak = (
                    int(getattr(cursor, "otp_ready_guard_streak", 0) or 0) + 1
                )
                from mino_nexus.loop.milestones import login_block_id_for_ctx, read_state

                _st_fb = read_state(cursor)
                _bid_fb = str((_st_fb.get("block_ref") or {}).get("block_id") or "")
                if not _bid_fb:
                    _bid_fb = login_block_id_for_ctx(ctx)
                from mino_nexus.loop.flow_block_executor import _dispatch_otp_fill
                from mino_nexus.contract.execution.ui import UiChannel, ui_channel_from_ctx

                _fill_otp = None
                _remediate_fill = True
                if ui_channel_from_ctx(ctx) == UiChannel.WEB:
                    from mino_nexus.loop.web.web_progress import (
                        refresh_web_focus,
                        web_editable_focus_ready,
                    )

                    refresh_web_focus(ctx, proxy)
                    _remediate_fill = web_editable_focus_ready(ctx)
                if _remediate_fill:
                    _fill_otp = _dispatch_otp_fill(
                        proxy,
                        ctx,
                        cursor,
                        turn_seq=seq,
                        block_id=_bid_fb,
                        mid="otp_fill",
                        writer=writer,
                    )
                else:
                    cursor.correction_hint = (
                        f"{skip_reason} "
                        "焦点不在验证码框：请先 tap「6-Digit Code」再 input_text；勿再 get_otp。"
                    )
                from mino_nexus.loop.program_device_fallback import (
                    activate_program_vision_fallback,
                    is_vision_fallback,
                    merge_program_vision_fallback_slots,
                )

                if _fill_otp and is_vision_fallback(_fill_otp):
                    activate_program_vision_fallback(cursor, writer, _fill_otp)
                    merge_program_vision_fallback_slots(cursor, inspect_slots)
                    from mino_nexus.loop.vision_exec import run_vision_exec_turn, execution_phase_active

                    if execution_phase_active():
                        guard_vision_remediate = run_vision_exec_turn(
                            cursor=cursor,
                            ctx=ctx,
                            writer=writer,
                            shot=shot,
                            steps=steps,
                            inspect_slots=inspect_slots,
                            provider_id=str(provider_id or ""),
                            phase_tool_kinds=phase_tool_kinds or None,
                            send_image=True,
                        )
                        skip_reason = None
                elif _fill_otp:
                    _fcap = str(_fill_otp.get("capability_id") or "input_text")
                    _fst = "pass" if _fill_otp.get("ok") else "fail"
                    _fsum = str(_fill_otp.get("summary") or "")[:400]
                    rec(
                        seq,
                        capability_id=_fcap,
                        status=_fst,
                        summary=_fsum,
                        thought=_fsum,
                        thumb=thumb,
                    )
                    emit(
                        "result",
                        thought=_fsum,
                        step=seq,
                        capability_id=_fcap,
                        status=_fst,
                        summary=_fsum,
                        thumb=thumb,
                    )
                    _log_turn_end(writer, cap=_fcap, status=_fst)
                    continue
                if cursor.otp_ready_guard_streak >= 4:
                    stop_msg = (
                        f"otp_ready 后已连续 {cursor.otp_ready_guard_streak} 次误选 get_otp"
                        "且程序填码仍失败；请人工聚焦验证码框或 signal_give_up。"
                    )
            elif skip_cap.removeprefix("flow_block:") == "require_sms_send_before_otp":
                from mino_nexus.loop.login_verification import is_web_email_login

                extra = (
                    "请先用 tap_element 点击 Send 向邮箱发验证码，确认发码成功后再 get_otp。"
                    if is_web_email_login(ctx)
                    else "请先用 tap_element 点击 Send/发送验证码，确认发码成功后再 get_otp。"
                )
                cursor.correction_hint = f"{skip_reason} {extra}"
                cursor.sms_otp_guard_streak = (
                    int(getattr(cursor, "sms_otp_guard_streak", 0) or 0) + 1
                )
                if cursor.sms_otp_guard_streak >= 5:
                    stop_msg = (
                        f"连续 {cursor.sms_otp_guard_streak} 次在发码前尝试 get_otp。"
                        "请点 Send/发送验证码或 signal_give_up。"
                        f" 最近：{skip_reason[:120]}"
                    )
                elif cursor.sms_otp_guard_streak >= 3:
                    stop_msg = cursor.progress_gate.record_fuse_block(
                        f"【熔断·守卫】{skip_reason}"
                    )
            elif (
                cursor.phase == "prep"
                and isinstance(cursor, StepCursor)
                and skip_cap in (
                    "block_prep_guest_mine_tab",
                    "skip_repeat_clear_app_cache",
                )
            ):
                cursor.prep_guard_streak = int(getattr(cursor, "prep_guard_streak", 0) or 0) + 1
                cursor.correction_hint = skip_reason
                if cursor.prep_guard_streak >= 6:
                    stop_msg = (
                        f"前置空转 {cursor.prep_guard_streak} 轮（仍为 logged_in 或无法 logout）。"
                        "请确认应用退出登录路径可用，或将设备登出并重新观察后再跑；清缓存不等于退出登录。"
                        f" 最近拦截：{skip_reason[:160]}"
                    )
            elif (
                skip_cap in ("block_repeat_email_tab", "flow_block:block_repeat_email_tab")
                and isinstance(cursor, StepCursor)
                and cur
                and cursor.phase in ("prep", "do")
            ):
                cursor.email_tab_guard_streak = (
                    int(getattr(cursor, "email_tab_guard_streak", 0) or 0) + 1
                )
                cursor.correction_hint = skip_reason
                from mino_nexus.loop.step_flow_scope import login_flow_allowed

                _lf_em_ok, _ = login_flow_allowed(
                    cursor=cursor,
                    phase="do",
                    instruction=str(cur.instruction or ""),
                    expected=str(cur.expected or ""),
                )
                if _lf_em_ok and not getattr(cursor, "login_chain_broken", False):
                    from mino_nexus.loop.login_account_lease import try_lease_on_login_surface
                    from mino_nexus.loop.milestones import login_flow_under_milestones

                    try_lease_on_login_surface(ctx, case)
                    hist_em = list(history[-24:])
                    chain_em: list[dict[str, Any]] = []
                    if login_flow_under_milestones(cursor):
                        from mino_nexus.loop.flow_block_executor import try_dispatch_block_hook

                        hook = try_dispatch_block_hook(
                            proxy,
                            ctx,
                            cursor,
                            run_id=scout_run_id,
                            turn_seq=seq,
                            app_id=str(getattr(ctx, "app_id", "") or ""),
                            history_lines=hist_em,
                            writer=writer,
                        )
                        if hook:
                            chain_em = [dict(hook)]
                    else:
                        from mino_nexus.loop.email_login_auto import run_email_login_ui_chain

                        chain_em = run_email_login_ui_chain(
                            proxy,
                            ctx,
                            turn_seq=seq,
                            instruction=str(cur.instruction or ""),
                            intents_done=cursor.step_intents_done,
                            history_lines=hist_em,
                            login_module_case=bool(login_module_case),
                        )
                    if chain_em:
                        fail_msg = _login_chain_guard(chain_em, cursor=cursor)
                        if fail_msg:
                            return _leave(status="fail", summary=fail_msg, reason_code="login_program_incomplete")
                        _consume_login_chain(
                            chain_em,
                            seq=seq,
                            cursor=cursor,
                            rec=rec,
                            emit=emit,
                            thumb=thumb,
                            screen_fp=str(screen_fp or ""),
                            login_flow_step=True,
                            fuse_web=_progress_gate_web_kwargs(ctx, proxy),
                            writer=writer,
                        )
                        cursor.email_tab_guard_streak = 0
                        if writer:
                            writer.append(
                                "turn/end",
                                {
                                    "decision_cap": "email_login_chain",
                                    "decision_status": "pass",
                                    "guard_remediate": True,
                                },
                            )
                        continue
                    from mino_nexus.services.account_credential_text import lease_email_address
                    from mino_nexus.loop.device_execute_params import enrich_web_input_text_params
                    from mino_nexus.loop.local_executors import _dispatch_device

                    try:
                        from mino_nexus.navigation.geometry.hierarchy_slots import capture

                        _snap_em = capture(proxy, turn_id=int(seq or 0))
                        if _snap_em.ok and _snap_em.nodes:
                            setattr(
                                ctx,
                                "nav_hierarchy_nodes",
                                [n for n in _snap_em.nodes if isinstance(n, dict)],
                            )
                    except Exception:
                        pass
                    _acc = dict(getattr(ctx, "picked_account", None) or {})
                    _em = lease_email_address(_acc)
                    if _em:
                        _p = enrich_web_input_text_params(
                            {"field": "email", "text": _em}, ctx
                        )
                        if _p.get("x") is not None and _p.get("y") is not None:
                            _res = _dispatch_device(
                                proxy,
                                ctx=ctx,
                                seq=seq,
                                cap="input_text",
                                params=_p,
                                label="守卫：Email 已选，程序填租号邮箱",
                            )
                            _st = (
                                _res.status.value
                                if hasattr(_res.status, "value")
                                else str(_res.status)
                            )
                            _ok = str(_st) in ("pass", EventStatus.PASS.value)
                            _item = {
                                "ok": _ok,
                                "status": _st,
                                "summary": str(_res.summary or _res.error or "input_text"),
                                "capability_id": "input_text",
                                "params": _p,
                            }
                            _consume_login_chain(
                                [_item],
                                seq=seq,
                                cursor=cursor,
                                rec=rec,
                                emit=emit,
                                thumb=thumb,
                                screen_fp=str(screen_fp or ""),
                                login_flow_step=True,
                                fuse_web=_progress_gate_web_kwargs(ctx, proxy),
                                writer=writer,
                            )
                            if _ok:
                                cursor.email_tab_guard_streak = 0
                                cursor.step_intents_done.add("login_email")
                                if writer:
                                    writer.append(
                                        "turn/end",
                                        {
                                            "decision_cap": "input_text",
                                            "decision_status": "pass",
                                            "guard_remediate": True,
                                        },
                                    )
                                continue
                if int(getattr(cursor, "email_tab_guard_streak", 0) or 0) >= 2:
                    stop_msg = cursor.progress_gate.record_fuse_block(
                        "【熔断·守卫】Email 标签已选过仍重复点击；"
                        "程序填邮箱失败时请 signal_ask_human。"
                    )
            if writer:
                writer.append(
                    "turn/end",
                    {"decision_cap": skip_cap, "decision_status": "skipped", "guard": True},
                )
            if isinstance(cursor, StepCursor) and guard_vision_remediate is None:
                from mino_nexus.loop.milestones import read_state

                passed = tuple(sorted(
                    str(row.get("id") or "")
                    for row in (read_state(cursor).get("milestones") or [])
                    if isinstance(row, dict) and str(row.get("status") or "") == "pass"
                ))
                guard_stop = cursor.progress_gate.record_guard_rejection(
                    case_step=int(getattr(cur, "n", 0) or 0),
                    capability_id=cap_id,
                    guard_id=str(guard_verdict.guard_id or skip_cap),
                    passed_milestones=passed,
                    reason=str(guard_verdict.reason or skip_reason),
                )
                if guard_stop:
                    if writer:
                        writer.append("progress/stop", {
                            "reason": guard_stop,
                            "guard_id": str(guard_verdict.guard_id or skip_cap),
                            "cause": str(guard_verdict.reason or skip_reason)[:300],
                        })
                    return _leave(status="fail", summary=guard_stop, reason_code="action_guard_exhausted")
            if writer:
                if stop_msg:
                    writer.append(
                        "progress/stop",
                        {
                            "reason": stop_msg,
                            "fuse_block_streak": cursor.progress_gate.fuse_block_streak,
                        },
                    )
            if stop_msg:
                from mino_nexus.loop.fuse.interrupt_stack import try_push_on_fuse_block

                if try_push_on_fuse_block(cursor, ctx, writer=writer, message=str(stop_msg)[:240]):
                    continue
                return _leave(status="fail", summary=stop_msg, reason_code="target_resolution_failed")
            if guard_vision_remediate is None:
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
            from mino_nexus.catalog.tool_schema import fill_input_text_from_ctx, fill_target_package
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
            from mino_nexus.action_space import assemble_execute_params

            params = assemble_execute_params(
                cap_id,
                params,
                ctx,
                width=int(getattr(shot, "width", 0) or 0),
                height=int(getattr(shot, "height", 0) or 0),
            )
            missing_credential = params.pop("_credential_unresolved", None) if cap_id == "input_text" else None
            if missing_credential:
                if missing_credential == "account_credential_missing":
                    code = "account_credential_missing"
                    summary = "当前账号填写里程碑缺少已租账号的登录凭证，未派发输入；不能把占位说明或显示名当作账号。"
                else:
                    code = "otp_value_missing"
                    summary = "当前验证码填写里程碑缺少已获取的验证码，未派发输入；不能用邮箱或密码代替。"
                if writer:
                    writer.append("input/blocked", {
                        "code": code, "failure_stage": "credential_binding",
                        "execution_started": False, "reason": summary,
                    })
                return _leave(status="blocked", summary=summary, reason_code="execution_contract_blocked")
            if cap_id == "input_text" and params.pop("_focus_unresolved", None):
                from mino_nexus.loop.visual_review import recover_input_focus

                recovery = recover_input_focus(cursor, ctx, reason="focus_reference_missing", writer=writer)
                if recovery == "retry":
                    _log_turn_end(writer, cap="", status="pending")
                    continue
                if recovery == "stop":
                    return _leave(status="blocked", summary=str(cursor.semantic_stop)[:500], reason_code="execution_contract_blocked")
                summary = "当前焦点输入缺少有效的聚焦动作引用，不能静默写入"
                if writer:
                    writer.append(
                        "contract/blocked",
                        {"code": "parameter_contract_mismatch", "reason": summary, "retryable": False},
                    )
                return _leave(status="blocked", summary=summary, reason_code="execution_contract_blocked")
            from mino_nexus.action_space.assemble import visual_point_missing
            from mino_nexus.action_space.scheme import execution_scheme as _run_scheme

            gate = getattr(cursor, "progress_gate", None)
            if _run_scheme(ctx) != "dom" and visual_point_missing(cap_id, params):
                hint = "上一次没有给出坐标，这次必须给出 0–1000 的 x 和 y。"
                cursor.correction_hint = hint
                stop_msg = gate.note_missing_coordinate(cap_id) if gate is not None else None
                if writer:
                    writer.append(
                        "fuse/missing_coordinate",
                        {
                            "capability_id": cap_id,
                            "streak": int(getattr(gate, "missing_coord_streak", 0) or 0),
                        },
                    )
                if stop_msg:
                    return _leave(status="fail", summary=stop_msg[:500], reason_code="target_resolution_failed")
                emit("think", thought=hint, step=seq, status="continue")
                continue
            if gate is not None:
                gate.clear_missing_coordinate()
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
        if hasattr(result, "recovered"):
            mode = str(getattr(result, "mode", "") or "")
            rid = str(getattr(result, "rule_id", "") or "").strip()
            if not rid and cap_id.startswith(RECOVER_PREFIX):
                rid = cap_id[len(RECOVER_PREFIX):]
            advice = str(getattr(result, "advice", "") or "").strip()
            if mode == "advise":
                status_val = "declined" if result.error else "pass"
                summary = advice or (result.summary() if callable(getattr(result, "summary", None)) else str(getattr(result, "error", "") or ""))
                elapsed_ms = 0
                error = result.error or ""
                executor_used = "recovery"
                if summary and isinstance(cursor, StepCursor):
                    cursor.correction_hint = summary[:500]
                if rid:
                    cursor.bump_advise_recovery(rid)
            else:
                status_val = "pass" if result.recovered else ("fail" if result.applied else "skipped")
                summary = result.summary() if callable(getattr(result, "summary", None)) else str(getattr(result, "error", "") or "")
                elapsed_ms = 0
                error = result.error or ""
                executor_used = "recovery"
                if rid:
                    if result.recovered:
                        pass
                    else:
                        cursor.bump_recovery_fail(rid)
            if result.stop_run:
                status_val = "blocked"
            actions = getattr(result, "actions", None)
            if isinstance(actions, list) and actions:
                recovery_extra["recovery_actions"] = actions
            ev_brief = str(getattr(result, "evidence", "") or "").strip()
            if ev_brief and not result.recovered:
                error = f"{error}; {ev_brief}".strip("; ")
        else:
            status_val = result.status.value if hasattr(result.status, "value") else str(result.status)
            summary = result.summary or ""
            elapsed_ms = result.elapsed_ms or 0
            error = result.error or ""
            executor_used = result.executor_used or ""
            if cap_id == "input_text":
                from mino_nexus.loop.flow_block_exit import coalesce_input_text_device_status

                coerced = coalesce_input_text_device_status(
                    status_val,
                    summary=str(summary or error or ""),
                    field=str((params or {}).get("field") or ""),
                    params=params if isinstance(params, dict) else None,
                )
                if coerced != status_val:
                    status_val = coerced
                    if coerced == "fail" and not error:
                        error = summary or "input_text_exit_not_pass"

        raw_resp = getattr(result, "raw_response", None)
        if isinstance(raw_resp, dict):
            if str(raw_resp.get("local_reason") or "") == "fsm_degraded":
                hint = str(raw_resp.get("correction_hint") or "").strip()
                if hint and isinstance(cursor, StepCursor):
                    cursor.correction_hint = hint
                from mino_nexus.loop.nav_onboarding_open_loop import (
                    format_open_loop_hint,
                    note_fsm_degrade_for_open_loop,
                )

                note_fsm_degrade_for_open_loop(
                    ctx,
                    raw_resp=raw_resp,
                    nav_target_hint=str((params or {}).get("to_state") or (params or {}).get("to") or ""),
                )
                if isinstance(cursor, StepCursor) and cur:
                    _ol = format_open_loop_hint(
                        ctx=ctx,
                        precondition=str(
                            getattr(cursor, "precondition", "") or case.get("precondition") or ""
                        ),
                        instruction=str(cur.instruction or ""),
                    )
                    if _ol:
                        cursor.correction_hint = (
                            f"{cursor.correction_hint}\n{_ol}".strip()
                            if cursor.correction_hint
                            else _ol
                        )
            nav_attempt = raw_resp.get("nav_attempt")
            if isinstance(nav_attempt, dict) and writer:
                writer.append("nav/attempt", nav_attempt)

        if cap_id in ("fsm_navigate", "recover_fsm_navigate"):
            nav_key = str(
                (params or {}).get("to_state")
                or (params or {}).get("to")
                or (params or {}).get("selector_text")
                or ""
            )
            if status_val == "declined":
                prev_key = str(getattr(ctx, "fsm_last_decline_key", "") or "")
                prev_fp = str(getattr(ctx, "fsm_last_decline_fp", "") or "")
                if nav_key and nav_key == prev_key and screen_fp_turn == prev_fp:
                    setattr(
                        ctx,
                        "fsm_decline_repeat_streak",
                        int(getattr(ctx, "fsm_decline_repeat_streak", 0) or 0) + 1,
                    )
                else:
                    setattr(ctx, "fsm_last_decline_key", nav_key)
                    setattr(ctx, "fsm_last_decline_fp", screen_fp_turn)
                    setattr(ctx, "fsm_decline_repeat_streak", 0)
                if isinstance(cursor, StepCursor) and cur and (
                    "tab_not_visible" in str(summary or "")
                    or "无底栏" in str(summary or "")
                ):
                    from mino_nexus.loop import step_effect as step_effect_mod

                    _vlm_decl = (
                        decision.vlm_hierarchy
                        if isinstance(decision.vlm_hierarchy, dict)
                        else None
                    )
                    step_effect_mod.maybe_mark_deferred_nav_tab(
                        cursor,
                        instruction=str(cur.instruction or ""),
                        expected=str(cur.expected or ""),
                        localized=dict(getattr(ctx, "nav_localized", None) or {}),
                        nodes=_effect_nodes(nav, ctx, extra_vlm=_vlm_decl),
                        thought=thought,
                        coerce_done=False,
                    )
            elif status_val == "pass" and isinstance(raw_resp, dict):
                nav_attempt = raw_resp.get("nav_attempt") or {}
                if isinstance(nav_attempt, dict) and nav_attempt.get("arrived_at_target"):
                    setattr(ctx, "fsm_decline_repeat_streak", 0)
                    setattr(ctx, "fsm_last_decline_key", "")
                corr = str(raw_resp.get("correction_hint") or "")
                if "已在目标屏" in corr or "signal_done" in corr:
                    if isinstance(cursor, StepCursor):
                        cursor.step_intents_done.add("nav_tab")

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
            from mino_nexus.loop.input_contract import contract_reject_summary, focus_reject_summary

            if cap_id == "tap_element":
                from mino_nexus.loop.visual_review import input_focus_menu_ids, recover_input_focus

                input_menu = input_focus_menu_ids(cursor, ctx)
                if input_menu and "tap_element" in input_menu and "input_text" not in input_menu:
                    recovery = recover_input_focus(
                        cursor, ctx, reason="refocus_click_failed", execution_started=True, writer=writer,
                    )
                    if recovery == "stop":
                        return _leave(status="blocked", summary=str(cursor.semantic_stop)[:500], reason_code="execution_contract_blocked")
            focus_error = focus_reject_summary(str(summary or ""), str(error or ""))
            if cap_id == "input_text" and focus_error and str((params or {}).get("target_mode") or "") == "current_focus":
                from mino_nexus.loop.visual_review import recover_input_focus

                recovery = recover_input_focus(cursor, ctx, reason=focus_error, writer=writer)
                if recovery == "retry":
                    _log_turn_end(writer, cap=cap_id, status="pending")
                    continue
                msg = str(getattr(cursor, "semantic_stop", "") or "") if recovery == "stop" else f"当前焦点无效，无法安全输入：{focus_error}"
                if writer:
                    writer.append("input/blocked", {
                        "code": "focus_target_unavailable", "failure_stage": "focus_validation",
                        "retryable": False, "execution_started": False, "reason": focus_error,
                    })
                return _leave(status="blocked", summary=msg[:500], reason_code="execution_contract_blocked")

            rejected = contract_reject_summary(str(summary or ""), str(error or ""))
            if cap_id == "input_text" and rejected and str((params or {}).get("target_mode") or "") == "current_focus":
                msg = (
                    "节点拒绝当前焦点输入，契约不兼容，已停止。"
                    f"原始原因：{rejected}"
                )
                if writer:
                    writer.append(
                        "contract/blocked",
                        {
                            "code": "contract_unsupported",
                            "failure_stage": "compatibility",
                            "retryable": False,
                            "execution_started": False,
                            "reason": rejected[:300],
                        },
                    )
                return _leave(status="blocked", summary=msg[:500], reason_code="execution_contract_blocked")
            SLog.w(TAG, f"[{scout_run_id[:8]}] step {seq} {event.capability_id} {status_val}: {error or summary}")
            if in_check and cap_id == "assert_visual":
                fail_summary = f"步骤 {cur.n} 预期未成立：{cur.expected}。{summary}".strip()
                return _leave(status="fail", summary=fail_summary, reason_code="check_evidence_incomplete")
            if (
                not in_check and isinstance(cursor, StepCursor)
            ):
                from mino_nexus.loop.step_phase_fsm import (
                    first_active_milestone_id,
                    mark_milestone_program_fail,
                    milestone_phase_active,
                )

                if milestone_phase_active():
                    mid = first_active_milestone_id(cursor)
                    if mid:
                        mark_milestone_program_fail(
                            cursor,
                            milestone_id=mid,
                            reason=str(error or summary or "")[:300],
                            cap_id=cap_id,
                            writer=writer,
                        )
            if not in_check:
                from mino_nexus.loop.milestones import note_tool_fail_milestone
                from mino_nexus.loop.vision_flags import execution_phase_active

                if execution_phase_active():
                    note_tool_fail_milestone(
                        cursor,
                        ctx,
                        capability_id=cap_id,
                        summary=str(error or summary or "")[:300],
                        writer=writer,
                    )
                if cap_id == "input_text":
                    fld_fail = str((params or {}).get("field") or "").lower()
                    summ_fail = str(error or summary or "")
                    if fld_fail in ("sms_code", "验证码", "otp"):
                        mis_email = bool(
                            re.search(r"点击", summ_fail)
                            and re.search(r"@|gmail|\.com", summ_fail, re.I)
                        )
                        if mis_email:
                            streak = (
                                int(getattr(cursor, "otp_fill_device_fail_streak", 0) or 0) + 1
                            )
                            cursor.otp_fill_device_fail_streak = streak
                            cursor.correction_hint = (
                                "验证码须填入「6-Digit Code」框：先 tap 验证码框，"
                                f"再 input_text(field=sms_code)（已连续误点邮箱 {streak} 次）。"
                            )
                            if streak >= 5:
                                return _leave(
                                    status="fail",
                                    summary=(
                                        f"验证码填入连续失败 {streak} 次："
                                        "input_text 落在邮箱框。请检查 Web 坐标或 signal_ask_human。"
                                    ),
                                 reason_code="input_target_mismatch")

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
            from mino_nexus.loop.milestone_orchestrator import in_progress_milestone_id
            from mino_nexus.loop.visual_review import input_focus_field, input_focus_milestone_id

            focus_target = (
                input_focus_milestone_id(cursor) or str(in_progress_milestone_id(cursor) or "")
            ) if isinstance(cursor, StepCursor) else ""
            tap_field = input_focus_field(cursor, ctx) or str(params.get("field") or "").strip().lower()
            tap_target = params.get("target") if isinstance(params.get("target"), dict) else {}
            input_click = (
                bool(input_focus_milestone_id(cursor))
                or focus_target in ("account_field", "otp_field", "prep_tap_input_box")
                or tap_field in ("email", "login_email", "sms_code", "otp", "phone", "password")
                or str(tap_target.get("role") or "").strip().lower() in ("input", "textbox", "searchbox")
            )
            # Scout 以 EXECUTE.step_idx 作为 tap 的 focus 编号并精确比对。step_idx 现在是
            # RouterProxy 按 run 分配的线上序号（_next_wire_step_idx），只有回执里的才是真值；
            # 缺少真实回执时不猜编号，由输入菜单要求重新聚焦。
            wire_idx = None
            raw_res = getattr(result, "raw_response", None)
            if isinstance(raw_res, dict):
                ref_obj = raw_res.get("action_ref") if isinstance(raw_res.get("action_ref"), dict) else {}
                sc = raw_res.get("scout") if isinstance(raw_res.get("scout"), dict) else {}
                for cand in (ref_obj.get("step_idx"), sc.get("step_idx")):
                    if isinstance(cand, int) and not isinstance(cand, bool) and cand > 0:
                        wire_idx = cand
                        break
            setattr(
                ctx, "focus_action_ref",
                f"focus:{scout_run_id}:{wire_idx}" if input_click and wire_idx is not None else "",
            )
            setattr(ctx, "focus_action_field", tap_field if input_click else "")
            if writer and input_click and wire_idx is not None:
                writer.append("input/focus_bound", {
                    "milestone_id": focus_target, "field": tap_field,
                    "source": "current_input_pair", "wire_step_idx": wire_idx,
                    "verification_status": "pending",
                })
            from mino_nexus.action_space.scheme import execution_scheme

            setattr(ctx, "focus_action_scheme", execution_scheme(ctx) if input_click and wire_idx is not None else "")
        if status_val == "pass" and cap_id in (
            "launch_app", "open_url", "open_app", "fsm_navigate", "reload_page", "back", "switch_window",
        ):
            setattr(ctx, "focus_action_ref", "")
            setattr(ctx, "focus_action_field", "")
            setattr(ctx, "focus_action_scheme", "")
        if cap_id in ("tap_element", "multi_tap", "long_press_element", "swipe_direction",
                      "swipe_element_to_element", "press_key", "input_text") and not (
            cap_id == "tap_element" and status_val == "pass"
        ):
            # 点击失败、其他触摸/键盘动作以及一次写入后均不能沿用旧焦点。
            setattr(ctx, "focus_action_ref", "")
            setattr(ctx, "focus_action_field", "")
            setattr(ctx, "focus_action_scheme", "")

        if isinstance(cursor, StepCursor):
            from mino_nexus.loop.milestone_orchestrator import in_progress_milestone_id
            from mino_nexus.services.login_evidence_policy import clear_submission
            binding_focus = in_progress_milestone_id(cursor)
            if ((binding_focus == "account_fill" and cap_id == "input_text")
                    or (binding_focus == "submit_login" and status_val != "pass")
                    or cap_id in {"clear_app_cache", "system_pkg_clear"}):
                clear_submission(ctx)
        if status_val == "pass" and cap_id == "clear_app_cache":
            setattr(ctx, "prep_clear_done", True)
        if status_val == 'pass' and isinstance(cursor, StepCursor):
            from mino_nexus.action_space.scheme import execution_scheme as _scheme_now
            from mino_nexus.loop.channel_observation import bind_fresh_observation
            from mino_nexus.loop.milestone_orchestrator import in_progress_milestone_id
            from mino_nexus.loop.milestones import note_tool_pass_milestone
            from mino_nexus.loop.visual_review import defer_visual_pass, remember_visual_prior

            visual_now = _scheme_now(ctx) == "visual"
            if redo_turn:
                defer_visual_pass(
                    cursor,
                    capability_id=cap_id,
                    params=params if isinstance(params, dict) else None,
                    summary=summary,
                    writer=writer,
                    ctx=ctx,
                )
                setattr(cursor, "visual_turn_pending", False)
            else:
                focus_before = in_progress_milestone_id(cursor)
                deferred = defer_visual_pass(
                    cursor,
                    capability_id=cap_id,
                    params=params if isinstance(params, dict) else None,
                    summary=summary,
                    writer=writer,
                    ctx=ctx,
                )
                if not deferred:
                    try:
                        from mino_nexus.loop.binding_cache import remember_success

                        remember_success(
                            cursor,
                            ctx,
                            params=params if isinstance(params, dict) else None,
                            capability_id=cap_id,
                            writer=writer,
                        )
                    except Exception:  # noqa: BLE001
                        pass
                    if cap_id == "input_text":
                        bind_fresh_observation(ctx, proxy)
                    noted = note_tool_pass_milestone(
                        cursor,
                        capability_id=cap_id,
                        writer=writer,
                        params=params if isinstance(params, dict) else None,
                        tool_summary=summary,
                        ctx=ctx,
                    )
                    if noted and visual_now and cap_id == "tap_element" and focus_before:
                        remember_visual_prior(
                            cursor,
                            milestone_id=focus_before,
                            capability_id=cap_id,
                            params=params if isinstance(params, dict) else None,
                            summary=summary,
                        )
                    stop = str(getattr(cursor, "semantic_stop", "") or "").strip()
                    if stop:
                        return _leave(status="fail", summary=stop[:500], reason_code=str(getattr(cursor, "review_contract_reason", "") or "action_result_unverified"))
                    if in_prep:
                        from mino_nexus.loop.prep_program_plan import sync_prep_internal_milestones

                        sync_prep_internal_milestones(cursor, ctx)
                    from mino_nexus.loop.step_phase_fsm import try_transition_after_tool_pass

                    fsm_tr = try_transition_after_tool_pass(
                        cursor,
                        ctx,
                        writer=writer,
                        in_prep=in_prep,
                        in_check=in_check,
                    )
                    if fsm_tr == "check_to_case_done":
                        return _leave_case(cursor)
                    if fsm_tr:
                        _log_turn_end(writer, cap=cap_id, status=status_val)
                        continue

        if status_val == "pass" and cap_id in PROGRESS_CAPS and not recovery_action and not str(cap_id).startswith(RECOVER_PREFIX):
            count_nav = True
            if cap_id in ("fsm_navigate", "recover_fsm_navigate"):
                raw_nav = getattr(result, "raw_response", None) or {}
                if isinstance(raw_nav, dict):
                    nav_attempt = raw_nav.get("nav_attempt") or {}
                    count_nav = bool(nav_attempt.get("arrived_at_target"))
            cursor.record_step_op(
                cap_id,
                params=params if isinstance(params, dict) else None,
                count_nav_intent=count_nav,
            )
            if (
                isinstance(cursor, StepCursor)
                and "logout" in set(getattr(cursor, "step_intents_done", ()) or ())
                and not getattr(cursor, "ui_logout_committed", False)
            ):
                from mino_nexus.services.resource_transition import emit_device_logout

                emit_device_logout(ctx, source="ui_logout")
                cursor.ui_logout_committed = True
            if isinstance(cursor, StepCursor) and cursor.phase == "do":
                _finish_hist = list(history[-48:])
                if status_val == "pass" and cap_id:
                    _finish_hist.append(
                        f"{seq}. {cap_id} → pass: {str(summary or '')[:160]}"
                    )
                cursor.bind_finish_context(ctx, _finish_hist)
            if isinstance(cursor, StepCursor) and cursor.phase == "do":
                from mino_nexus.loop.vision_plan import hold_visual_do_exit_until_effect

                hold_visual_do_exit_until_effect(cursor, ctx)
            if (
                isinstance(cursor, StepCursor)
                and cursor.try_auto_finish_do_when_milestones_met(ctx, writer=writer)
            ):
                rec(
                    seq,
                    capability_id="signal_done",
                    status="skipped",
                    summary="子里程碑已齐，自动进入校验",
                    thought="",
                    thumb=thumb,
                )
                emit(
                    "result",
                    thought="",
                    step=seq,
                    capability_id="signal_done",
                    status="skipped",
                    summary="子里程碑已齐，自动进入校验",
                    thumb=thumb,
                )
                _log_turn_end(writer, cap="signal_done", status="skipped")
                continue
        if (
            status_val == "pass"
            and cap_id == "lease_account"
            and isinstance(cursor, StepCursor)
            and cursor.phase == "prep"
        ):
            setattr(ctx, "prep_lease_account_done", True)
            from mino_nexus.loop.milestones import read_state as _read_ms

            _ms_now = (_read_ms(cursor).get("milestones") or [])
            _filling = any(
                isinstance(row, dict)
                and str(row.get("id") or "") == "account_fill"
                and str(row.get("status") or "pending") not in ("pass", "skipped", "failed")
                for row in _ms_now
            )
            # 填写登录账号时的租号只取凭证。资产转移只属于前置里的租号步。
            if not _filling:
                if in_prep:
                    setattr(ctx, "app_launch_confirmed", False)
                from mino_nexus.loop.observe.session_persist import mark_session_dirty

                # Selecting credentials did not clear cache or log out the UI.
                mark_session_dirty(ctx, reason="account_lease_changed")
            from mino_nexus.loop.observe.session_persist import effective_session_block

            inspect_slots["session_block"] = effective_session_block(ctx, "")
        elif status_val == "pass":
            from mino_nexus.services.resource_transition_engine import maybe_fire_cap_transition

            maybe_fire_cap_transition(ctx, cap_id, status_val)
        if status_val == "pass" and cap_id in ("launch_app", "open_app", "open_url"):
            setattr(ctx, "app_launch_confirmed", True)
            from mino_nexus.loop.launch_grace import stamp_launch_grace
            from mino_nexus.runtime.run_context import is_web_slot
            from mino_nexus.services.nav_target_scope import _looks_like_url

            stamp_launch_grace(ctx)
            if is_web_slot(str(getattr(ctx, "sn", "") or ""), str(getattr(ctx, "platform", "") or "")):
                tgt = str(getattr(ctx, "target_package", "") or "").strip()
                if tgt and _looks_like_url(tgt):
                    setattr(ctx, "app_foreground", "yes")
        if status_val == "pass" and cap_id == "press_key":
            key = str((params or {}).get("key") or "").strip().upper()
            if key in ("BACK", "BACK_KEY"):
                setattr(ctx, "recovery_allow_back", False)

        if status_val == "pass" and cap_id == "tap_element":
            from mino_nexus.loop.nav_onboarding_open_loop import clear_open_loop_if_tabs_visible

            clear_open_loop_if_tabs_visible(ctx)
        post_fp = ""
        from mino_nexus.action_space.scheme import execution_scheme

        visual_scheme = execution_scheme(ctx) == "visual"
        if status_val == "pass" and (fuseable_cap(cap_id) or cap_id in ("fsm_navigate", "recover_fsm_navigate")):
            if visual_scheme:
                # 纯视觉的进展键不看像素。操作后再截一张会占住截图线程，下一回合决策图报 scout timeout。
                post_fp = screen_fp
            else:
                post_shot = proxy.observe("screenshot", force_fresh=True)
                post_fp = _screen_fp(
                    post_shot, inspect_slots.get("hierarchy_text") or "", ctx=ctx
                )
            _lf_record = False
            if isinstance(cursor, StepCursor) and cur and cursor.phase == "do":
                from mino_nexus.loop.step_contract import instruction_allows_login_flow

                _lf_record = instruction_allows_login_flow(
                    str(cur.instruction or ""),
                    login_module_case=bool(login_module_case),
                )
            _focus_mid = ""
            if isinstance(cursor, StepCursor):
                from mino_nexus.loop.milestone_orchestrator import in_progress_milestone
                from mino_nexus.loop.milestones import read_state

                _focus_row = in_progress_milestone(read_state(cursor))
                if isinstance(_focus_row, dict):
                    _focus_mid = str(_focus_row.get("id") or "")
            cursor.progress_gate.record_pass(
                cap_id=cap_id if fuseable_cap(cap_id) else "tap_element",
                params=params if fuseable_cap(cap_id) else {},
                pre_fp=screen_fp,
                post_fp=post_fp,
                intents_done=set(getattr(cursor, "step_intents_done", None) or set()),
                login_flow_step=_lf_record,
                fingerprint_compared=not visual_scheme,
                milestone_id=_focus_mid,
                **_progress_gate_web_kwargs(ctx, proxy),
            )
            if isinstance(cursor, StepCursor):
                cursor.clear_recovery_block()
                if cursor.phase == "prep" and cap_id not in ("wait_ms", "wait_screen_ready"):
                    cursor.prep_guard_streak = 0
                if cap_id == "swipe_direction" and status_val == "pass":
                    cursor.note_swipe_pass(
                        direction=str((params or {}).get("direction") or ""),
                        pre_fp=screen_fp,
                        post_fp=post_fp,
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
            _tap_sel = str(
                (params or {}).get("selector_text")
                or (params or {}).get("text")
                or (params or {}).get("content_desc")
                or summary
                or ""
            )
            from mino_nexus.loop.login_submit import (
                account_session_logged_in,
                login_submit_tap_matches,
                sms_send_tap_matches,
            )

            send_just = bool(getattr(cursor, "send_code_tap_passed", False))
            setattr(cursor, "send_code_tap_passed", False)

            if sms_send_tap_matches(_tap_sel):
                from mino_nexus.loop.login_verification import (
                    is_web_email_login,
                    record_verification_send,
                )

                from mino_nexus.action_space.scheme import execution_scheme as action_scheme

                pending_send_review = action_scheme(ctx) == "visual"
                if pending_send_review:
                    cursor.correction_hint = "已点击发送；须先回看确认本次发码被接受，才能取码。"
                else:
                    record_verification_send(ctx)
                    cursor.step_intents_done.add("sms_send")
                    cursor.login_post_sms_pending = True
                    cursor.login_post_sms_stall = 0
                    update_auth_state(ctx, login_auto_submit_attempts=0)
                if not pending_send_review and is_web_email_login(ctx):
                    cursor.correction_hint = (
                        "已向邮箱发码（Send）。下一轮程序先取验证码；取到后点验证码输入框，"
                        "程序按该次点击的坐标输入。勿重复点 Send，勿在点框前输入。"
                    )
                elif not pending_send_review:
                    cursor.correction_hint = (
                        "已点发送验证码；下一轮程序链将 get_otp 并填码，再点登录。"
                        "本 turn 勿重复发码或提前 get_otp。"
                    )
            elif login_submit_tap_matches(_tap_sel) and not send_just:
                cursor.step_intents_done.add("login_submit")
                if account_session_logged_in(ctx):
                    cursor.login_continue_stall = 0
                    cursor.step_goal_met = True
                    cursor.correction_hint = (
                        "账号已登录（session=logged_in）；本步操作已达成，请立即 signal_done，"
                        "勿再点 Continue/登录/头像。"
                    )
                    cursor.bind_finish_context(ctx, list(history[-24:]))
                    cursor.try_auto_finish_do_when_milestones_met(ctx, writer=writer)
                else:
                    fp_now = str(post_fp or screen_fp or "")
                    last = str(getattr(cursor, "_last_continue_screen_fp", "") or "")
                    if fp_now and last and fp_now == last:
                        cursor.login_continue_stall = int(
                            getattr(cursor, "login_continue_stall", 0) or 0
                        ) + 1
                    else:
                        cursor.login_continue_stall = 0
                    if fp_now:
                        setattr(cursor, "_last_continue_screen_fp", fp_now)
                    if int(getattr(cursor, "login_continue_stall", 0) or 0) >= 2:
                        cursor.correction_hint = (
                            "Continue 已连点但账号仍未登录；勿再点 Continue，"
                            "请 signal_ask_human 或核对验证码/邮箱。"
                        )
            elif (
                re.search(r"\bEmail\b|邮箱", _tap_sel, re.I)
                or (
                    re.search(r"log\s*in|sign\s*in|登录", _tap_sel, re.I)
                    and not re.search(r"email|邮箱", _tap_sel, re.I)
                )
            ):
                from mino_nexus.loop.email_login_auto import run_email_login_ui_chain
                from mino_nexus.loop.step_contract import instruction_allows_login_flow
                from mino_nexus.loop.step_flow_scope import login_flow_allowed

                _lf_em, _ = login_flow_allowed(
                    cursor=cursor,
                    phase="do",
                    instruction=str(cur.instruction or ""),
                    expected=str(cur.expected or ""),
                )
                if _lf_em and not getattr(cursor, "login_chain_broken", False):
                    from mino_nexus.loop.login_account_lease import try_lease_on_login_surface

                    _ok_lease, _lease_err, _lease_brief = try_lease_on_login_surface(
                        ctx, case
                    )
                    if not _ok_lease and _lease_err:
                        cursor.correction_hint = (
                            f"登录界面租号失败：{_lease_err}；"
                            "请检查号池或 signal_ask_human。"
                        )
                    hist_em = list(history[-24:])
                    if status_val == "pass":
                        hist_em.append(
                            f"{seq}. tap_element → pass: {_tap_sel[:120]}"
                        )
                    if _ok_lease and _lease_brief:
                        hist_em.append(
                            f"{seq}. lease_account → pass: {_lease_brief[:160]}"
                        )
                    chain_em = run_email_login_ui_chain(
                        proxy,
                        ctx,
                        turn_seq=seq,
                        instruction=str(cur.instruction or ""),
                        intents_done=cursor.step_intents_done,
                        history_lines=hist_em,
                        login_module_case=bool(login_module_case),
                    )
                    if chain_em:
                        fail_msg = _login_chain_guard(chain_em, cursor=cursor)
                        if fail_msg:
                            return _leave(status="fail", summary=fail_msg, reason_code="login_program_incomplete")
                        lc, ls = _consume_login_chain(
                            chain_em,
                            seq=seq,
                            cursor=cursor,
                            rec=rec,
                            emit=emit,
                            thumb=thumb,
                            screen_fp=str(post_fp or screen_fp or ""),
                            login_flow_step=instruction_allows_login_flow(
                                str(cur.instruction or ""),
                                login_module_case=bool(login_module_case),
                            ),
                            fuse_web=_progress_gate_web_kwargs(ctx, proxy),
                            writer=writer,
                        )
                        for item in chain_em:
                            if str(item.get("capability_id") or "") == "input_text" and item.get("ok"):
                                cursor.record_step_op(
                                    "input_text",
                                    params={"field": "email"},
                                )
                                cursor.step_intents_done.add("login_email")
                            if str(item.get("step_id") or "") == "send_code" and item.get("ok"):
                                cursor.step_intents_done.add("sms_send")
                                cursor.login_post_sms_pending = True
                                cursor.login_post_sms_stall = 0
                        _log_turn_end(writer, cap=lc or "email_login", status=ls)
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

        if (
            cap_id == "signal_done"
            and status_val == "pass"
            and isinstance(cursor, StepCursor)
            and cursor.phase == "do"
        ):
            from mino_nexus.loop.step_phase_fsm import (
                handle_model_signal_done_blocked,
                milestone_phase_active,
            )

            if milestone_phase_active():
                act, tr = handle_model_signal_done_blocked(
                    cursor,
                    ctx,
                    writer=writer,
                    in_prep=False,
                    in_check=False,
                )
                if act == "transition" and tr == "check_to_case_done":
                    return _leave_case(cursor)
                _log_turn_end(writer, cap="signal_done", status="skipped")
                continue
            if cur:
                from mino_nexus.loop import step_effect as step_effect_mod

                _vlm_sd = (
                    decision.vlm_hierarchy if isinstance(decision.vlm_hierarchy, dict) else None
                )
                step_effect_mod.maybe_mark_deferred_nav_tab(
                    cursor,
                    instruction=str(cur.instruction or ""),
                    expected=str(cur.expected or ""),
                    localized=dict(getattr(ctx, "nav_localized", None) or {}),
                    nodes=_effect_nodes(nav, ctx, extra_vlm=_vlm_sd),
                    thought=thought,
                    coerce_done=True,
                )
            cursor.enter_check()
            _log_turn_end(writer, cap="signal_done", status="pass")
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


def _finish_case(emit, *, cursor: StepCursor, steps: list, t0: float, pack=None) -> dict[str, Any]:
    if cursor.all_uncheckable() or not cursor.saw_assert:
        summary = "未写预期，无法校验"
        return _finish(emit, status="unverifiable", summary=summary, steps=steps, t0=t0, pack=pack)
    last = next((s for s in reversed(steps) if s.get("capability_id") == "assert_visual"), None)
    summary = (last or {}).get("summary") or "全部步骤已校验"
    return _finish(emit, status="pass", summary=summary, steps=steps, t0=t0, pack=pack)
