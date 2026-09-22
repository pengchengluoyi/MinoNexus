"""单用例看图闭环。prep / do / check 都由 Agent 看图决策。"""
from __future__ import annotations

import re
import time
from typing import Any, Optional

from mino_nexus.loop.action_fuse import fuseable_cap
from mino_nexus.loop.step_pointer import cap_clears_repeat_tap
from mino_nexus.ai.planner import decide_next_action
from mino_nexus.ai.schemas import AgentAction, AgentDecision
from mino_nexus.catalog.exec_classes import MUTATE_CAPS, PROGRESS_CAPS
from mino_nexus.loop.registry import apply_force_case_expectation, run_guards
from mino_nexus.loop.inspections import (
    _session_block_is_conclusive,
    expand_named_knowledge,
    llm_session_block,
    match_stuck_docs,
    refresh_session_block,
    reconcile_session_block_with_hierarchy,
    run_inspections,
)
from mino_nexus.services.app_intel import context_pack_for_step, log_context_pack
from mino_nexus.runtime.session_gate import (
    compile_login_session_hint,
    compile_otp_prep_hint,
    compile_sms_send_hint,
    ensure_case_scene,
    is_login_module_case,
)
from mino_nexus.runtime.platform_gate import platform_skip_reason
from mino_nexus.loop.sop_runtime import merge_phase_tool_kinds, normalize_inspections, normalize_phases, phase_for_id
from mino_nexus.core.log import SLog
from mino_nexus.loop.agent_stream import emit_agent_event, make_thumb
from mino_nexus.loop.session_log import SessionWriter, bind_writer, open_session
from mino_nexus.loop.local_executors import dispatch_local, is_local_cap
from mino_nexus.loop.nav_runtime import NavRuntime
from mino_nexus.loop.recovery import apply_rule, ensure_target_app_foreground, recover_if_needed, RuleMatch
from mino_nexus.loop.router_proxy import RouterProxy
from mino_nexus.loop.skill_trace import envelope as skill_envelope
from mino_nexus.loop.step_pointer import (
    StepCursor,
    _expected_defers_to_check,
    build_seq_nodes,
    compute_case_wall_budget_sec,
    enrich_assert_expectation,
    screen_fingerprint,
    tap_params_guard_summary,
)
from mino_nexus.catalog import registry as catalog
from mino_nexus.core.protocol import EventStatus
from mino_nexus.services.run_store import line_text, report_run_id
from mino_nexus.runtime.run_context import build_run_context
from mino_nexus.loop.web_env import agent_step_idx, cleanup_after_case, frame_step, reset_before_case
from mino_nexus.loop.app_env import launch_if_hierarchy_away, reset_native_app_before_case
from mino_nexus.core.schemas import EventResult, PlanEvent

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
    from mino_nexus.loop.recovery import resolve_app_foreground_guard

    fg = resolve_app_foreground_guard(
        ctx,
        router,
        nodes=nodes,
        target_package=str(target_pkg or getattr(ctx, "target_package", "") or ""),
    )
    setattr(ctx, "system_overlay", str(fg.get("system_overlay") or ""))
    setattr(ctx, "app_foreground", str(fg.get("app_foreground") or ""))
    return fg


def _screen_fp(shot, hierarchy_text: str = "") -> str:
    return screen_fingerprint(
        hierarchy_text=hierarchy_text,
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


def _log_program_tool(
    writer: SessionWriter | None,
    *,
    capability_id: str,
    params: dict[str, Any],
    status: str,
    summary: str,
    executor_used: str = "adb",
    source: str = "program",
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
        },
    )


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
) -> None:
    if writer is None:
        return
    cur_node = cursor.current()
    case_step = 0 if cursor.phase == "prep" else (int(cur_node.n) if cur_node else 0)
    writer.append(
        "context/slots",
        {
            "slots": {
                "phase": cursor.phase,
                "case_step": case_step,
                "goal": cursor.decide_goal(),
                "checkpoints_block": cursor.prompt_block(),
                "success_criteria": cursor.decide_success(),
                "session_block": inspect_slots.get("session_block") or "",
                "hierarchy_text": inspect_slots.get("hierarchy_text") or "",
                "knowledge_hint": inspect_slots.get("knowledge_hint") or "",
                "knowledge_body": inspect_slots.get("knowledge_body") or "",
                "nav_assist": inspect_slots.get("nav_assist") or "",
                "doc_context": inspect_slots.get("doc_context") or "",
                "history_block": _history(history),
                "tool_kinds": list(phase_tool_kinds or []),
                "menu_count": len(menu),
                "cap_ids": [str(c.get("id") or "") for c in menu if c.get("id")][:80],
            },
        },
    )


def _finish(emit, *, status: str, summary: str, steps: list, t0: float, pack=None) -> dict[str, Any]:
    extra = pack(status=status, summary=summary) if callable(pack) else {}
    emit("done", overall=status, status=status, summary=summary)
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
) -> dict[str, Any]:
    from mino_nexus.ai import dispatch_log as dispatch

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
    if playbook:
        ctx.playbook = playbook
    scout_run_id = stream_id
    ctx.scout_run_id = scout_run_id
    ctx.case_seq = case_seq
    env_brief = str(run_env_brief or "").strip()
    if env_brief:
        ctx.env_label = env_brief
        ctx.env_fact = {"brief": env_brief, "confirmed": True}
    run_type = "manual"
    try:
        from mino_nexus.services import run_store as _rs

        _doc = _rs.get(run_id)
        if _doc:
            ctx.env_profile = str(_doc.get("env_profile") or ctx.env_profile or "test")
            # GuardGate stop 时 ask_human 还是 give_up 取决于它（设计稿 §11.5）
            run_type = str(_doc.get("run_type") or "manual").lower()
            if run_type == "explore":
                is_explore_case = True
    except Exception:
        pass
    proxy = RouterProxy(
        sn,
        run_id=scout_run_id,
        task_id=run_id,
        target_package=package,
        playwright_headless=playwright_headless,
    )
    history: list[str] = []
    steps: list[dict[str, Any]] = []

    def emit(phase: str, **extra: Any) -> None:
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
    )
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
            **({"parent_session_id": parent_session_id} if parent_session_id else {}),
            **({"fork_from_turn": fork_state.get("fork_from_turn")} if fork_state else {}),
        },
    )
    if fork_state and writer:
        writer.append("session/fork", dict(fork_state))
    outcome: dict[str, Any] = {"status": "fail", "summary": "未开始", "steps": steps}
    plat_skip = platform_skip_reason(case, str(getattr(ctx, "platform", "") or "android"))
    try:
        with bind_writer(writer):
            if plat_skip:
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
                    {"reason": plat_skip, "device_platform": getattr(ctx, "platform", "")},
                )
                _skip_emit("start", thought="渠道校验", step=0, goal=overview)
                outcome = _finish(_skip_emit, status="skip", summary=plat_skip, steps=steps, t0=t0)
            else:
                outcome = _run_loop(
                    emit=emit,
                    ctx=ctx,
                    proxy=proxy,
                    case=case,
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
    except Exception as exc:
        outcome = {
            "status": "fail",
            "summary": str(exc)[:240] or "执行异常",
            "steps": steps,
        }
        raise
    finally:
        from mino_nexus.services.account_facet_commit import commit_case_facet_effects

        if not writer._closed:
            step_cursor = getattr(ctx, "_step_cursor", None)
            if step_cursor is not None:
                from mino_nexus.loop.ops_guard_diag import session_end_guard_summary

                gs = session_end_guard_summary(step_cursor)
                if gs.get("guard_block_counts") or gs.get("last_guard_block"):
                    writer.append("ops/guard_summary", gs)
            writer.close(
                status=str(outcome.get("status") or "fail"),
                summary=str(outcome.get("summary") or ""),
                step_count=len(outcome.get("steps") or []),
            )
        commit_case_facet_effects(
            ctx,
            case,
            status=str(outcome.get("status") or ""),
            outcome=outcome,
        )
        from mino_nexus.services.account_lease import release_ctx_lease

        release_ctx_lease(ctx)
        cleanup_after_case(proxy, ctx, run_id=scout_run_id, case_seq=case_seq, case=case)
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
) -> tuple[str, str]:
    last_cap, last_st = "", "fail"
    from mino_nexus.loop.action_fuse import fuseable_cap

    intents = set(getattr(cursor, "step_intents_done", None) or set())
    for item in chain:
        cap = str(item.get("capability_id") or "noop")
        st = str(item.get("status") or "fail")
        summ = str(item.get("summary") or "")
        if item.get("ran_get_otp"):
            cursor.record_step_op("get_otp")
        if cap == "input_text" and item.get("ok"):
            cursor.record_step_op("input_text", params={"field": "sms_code"})
        if cap == "tap_element" and item.get("ok"):
            cursor.record_step_op("tap_element", params={"selector_text": "登录"})
        if item.get("ok") and fuseable_cap(cap):
            params: dict[str, Any] = {}
            if cap == "input_text":
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


def _ensure_web_page(proxy: RouterProxy, ctx, *, run_id: str, case_seq: int = 0) -> Optional[dict[str, Any]]:
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
    }



def _run_action(*, event: PlanEvent, shot, proxy: RouterProxy, ctx, scout_run_id: str, case_seq: int, seq: int) -> Any:
    cap = event.capability_id
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
    if is_local_cap(cap):
        return dispatch_local(
            event,
            shot=shot,
            ctx=ctx,
            router=proxy,
            target_package=str(getattr(ctx, "target_package", "") or ""),
        )
    return proxy.dispatch(
        event,
        run_id=scout_run_id,
        step_idx=agent_step_idx(case_seq, seq),
    )


def _run_loop(
    *,
    emit,
    ctx,
    proxy: RouterProxy,
    case: dict[str, Any],
    cid: str,
    overview: str,
    provider_id: str,
    cancel_check,
    history: list[str],
    steps: list[dict[str, Any]],
    t0: float,
    scout_run_id: str,
    case_seq: int = 0,
    writer: SessionWriter | None = None,
    fork_state: dict[str, Any] | None = None,
    run_env_brief: str = "",
    run_type: str = "manual",
) -> dict[str, Any]:
    from mino_nexus.runtime.menu import available_menu_brief
    from mino_nexus.services.skill_store import get_skill

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
    setattr(ctx, "_step_cursor", cursor)
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

    display_guard: Optional[Any] = None
    screen_recovery_exclude: frozenset[str] | None = None
    target_pkg = str(getattr(ctx, "target_package", "") or "")

    def _stop_display_guard() -> None:
        if display_guard is not None:
            display_guard.stop()

    def _leave(*, status: str, summary: str, pack=None):
        _stop_display_guard()
        if nav is not None:
            nav.shutdown(writer=writer, status=status, summary=summary)
        return _finish(emit, status=status, summary=summary, steps=steps, t0=t0, pack=pack or _pack)

    def _leave_case(cursor):
        _stop_display_guard()
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
    ctx.case_scene = ensure_case_scene(case, getattr(ctx, "case_scene", None))
    ctx.case = case
    from mino_nexus.loop.session_scope import begin_case_session_scope

    begin_case_session_scope(ctx, case_id=str(cid or ""), scene=ctx.case_scene)
    from mino_nexus.loop.llm_screenshot_gate import reset_case_llm_image_gate

    reset_case_llm_image_gate(ctx)
    login_module_case = is_login_module_case(case=case, scene=ctx.case_scene)
    if isinstance(cursor, StepCursor):
        cursor.login_module_prompt = login_module_case
    from mino_nexus.loop.session_ensure import ensure_case_account

    ensure_case_account(ctx, case)
    task_env_brief = str(run_env_brief or getattr(ctx, "env_label", "") or "").strip()
    if task_env_brief:
        history.append(f"0. program → info: 本任务运行环境已确认：{task_env_brief}")
    if not is_explore and wall_budget_sec > 0:
        history.append(
            f"0. program → info: 本用例墙钟预算 {wall_budget_sec} 秒"
            f"（前置+操作+校验按步数各 ×{30} 秒）；耗尽即失败，不再按「N 步」截断。"
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

    def emit(phase: str, **extra: Any) -> None:
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
        return _leave(status="fail", summary=summary)

    if not cursor.nodes:
        summary = "用例没有可执行的步骤。"
        return _leave(status="fail", summary=summary)

    reset_before_case(proxy, ctx, run_id=scout_run_id, case_seq=case_seq, case=case)
    from mino_nexus.services.resource_preflight import claim_requires_clear_cache

    _rk = case.get("resource_key") if isinstance(case.get("resource_key"), dict) else None
    _clear_before_launch = claim_requires_clear_cache(
        _rk,
        getattr(ctx, "case_scene", None),
        str(case.get("precondition") or ""),
    )
    cold_started = reset_native_app_before_case(
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
    opened = _ensure_web_page(proxy, ctx, run_id=scout_run_id, case_seq=case_seq)
    if opened:
        if writer:
            writer.append(
                "tool/call",
                {
                    "capability_id": "launch_app",
                    "params": {"url": str(getattr(ctx, "target_package", "") or "")},
                },
            )
        rec(
            0,
            capability_id="launch_app",
            status=str(opened.get("status") or "pass"),
            summary=str(opened.get("summary") or "打开页面"),
            thought="Web 槽截图前先打开被测页",
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
                },
            )
        if opened.get("fatal"):
            summary = opened.get("summary") or "打开页面失败"
            emit("observe", thought=summary, step=0, status="fail")
            return _leave(status="fail", summary=summary)

    from mino_nexus.loop.recovery import recover_if_needed
    from mino_nexus.loop.device_display_guard import DeviceDisplayGuard, platform_needs_display_guard

    if platform_needs_display_guard(ctx):
        try:
            display_guard = DeviceDisplayGuard(
                ctx,
                proxy,
                target_package=target_pkg,
                cancel_check=cancel_check,
            )
            display_guard.start(initial_timeout=90.0)
            screen_recovery_exclude = frozenset({"screen_asleep_or_locked"})
        except Exception as exc:
            SLog.w(TAG, f"DisplayGuard start failed: {exc!r}")
            display_guard = None

    preflight_shot = None
    try:
        from mino_nexus.runtime.run_context import is_web_slot

        if not is_web_slot(str(getattr(ctx, "sn", "") or ""), str(getattr(ctx, "platform", "") or "")):
            preflight_shot = proxy.observe("screenshot", force_fresh=True)
    except Exception:
        preflight_shot = None

    def _log_preflight_recovery(out, *, source: str) -> None:
        cap_id = str(getattr(out, "rule_id", "") or "recovery")
        summary = out.summary() if callable(getattr(out, "summary", None)) else (
            getattr(out, "evidence", "") or getattr(out, "error", "") or cap_id
        )
        st = "pass" if out.recovered else ("skipped" if out.mode == "advise" else "fail")
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
        exclude_rule_ids=screen_recovery_exclude,
    )
    if recovery_out:
        _log_preflight_recovery(recovery_out, source="preflight")

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

    if not str(getattr(ctx, "app_version", "") or "").strip() and target_pkg:
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
        ver_result = proxy.dispatch(ver_event, run_id=scout_run_id, step_idx=frame_step(case_seq, 1))
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
        return _leave(status="cancelled", summary="任务已取消")

    if login_module_case:
        pre_hint = compile_login_session_hint(
            getattr(ctx, "case_scene", None),
            "",
        )
        if pre_hint:
            history.append(f"0. program → info: {pre_hint}")

    for seq in range(1, max_steps + 1):
        turn_decision_cap = ""
        turn_decision_status = ""
        cur_node = cursor.current()
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
        if cancel_check and cancel_check():
            return _leave(status="cancelled", summary="任务已取消")

        if not is_explore and wall_budget_sec > 0 and (time.time() - t0) >= wall_budget_sec:
            summary = f"超过 {wall_budget_sec} 秒仍未完成"
            return _leave(status="fail", summary=summary)

        if cursor.done:
            return _leave_case(cursor)

        if display_guard is not None:
            if cancel_check and cancel_check():
                return _leave(status="cancelled", summary="任务已取消")
            display_guard.wait_ready(timeout=60.0)

        shot = proxy.observe("screenshot", force_fresh=True)
        thumb = make_thumb(shot.image_base64) if shot.has_image() else ""
        if writer and shot.has_image():
            writer.append(
                "observe/screen",
                {
                    "w": shot.width or 0,
                    "h": shot.height or 0,
                    "mime": shot.image_mime or "image/png",
                    "thumb": thumb,
                    "thumb_len": len(thumb or ""),
                },
            )
        if not shot.has_image():
            summary = shot.error or "Scout 截图失败"
            if "任务已取消" in summary:
                return _leave(status="cancelled", summary=summary)
            if "没有打开的页面" in summary:
                summary = "浏览器还没有打开页面。请确认应用填了 Web 地址，或在前置里先打开网址。"
            emit("observe", thought=summary, step=seq, status="fail")
            return _leave(status="fail", summary=summary)

        if nav is not None:
            nav.observe(
                proxy,
                turn_id=seq,
                slot_sink=inspect_slots,
                session_block=inspect_slots.get("session_block") or "",
                screenshot_turn_id=seq,
                writer=writer,
                screenshot=shot,
            )
            _sync_app_foreground_ctx(ctx, nav, target_pkg, proxy)
        elif not is_explore:
            _sync_app_foreground_ctx(ctx, nav, target_pkg, proxy)

        if login_module_case and cursor.phase in ("prep", "do"):
            conclusive = _session_block_is_conclusive(inspect_slots.get("session_block") or "")
            if conclusive:
                ran_case_start_inspection = True
            elif not ran_case_start_inspection or (
                last_phase_seen and last_phase_seen != cursor.phase
            ):
                refresh_session_block(
                    shot=shot,
                    ctx=ctx,
                    case=case,
                    provider_id=provider_id,
                    slot_sink=inspect_slots,
                    force=False,
                    nav=nav,
                    turn_id=seq,
                )
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
            if (
                cursor.phase == "do"
                and bool(getattr(ctx, "session_dirty", False))
                and shot.has_image()
            ):
                refresh_session_block(
                    shot=shot,
                    ctx=ctx,
                    case=case,
                    provider_id=provider_id,
                    slot_sink=inspect_slots,
                    force=True,
                    nav=nav,
                    turn_id=seq,
                )
                ctx.session_dirty = False

        from mino_nexus.loop.session_persist import effective_session_block, execution_context_session_line

        if (
            isinstance(cursor, StepCursor)
            and cursor.phase == "check"
            and cursor.check_session_refresh
            and shot.has_image()
        ):
            refresh_session_block(
                shot=shot,
                ctx=ctx,
                case=case,
                provider_id=provider_id,
                slot_sink=inspect_slots,
                force=True,
                nav=nav,
                turn_id=seq,
            )
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
                    return _leave(status="pass", summary=cursor.summary(), pack=_pack())
            blocked = nav.preflight_block()
            if blocked:
                reason = str(blocked.get("reason") or "租号初态与用例要求不一致")
                if writer:
                    writer.append("turn/end", {"decision_cap": "nav_precondition", "decision_status": "blocked"})
                return _leave(status="blocked", summary=reason)
        elif is_explore:
            cursor.note_observation([])
            if cursor.done:
                return _leave(status="pass", summary=cursor.summary(), pack=_pack())

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
                launched = launch_if_hierarchy_away(
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
                )
                rec(
                    seq,
                    capability_id="launch_app",
                    status=str(launched.get("status") or "pass"),
                    summary=str(launched.get("summary") or ""),
                    thought=str(launched.get("summary") or ""),
                    thumb=thumb,
                    executor_used="adb",
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
                )
                rec(
                    seq,
                    capability_id="launch_app",
                    status=fg_st,
                    summary=fg_summ,
                    thought="程序将被测 App 带到前台",
                    thumb=thumb,
                    executor_used="adb",
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

        screen_fp_turn = _screen_fp(shot, inspect_slots.get("hierarchy_text") or "")
        if not is_explore and isinstance(cursor, StepCursor):
            if cursor.phase == "do" and not cursor.step_start_fp:
                cursor.refresh_step_start_fp(screen_fp_turn)
            cur_probe = cursor.current()
            if cursor.phase == "do" and cur_probe and (
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
                        if "nav_tab" in need_int_probe and (hit or loc_hit) and defer_chk:
                            cursor.step_intents_done.add("nav_tab")
                            cursor.refresh_do_subphase()
                        if (
                            cursor.phase == "do"
                            and step_effect_mod.should_auto_enter_check(
                                loc_hit=bool(loc_hit),
                                probe_hit=bool(hit),
                                expected=str(cur_probe.expected or ""),
                                keywords=keywords,
                                hit_streak=cursor.step_effect_hit_streak,
                                defer_expected_to_check=defer_chk,
                            )
                        ):
                            from mino_nexus.loop.step_contract import step_actions_satisfied
                            from mino_nexus.loop.step_intent import (
                                instruction_required_intents,
                                step_intents_satisfied,
                            )

                            need_int = instruction_required_intents(str(cur_probe.instruction or ""))
                            int_ok, _ = step_intents_satisfied(
                                instruction=str(cur_probe.instruction or ""),
                                intents_done=cursor.step_intents_done,
                            )
                            fam_ok, _fam_msg = step_actions_satisfied(
                                instruction=str(cur_probe.instruction or ""),
                                families_done=cursor.step_action_families,
                                family_counts=cursor.step_family_counts,
                            )
                            work_ok = int_ok if need_int else fam_ok
                            if need_int and "nav_tab" in need_int:
                                if "nav_tab" not in cursor.step_intents_done:
                                    work_ok = False
                            if work_ok and step_effect_mod.should_auto_enter_check(
                                loc_hit=bool(loc_hit),
                                probe_hit=bool(hit),
                                expected=str(cur_probe.expected or ""),
                                keywords=keywords,
                                hit_streak=cursor.step_effect_hit_streak,
                                defer_expected_to_check=defer_chk,
                            ):
                                cursor.step_goal_met = True
                                cursor.enter_check()
                                cursor.correction_hint = ""
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
                return _leave(status="pass", summary=cursor.summary(), pack=_pack())
            return _leave_case(cursor)

        if not is_explore and cur is not None:
            prev_step_n = int(getattr(ctx, "_run_case_step_n", 0) or 0)
            if int(cur.n) != prev_step_n:
                setattr(ctx, "_run_case_step_n", int(cur.n))
                setattr(ctx, "login_flow_macro_done", set())

        phase_cfg = _phase_cfg()
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
                setattr(ctx, "_login_flow_step_n", int(cur.n))
                setattr(ctx, "login_flow_macro_done", set())
                setattr(ctx, "login_flow_interrupt", False)
                setattr(ctx, "login_flow_macro_active", False)
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
        if bool(getattr(ctx, "display_guard_active", False)):
            menu = [
                c for c in menu
                if str(c.get("id") or "") not in ("recover_screen_asleep_or_locked",)
            ]
        from mino_nexus.loop.step_contract import instruction_allows_login_flow

        _allows_login_step = instruction_allows_login_flow(
            str(cur.instruction or "") if cur else "",
            login_module_case=bool(login_module_case),
        )
        if _allows_login_step or getattr(ctx, "login_flow_interrupt", False):
            _macro_hide = {"request_sms_code", "accept_legal_consent"}
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
            sms_line = compile_sms_send_hint(
                accounts_brief=str(getattr(ctx, "accounts_brief", "") or ""),
                hierarchy_nodes=list(getattr(ctx, "nav_hierarchy_nodes", None) or []),
                has_request_sms_code=any(str(c.get("id") or "") == "request_sms_code" for c in menu),
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
                cursor.correction_hint = _lf_hint_msg

        if (
            not is_explore
            and isinstance(cursor, StepCursor)
            and cursor.phase == "do"
            and cur
        ):
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
            if _lf_ok and not getattr(cursor, "login_chain_broken", False):
                _pending_sms = bool(getattr(cursor, "login_post_sms_pending", False))
                from mino_nexus.loop.login_submit import (
                    _otp_code_already_entered,
                    run_login_otp_submit_chain,
                    run_post_sms_login_pipeline,
                )

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
                            return _leave(status="cancelled", summary="任务已取消")
                        fail_msg = _login_chain_guard(chain, cursor=cursor)
                        if fail_msg:
                            return _leave(status="fail", summary=fail_msg)
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
                        )
                        if _pending_sms or getattr(cursor, "login_post_sms_pending", False):
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
            macro = try_run_system_dialog_macro(
                proxy,
                ctx,
                turn_seq=seq,
                target_package=str(getattr(ctx, "target_package", "") or target_pkg),
            )
            if not macro:
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
                if overlay_login:
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
                    if _lf_ok and (_allows_login_step or login_module_case) and not cursor.sms_auto_attempted:
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
            if macro:
                login_block = str(macro.get("block_id") or "") == "fb.global.login"
                if not login_block or macro.get("ok"):
                    cursor.sms_auto_attempted = True
                cap_auto = str(macro.get("capability_id") or "login_flow_macro")
                st_auto = str(macro.get("status") or "fail")
                sum_auto = str(macro.get("summary") or "")
                if macro.get("ok"):
                    cursor.record_step_op(cap_auto)
                    if login_block:
                        from mino_nexus.loop.flow_block_runner import maybe_emit_login_flow_complete

                        maybe_emit_login_flow_complete(
                            ctx,
                            app_id=str(getattr(ctx, "app_id", "") or ""),
                            history_lines=hist_lines,
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
                continue

        in_prep = cursor.phase == "prep"
        in_check = cursor.phase == "check"
        check_programmatic_pass = False
        if in_check and cur and str(cur.expected or "").strip() and not cursor.step_checked:
            from mino_nexus.loop.check_plan import build_check_plan, format_check_plan_brief
            from mino_nexus.loop.check_verify import (
                plan_for_step,
                run_programmatic_checks,
                synthesize_verdict,
            )

            chk_plan = plan_for_step(str(cur.expected or ""), instruction=str(cur.instruction or ""))
            inspect_slots["check_plan_brief"] = format_check_plan_brief(chk_plan)
            probe_nodes_chk = _effect_nodes(nav, ctx)
            overlay_blk = bool(nav and nav.preflight_block())
            evidences = run_programmatic_checks(
                chk_plan,
                nodes=probe_nodes_chk,
                nav_localized=dict(getattr(nav, "localized", None) or {}) if nav else {},
                session_block=str(inspect_slots.get("session_block") or ""),
                overlay_blocked=overlay_blk,
            )
            verdict = synthesize_verdict(chk_plan, evidences)
            if writer:
                writer.append(
                    "check/verdict",
                    {
                        "status": verdict.status,
                        "confidence": verdict.confidence,
                        "summary": verdict.summary[:240],
                        "points": len(chk_plan.points),
                    },
                )
            if verdict.status == "pass" and verdict.confidence >= 0.72:
                cursor.mark_checked()
                check_programmatic_pass = True
        scripted_check = bool(
            in_check and cur and str(cur.expected or "").strip() and not cursor.step_checked
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

        _log_context_slots(
            writer,
            cursor=cursor,
            menu=menu,
            phase_tool_kinds=phase_tool_kinds,
            inspect_slots=inspect_slots,
            history=history,
        )
        if cancel_check and cancel_check():
            return _leave(status="cancelled", summary="任务已取消")
        if check_programmatic_pass:
            decision = AgentDecision(
                status="continue",
                thought=f"程序校验通过：{verdict.summary}",
                action=AgentAction(capability_id="signal_done", params={}),
            )
        elif scripted_check:
            exp = enrich_assert_expectation(cur.instruction, cur.expected)
            assert_params: dict[str, Any] = {"expectation": exp}
            ctx_bits = [auto_knowledge_body, inspect_slots.get("doc_context") or ""]
            from mino_nexus.loop.session_persist import assert_session_context

            sess_ctx = assert_session_context(
                ctx, instruction=str(cur.instruction or ""), expected=str(cur.expected or "")
            )
            if sess_ctx:
                ctx_bits.append(sess_ctx)
            merged_ctx = "\n\n".join(b for b in ctx_bits if b).strip()
            if merged_ctx:
                assert_params["knowledge_context"] = merged_ctx
            decision = AgentDecision(
                status="continue",
                thought=f"校验步骤 {cur.n}：{cur.expected}",
                action=AgentAction(
                    capability_id="assert_visual",
                    params=assert_params,
                ),
            )
        else:
            from mino_nexus.loop.llm_screenshot_gate import (
                WITHHOLD_HINT,
                apply_allow_foreign_flag,
                clear_transient_llm_image_gate,
                parse_allow_foreign_flag,
                should_withhold_llm_image,
            )

            cp_block = cursor.prompt_block()
            if should_withhold_llm_image(ctx):
                cp_block = f"{cp_block}\n{WITHHOLD_HINT}"

            def _decide(*, send_image: bool) -> AgentDecision:
                img_b64 = shot.image_base64 if send_image and shot.has_image() else ""
                return decide_next_action(
                    goal=cursor.decide_goal(),
                    checkpoints_block=cp_block,
                    run_context=ctx,
                    history_block=_history(history),
                    width=shot.width or 1080,
                    height=shot.height or 1920,
                    image_base64=img_b64,
                    image_mime=shot.image_mime or "image/png",
                    success_criteria=cursor.decide_success(),
                    provider_id=provider_id or None,
                    phase=cursor.phase,
                    tool_kinds=phase_tool_kinds or None,
                    session_block=(
                        ""
                        if cursor.phase == "prep"
                        else llm_session_block(ctx, inspect_slots.get("session_block") or "")
                    ),
                    hierarchy_text=inspect_slots.get("hierarchy_text") or "",
                    knowledge_hint=inspect_slots.get("knowledge_hint") or "",
                    knowledge_body=inspect_slots.get("knowledge_body") or "",
                    nav_assist=inspect_slots.get("nav_assist") or "",
                    doc_context=inspect_slots.get("doc_context") or "",
                )

            def _decide_gated() -> AgentDecision:
                send_first = not should_withhold_llm_image(ctx)
                dec = _decide(send_image=send_first)
                apply_allow_foreign_flag(ctx, dec)
                if not send_first and parse_allow_foreign_flag(dec) is True:
                    dec = _decide(send_image=True)
                    apply_allow_foreign_flag(ctx, dec)
                return dec

            decision = _decide_gated()
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
            clear_transient_llm_image_gate(ctx)
        thought = decision.thought or ""
        trace = _llm_trace(decision)
        emit(
            "think",
            thought=thought, step=seq, goal=cursor.decide_goal(),
            status=decision.status, thumb=thumb,
            confidence=decision.confidence,
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

        screen_fp = _screen_fp(shot, inspect_slots.get("hierarchy_text") or "")

        if decision.status in ("give_up", "ask_human", "skip"):
            if (
                decision.status == "give_up"
                and not is_explore
                and isinstance(cursor, StepCursor)
            ):
                cur_give = cursor.current()
                if (
                    cursor.phase == "do"
                    and cur_give
                    and str(cur_give.expected or "").strip()
                    and cursor.step_ops > 0
                    and cursor.step_start_fp
                    and screen_fp != cursor.step_start_fp
                    and (
                        bool(cursor.step_goal_met)
                        or str(getattr(cursor, "do_subphase", "") or "") == "achievement"
                    )
                ):
                    cursor.enter_check()
                    cursor.correction_hint = (
                        "【纠偏】你在本步已执行过操作且界面已发生变化。"
                        "先按本步预期校验一次（assert_visual），再决定是否放弃。"
                    )
                    cursor.reset_step_effect_streak()
                    if writer:
                        writer.append(
                            "decision/give_up_rejected",
                            {
                                "thought": thought,
                                "case_step": cur_give.n,
                                "phase": cursor.phase,
                            },
                        )
                    continue
            if decision.status == "skip":
                st = "skip"
                summary = thought or "当前渠道跳过本条用例"
                if writer:
                    writer.append("decision/skip", {"thought": thought, "phase": cursor.phase})
                    writer.append("turn/end", {"decision_cap": "signal_skip", "decision_status": st})
                return _leave(status=st, summary=summary)
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
            return _leave(status=st, summary=summary)

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
        if cap_id == "swipe_direction" and params:
            from mino_nexus.ai.coords import prepare_xy_params_for_execute

            prepare_xy_params_for_execute(
                params,
                int(getattr(shot, "width", 0) or 0),
                int(getattr(shot, "height", 0) or 0),
            )
        from mino_nexus.loop.thought_done import should_coerce_mutate_to_signal_done

        if should_coerce_mutate_to_signal_done(
            thought=thought,
            cap_id=cap_id,
            phase=str(cursor.phase or ""),
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
            from mino_nexus.ai.coords import lift_selector_target, prepare_xy_params_for_execute
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
                return _leave(status="pass", summary=cursor.summary(), pack=_pack())
            if in_prep:
                from mino_nexus.services.resource_preflight import prep_resource_gate_issues

                resource_issues = prep_resource_gate_issues(ctx, case)
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
                            )
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
                    rec(seq, capability_id="signal_done", status="skipped",
                            summary="校验尚未通过，继续本步", thought=thought, thumb=thumb)
                    emit("result", thought=thought, step=seq, capability_id="signal_done",
                         status="skipped", summary="请先 assert_visual", thumb=thumb)
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
                            )
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
                        from mino_nexus.loop.ops_guard_diag import enrich_guard_block_payload

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
            return _leave(status="fail", summary=summary)

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
                return _leave(status=st, summary=verdict.reason)
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
                # steer **不计** no_progress_streak（§8.2）—— 这里刻意不碰 cursor.progress_gate
                _log_turn_end(writer, cap=nav_cap, status="skipped", guard_id=verdict.guard_id)
                continue

        from mino_nexus.loop.registry import run_guards_verdict

        guard_verdict = run_guards_verdict(list(phase_cfg.get("guards") or []), guard_ctx)
        skip_reason = guard_verdict.reason if not guard_verdict.allowed else None
        if skip_reason:
            skip_cap = guard_verdict.code or cap_id
            if writer:
                from mino_nexus.loop.ops_guard_diag import enrich_guard_block_payload, note_guard_block

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
            rec(seq, capability_id=skip_cap, status="skipped", summary=skip_reason, thought=thought, thumb=thumb)
            emit(
                "result",
                thought=thought,
                step=seq,
                capability_id=skip_cap,
                status="skipped",
                summary=skip_reason,
                thumb=thumb,
            )
            stop_msg = None
            if skip_cap == "action_fuse":
                stop_msg = cursor.progress_gate.record_fuse_block(skip_reason)
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
            elif skip_cap == "block_login_flow_unless_step_scope" and cursor.phase == "do":
                cursor.correction_hint = (
                    f"{skip_reason} "
                    "请按本步 instruction 点帖子/业务控件；勿点登录按钮，"
                    "tap 的 selector 勿填登录相关文案。"
                )
                cursor.login_scope_block_streak = (
                    int(getattr(cursor, "login_scope_block_streak", 0) or 0) + 1
                )
                if cursor.login_scope_block_streak >= 6:
                    stop_msg = (
                        f"连续 {cursor.login_scope_block_streak} 次登录 scope 拦截后仍重复 tap，"
                        "判定陷入死循环。请 signal_give_up 或改点帖子/内容区。"
                        f" 最近：{skip_reason[:120]}"
                    )
            elif (
                cursor.phase == "prep"
                and isinstance(cursor, StepCursor)
                and skip_cap in (
                    "block_prep_guest_mine_tab",
                    "skip_repeat_clear_app_cache",
                    "block_prep_login_when_logged_in_required",
                )
            ):
                cursor.prep_guard_streak = int(getattr(cursor, "prep_guard_streak", 0) or 0) + 1
                cursor.correction_hint = skip_reason
                if cursor.prep_guard_streak >= 6:
                    stop_msg = (
                        f"前置空转 {cursor.prep_guard_streak} 轮（仍为 logged_in 或无法 logout）。"
                        "请确认清缓存/路线图退出是否可用，或手动将设备登出后再跑。"
                        f" 最近拦截：{skip_reason[:160]}"
                    )
            if writer:
                writer.append(
                    "turn/end",
                    {"decision_cap": skip_cap, "decision_status": "skipped", "guard": True},
                )
                if stop_msg:
                    writer.append(
                        "progress/stop",
                        {
                            "reason": stop_msg,
                            "fuse_block_streak": cursor.progress_gate.fuse_block_streak,
                        },
                    )
            if stop_msg:
                return _leave(status="fail", summary=stop_msg)
            continue

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

        raw_resp = getattr(result, "raw_response", None) if not hasattr(result, "recovered") else {}
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
                        cursor.refresh_do_subphase()

        if writer:
            payload = {
                "capability_id": cap_id,
                "status": status_val,
                "summary": summary,
                "error": error,
                "executor_used": executor_used,
                "elapsed_ms": elapsed_ms,
            }
            if isinstance(raw_resp, dict) and raw_resp.get("nav_attempt"):
                payload["nav_attempt"] = raw_resp.get("nav_attempt")
            if recovery_extra:
                payload.update(recovery_extra)
            writer.append("tool/result", payload)
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
        rec(
            seq,
            capability_id=event.capability_id, status=status_val,
            summary=summary, error=error,
            executor_used=executor_used,
            elapsed_ms=elapsed_ms, thought=thought, thumb=thumb, extra=extra or None,
        )
        emit(
            "result",
            thought=thought, step=seq, action={"capability_id": cap_id, "params": params},
            capability_id=cap_id, status=status_val, result_status=status_val,
            summary=summary, elapsed_ms=elapsed_ms, thumb=thumb,
            executor_used=executor_used,
        )

        if hasattr(result, "status") and result.status in (EventStatus.BLOCKED,):
            if writer:
                writer.append(
                    "hitl/blocked",
                    {
                        "capability_id": cap_id,
                        "summary": error or summary or "被阻塞",
                        "executor_used": executor_used,
                    },
                )
            return _leave(status="blocked", summary=error or summary or "被阻塞")
        if hasattr(result, "status") and result.status in (EventStatus.FAIL, EventStatus.DECLINED):
            SLog.w(TAG, f"[{scout_run_id[:8]}] step {seq} {event.capability_id} {status_val}: {error or summary}")
            if in_check and cap_id == "assert_visual":
                fail_summary = f"步骤 {cur.n} 预期未成立：{cur.expected}。{summary}".strip()
                return _leave(status="fail", summary=fail_summary)

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

        if status_val == "pass" and cap_id in PROGRESS_CAPS and not str(cap_id).startswith(RECOVER_PREFIX):
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
        if status_val == "pass" and cap_id == "clear_app_cache":
            setattr(ctx, "prep_clear_done", True)
            if in_prep:
                setattr(ctx, "app_launch_confirmed", False)
            from mino_nexus.services.resource_transition import emit_clear_app_cache

            emit_clear_app_cache(ctx)
            ctx.session_dirty = False
            ctx.session_fact = {
                "session": "logged_out",
                "identity": "",
                "seen": "clear_app_cache",
                "source": "clear_app_cache",
                "reason": "清缓存后按未登录继续前置",
            }
            from mino_nexus.loop.session_persist import effective_session_block

            inspect_slots["session_block"] = effective_session_block(ctx, "")
        elif status_val == "pass":
            from mino_nexus.services.resource_transition_engine import maybe_fire_cap_transition

            maybe_fire_cap_transition(ctx, cap_id, status_val)
        if status_val == "pass" and cap_id in ("launch_app", "open_app", "open_url"):
            setattr(ctx, "app_launch_confirmed", True)
            from mino_nexus.loop.launch_grace import stamp_launch_grace

            stamp_launch_grace(ctx)
        if status_val == "pass" and cap_id == "press_key":
            key = str((params or {}).get("key") or "").strip().upper()
            if key in ("BACK", "BACK_KEY"):
                setattr(ctx, "recovery_allow_back", False)

        if status_val == "pass" and cap_id == "tap_element":
            from mino_nexus.loop.nav_onboarding_open_loop import clear_open_loop_if_tabs_visible

            clear_open_loop_if_tabs_visible(ctx)
            if isinstance(cursor, StepCursor):
                cursor.login_scope_block_streak = 0
        post_fp = ""
        if status_val == "pass" and (fuseable_cap(cap_id) or cap_id in ("fsm_navigate", "recover_fsm_navigate")):
            post_shot = proxy.observe("screenshot", force_fresh=True)
            post_fp = _screen_fp(post_shot, inspect_slots.get("hierarchy_text") or "")
            _lf_record = False
            if isinstance(cursor, StepCursor) and cur and cursor.phase == "do":
                from mino_nexus.loop.step_contract import instruction_allows_login_flow

                _lf_record = instruction_allows_login_flow(
                    str(cur.instruction or ""),
                    login_module_case=bool(login_module_case),
                )
            cursor.progress_gate.record_pass(
                cap_id=cap_id if fuseable_cap(cap_id) else "tap_element",
                params=params if fuseable_cap(cap_id) else {},
                pre_fp=screen_fp,
                post_fp=post_fp,
                intents_done=set(getattr(cursor, "step_intents_done", None) or set()),
                login_flow_step=_lf_record,
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
            from mino_nexus.loop.login_submit import (
                _otp_code_already_entered,
                run_post_sms_login_pipeline,
            )
            from mino_nexus.loop.step_contract import instruction_allows_login_flow
            from mino_nexus.loop.step_flow_scope import login_flow_allowed

            cursor.login_post_sms_pending = True
            cursor.login_post_sms_stall = 0
            setattr(ctx, "login_auto_submit_attempts", 0)

            _lf_post, _ = login_flow_allowed(
                cursor=cursor,
                phase="do",
                instruction=str(cur.instruction or ""),
                expected=str(cur.expected or ""),
            )
            if _lf_post and not getattr(cursor, "login_chain_broken", False):
                chain_post = run_post_sms_login_pipeline(
                    proxy,
                    ctx,
                    turn_seq=seq,
                    instruction=str(cur.instruction or ""),
                    intents_done=cursor.step_intents_done,
                    history_lines=list(history[-24:]),
                    hierarchy_nodes=list(getattr(ctx, "nav_hierarchy_nodes", None) or []),
                    login_module_case=bool(login_module_case),
                    retries=2,
                )
                if chain_post:
                    from mino_nexus.loop.login_submit import _otp_code_already_entered

                    if len(chain_post) == 1 and str(chain_post[0].get("capability_id") or "") == "input_text":
                        if chain_post[0].get("ok") and _otp_code_already_entered(
                            list(history[-24:]), cursor.step_intents_done
                        ):
                            chain_post = []
                    if chain_post:
                        if cancel_check and cancel_check():
                            return _leave(status="cancelled", summary="任务已取消")
                        fail_msg = _login_chain_guard(chain_post, cursor=cursor)
                        if fail_msg:
                            return _leave(status="fail", summary=fail_msg)
                        lc, ls = _consume_login_chain(
                            chain_post,
                            seq=seq,
                            cursor=cursor,
                            rec=rec,
                            emit=emit,
                            thumb=thumb,
                            screen_fp=str(post_fp or screen_fp or ""),
                            login_flow_step=bool(
                                instruction_allows_login_flow(
                                    str(cur.instruction or ""),
                                    login_module_case=bool(login_module_case),
                                )
                            ),
                        )
                        if _otp_code_already_entered(
                            list(history[-24:]), cursor.step_intents_done
                        ):
                            cursor.login_post_sms_pending = False
                            cursor.login_post_sms_stall = 0
                        _log_turn_end(writer, cap=lc or "login_chain", status=ls)
                        continue

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
            if re.search(r"发送验证码|重新发送|获取验证码", _tap_sel):
                from mino_nexus.loop.login_submit import run_post_sms_login_pipeline
                from mino_nexus.loop.step_contract import instruction_allows_login_flow
                from mino_nexus.loop.step_flow_scope import login_flow_allowed

                cursor.step_intents_done.add("sms_send")
                cursor.login_post_sms_pending = True
                cursor.login_post_sms_stall = 0
                setattr(ctx, "login_auto_submit_attempts", 0)
                _lf_tap, _ = login_flow_allowed(
                    cursor=cursor,
                    phase="do",
                    instruction=str(cur.instruction or ""),
                    expected=str(cur.expected or ""),
                )
                if _lf_tap and not getattr(cursor, "login_chain_broken", False):
                    chain_tap = run_post_sms_login_pipeline(
                        proxy,
                        ctx,
                        turn_seq=seq,
                        instruction=str(cur.instruction or ""),
                        intents_done=cursor.step_intents_done,
                        history_lines=list(history[-24:]),
                        hierarchy_nodes=list(getattr(ctx, "nav_hierarchy_nodes", None) or []),
                        login_module_case=bool(login_module_case),
                        retries=2,
                    )
                    if chain_tap:
                        lc, ls = _consume_login_chain(
                            chain_tap,
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
                        )
                        from mino_nexus.loop.login_submit import _otp_code_already_entered

                        if _otp_code_already_entered(list(history[-24:]), cursor.step_intents_done):
                            cursor.login_post_sms_pending = False
                        _log_turn_end(writer, cap=lc or "login_chain", status=ls)
                        continue

        if cap_id == "tap_element" and getattr(result, "status", None) == EventStatus.PASS:
            if not post_fp:
                post_shot = proxy.observe("screenshot", force_fresh=True)
                post_fp = _screen_fp(post_shot, inspect_slots.get("hierarchy_text") or "")
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
                return _leave(status="pass", summary=cursor.summary(), pack=_pack())
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
        return _leave(status="pass", summary=cursor.summary(), pack=_pack())
    if not is_explore and wall_budget_sec > 0:
        summary = f"超过 {wall_budget_sec} 秒仍未完成"
    else:
        summary = f"超过 {max_steps} 步仍未完成"
    return _leave(status="fail", summary=summary)


def _finish_case(emit, *, cursor: StepCursor, steps: list, t0: float, pack=None) -> dict[str, Any]:
    if cursor.all_uncheckable() or not cursor.saw_assert:
        summary = "未写预期，无法校验"
        return _finish(emit, status="unverifiable", summary=summary, steps=steps, t0=t0, pack=pack)
    last = next((s for s in reversed(steps) if s.get("capability_id") == "assert_visual"), None)
    summary = (last or {}).get("summary") or "全部步骤已校验"
    return _finish(emit, status="pass", summary=summary, steps=steps, t0=t0, pack=pack)
