"""单用例看图闭环。prep / do / check 都由 Agent 看图决策。"""
from __future__ import annotations

import time
from typing import Any, Optional

from mino_nexus.ai.planner import decide_next_action
from mino_nexus.catalog.exec_classes import MUTATE_CAPS
from mino_nexus.core.log import SLog
from mino_nexus.loop.agent_stream import emit_agent_event, make_thumb
from mino_nexus.loop.local_executors import dispatch_local, is_local_cap
from mino_nexus.loop.recovery import apply_rule, RuleMatch
from mino_nexus.loop.router_proxy import RouterProxy
from mino_nexus.loop.skill_trace import envelope as skill_envelope
from mino_nexus.loop.step_pointer import (
    StepCursor,
    build_seq_nodes,
    repeats_last_tap,
)
from mino_nexus.catalog import registry as catalog
from mino_nexus.core.protocol import EventStatus
from mino_nexus.services.run_store import line_text, report_run_id
from mino_nexus.runtime.run_context import build_run_context
from mino_nexus.core.schemas import EventResult, PlanEvent

TAG = "AgentLoop"
_MAX_STEPS = 24
RECOVER_PREFIX = "recover_"


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


def _elapsed(t0: float) -> int:
    return int((time.time() - t0) * 1000)


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
) -> dict[str, Any]:
    from mino_nexus.ai import dispatch_log as dispatch

    cid = str(case.get("case_id") or "")
    stream_id = str(case.get("report_run_id") or "").strip() or (
        report_run_id(run_id, cid) if cid else run_id
    )
    overview = _case_overview(case)
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
    proxy = RouterProxy(sn, run_id=run_id, target_package=package)
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
        trigger="case_run",
        source="case_run",
        role="test-engineer",
        skill="run-case",
        app_id=app_id,
        app_name=app_name,
        pipeline_id=stream_id,
    )
    try:
        return _run_loop(
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
            run_id=run_id,
        )
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
    }
    if extra:
        row.update(extra)
    steps.append(row)
    history.append(f"{seq}. {capability_id} → {status}: {summary or error}")
    return row


def _ensure_web_page(proxy: RouterProxy, ctx, *, run_id: str) -> Optional[dict[str, Any]]:
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
    result = proxy.dispatch(event, run_id=run_id, step_idx=0)
    status_val = result.status.value if hasattr(result.status, "value") else str(result.status)
    err = result.error or result.summary or ""
    fatal = status_val in ("fail", "failed", "declined", "blocked") and not any(
        k in err for k in ("能力目录里没有", "无可用实现", "应由 Nexus 本地")
    )
    return {
        "status": status_val,
        "summary": result.summary or (f"打开 {url}" if url else "打开空白页"),
        "error": result.error or "",
        "executor_used": result.executor_used or "playwright",
        "fatal": fatal,
    }


def _fill_assert_expectation(params: dict[str, Any], cur, *, in_check: bool) -> dict[str, Any]:
    """校验必须把用例同号预期塞给 VLM。模型常调 assert_visual 且 params 为空。"""
    out = dict(params or {})
    case_expected = str(getattr(cur, "expected", "") or "").strip()
    model_expected = str(out.get("expectation") or out.get("expected") or "").strip()
    if in_check and case_expected:
        out["expectation"] = case_expected
    elif not model_expected and case_expected:
        out["expectation"] = case_expected
    return out


def _run_action(*, event: PlanEvent, shot, proxy: RouterProxy, ctx, run_id: str, seq: int) -> Any:
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
        )
    if is_local_cap(cap):
        return dispatch_local(event, shot=shot)
    return proxy.dispatch(event, run_id=run_id, step_idx=seq)


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
    run_id: str,
) -> dict[str, Any]:
    from mino_nexus.services.skill_store import get_skill

    skill = get_skill("run-case") or {}
    sop = skill.get("sop") if isinstance(skill.get("sop"), dict) else {}
    tool_kinds = list(sop.get("tool_kinds") or [])
    try:
        max_steps = max(1, min(80, int(sop.get("max_steps") or _MAX_STEPS)))
    except (TypeError, ValueError):
        max_steps = _MAX_STEPS
    system_prompt = str(skill.get("system_prompt") or "").strip()
    cursor = StepCursor(
        build_seq_nodes(case),
        precondition=str(case.get("precondition") or "").strip(),
    )

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

    emit("start", thought="开始看图执行", step=0, goal=overview)

    if not ctx.has_control_channel:
        summary = "没有可用设备通道。请确认 Scout 在线并已上报这台设备。"
        return _finish(emit, status="fail", summary=summary, steps=steps, t0=t0, pack=_pack)

    if not cursor.nodes:
        summary = "用例没有可执行的步骤。"
        return _finish(emit, status="fail", summary=summary, steps=steps, t0=t0, pack=_pack)

    opened = _ensure_web_page(proxy, ctx, run_id=run_id)
    if opened:
        rec(
            0,
            capability_id="launch_app",
            status=str(opened.get("status") or "pass"),
            summary=str(opened.get("summary") or "打开页面"),
            thought="Web 槽截图前先打开被测页",
            error=str(opened.get("error") or ""),
            executor_used=str(opened.get("executor_used") or "playwright"),
        )
        if opened.get("fatal"):
            summary = opened.get("summary") or "打开页面失败"
            emit("observe", thought=summary, step=0, status="fail")
            return _finish(emit, status="fail", summary=summary, steps=steps, t0=t0, pack=_pack)

    for seq in range(1, max_steps + 1):
        if cancel_check and cancel_check():
            return _finish(emit, status="cancelled", summary="任务已取消", steps=steps, t0=t0, pack=_pack)

        if cursor.done:
            return _finish_case(emit, cursor=cursor, steps=steps, t0=t0, pack=_pack)

        shot = proxy.observe("screenshot", force_fresh=True)
        thumb = make_thumb(shot.image_base64) if shot.has_image() else ""
        if not shot.has_image():
            summary = shot.error or "Scout 截图失败"
            if "没有打开的页面" in summary:
                summary = "浏览器还没有打开页面。请确认应用填了 Web 地址，或在前置里先打开网址。"
            emit("observe", thought=summary, step=seq, status="fail")
            return _finish(emit, status="fail", summary=summary, steps=steps, t0=t0, pack=_pack)

        cur = cursor.current()
        if cur is None:
            return _finish_case(emit, cursor=cursor, steps=steps, t0=t0, pack=_pack)

        in_prep = cursor.phase == "prep"
        in_check = cursor.phase == "check"
        decision = decide_next_action(
            goal=cursor.decide_goal(),
            checkpoints_block=cursor.prompt_block(),
            run_context=ctx,
            history_block=_history(history),
            width=shot.width or 1080,
            height=shot.height or 1920,
            image_base64=shot.image_base64,
            image_mime=shot.image_mime or "image/png",
            success_criteria=cursor.decide_success(),
            provider_id=provider_id or None,
            phase=cursor.phase,
            system_prompt=system_prompt,
            tool_kinds=tool_kinds or None,
        )
        thought = decision.thought or ""
        emit(
            "think",
            thought=thought, step=seq, goal=cursor.decide_goal(),
            status=decision.status, thumb=thumb,
            confidence=decision.confidence,
        )

        if decision.status in ("give_up", "ask_human"):
            st = "blocked" if decision.status == "ask_human" else "fail"
            summary = thought or ("需要人介入" if st == "blocked" else "模型放弃")
            return _finish(emit, status=st, summary=summary, steps=steps, t0=t0, pack=_pack)

        action = decision.action
        cap_id = str(action.capability_id) if action and action.capability_id else ""
        params = dict(action.params or {}) if action else {}
        if cap_id == "assert_visual":
            params = _fill_assert_expectation(params, cur, in_check=in_check)
        pending_mutate = bool(decision.status == "done" and cap_id in MUTATE_CAPS and action)

        if decision.status == "done" and not pending_mutate:
            if in_prep:
                summary = thought or "前置检查完成，进入操作步骤"
                rec(seq, capability_id="signal_done", status="skipped",
                        summary=summary, thought=thought)
                emit("result", thought=thought, step=seq, capability_id="signal_done",
                     status="skipped", summary=summary, thumb=thumb)
                cursor.finish_prep()
                continue
            if in_check:
                if not cursor.step_checked:
                    rec(seq, capability_id="signal_done", status="skipped",
                            summary="校验尚未通过，继续本步", thought=thought)
                    emit("result", thought=thought, step=seq, capability_id="signal_done",
                         status="skipped", summary="请先 assert_visual", thumb=thumb)
                    continue
                rec(seq, capability_id="signal_done", status="skipped",
                        summary=thought or f"步骤 {cur.n} 校验结束", thought=thought)
                emit("result", thought=thought, step=seq, capability_id="signal_done",
                     status="skipped", summary=f"步骤 {cur.n} 校验通过", thumb=thumb)
                if not cursor.advance():
                    return _finish_case(emit, cursor=cursor, steps=steps, t0=t0, pack=_pack)
                continue
            rec(seq, capability_id="signal_done", status="skipped",
                    summary=thought or f"步骤 {cur.n} 操作结束，进入校验", thought=thought)
            emit("result", thought=thought, step=seq, capability_id="signal_done",
                 status="skipped", summary=f"步骤 {cur.n} 操作结束，进入校验", thumb=thumb)
            cursor.enter_check()
            continue

        if not cap_id:
            summary = thought or "模型没有给出下一步动作"
            return _finish(emit, status="fail", summary=summary, steps=steps, t0=t0, pack=_pack)

        if in_check and cap_id in MUTATE_CAPS:
            summary = "校验阶段不能改界面，已拒绝这次操作"
            rec(seq, capability_id=cap_id, status="skipped",
                    summary=summary, thought=thought)
            emit("result", thought=thought, step=seq, capability_id=cap_id,
                 status="skipped", summary=summary, thumb=thumb)
            continue

        if (not in_prep) and (not in_check) and cap_id == "tap_element" and repeats_last_tap(cursor.last_tap, params):
            summary = "入口已点过，跳过这次点击，进入本步校验"
            rec(seq, capability_id="skip_repeat_tap", status="skipped",
                    summary=summary, thought=thought)
            emit("result", thought=thought, step=seq, capability_id="skip_repeat_tap",
                 status="skipped", summary=summary, thumb=thumb)
            cursor.enter_check()
            continue

        emit(
            "step",
            thought=thought, step=seq, status=decision.status, thumb=thumb,
            action={"capability_id": cap_id, "params": params},
            capability_id=cap_id,
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
        result = _run_action(event=event, shot=shot, proxy=proxy, ctx=ctx, run_id=run_id, seq=seq)
        if hasattr(result, "recovered"):
            status_val = "pass" if result.recovered else ("fail" if result.applied else "skipped")
            summary = result.summary() if callable(getattr(result, "summary", None)) else str(getattr(result, "error", "") or "")
            elapsed_ms = 0
            error = result.error or ""
            executor_used = "recovery"
        else:
            status_val = result.status.value if hasattr(result.status, "value") else str(result.status)
            summary = result.summary or ""
            elapsed_ms = result.elapsed_ms or 0
            error = result.error or ""
            executor_used = result.executor_used or ""

        extra = {}
        if cap_id == "assert_visual":
            extra = {"case_step_index": cur.n, "expectation": cur.expected}
        rec(
            seq,
            capability_id=event.capability_id, status=status_val,
            summary=summary, error=error,
            executor_used=executor_used,
            elapsed_ms=elapsed_ms, thought=thought, extra=extra or None,
        )
        emit(
            "result",
            thought=thought, step=seq, action={"capability_id": cap_id, "params": params},
            capability_id=cap_id, status=status_val, result_status=status_val,
            summary=summary, elapsed_ms=elapsed_ms, thumb=thumb,
            executor_used=executor_used,
        )

        if hasattr(result, "status") and result.status in (EventStatus.BLOCKED,):
            return _finish(emit, status="blocked", summary=error or summary or "被阻塞", steps=steps, t0=t0, pack=_pack)
        if hasattr(result, "status") and result.status in (EventStatus.FAIL, EventStatus.DECLINED):
            SLog.w(TAG, f"[{run_id[:8]}] step {seq} {event.capability_id} {status_val}: {error or summary}")
            if in_check and cap_id == "assert_visual":
                fail_summary = f"步骤 {cur.n} 预期未成立：{cur.expected}。{summary}".strip()
                return _finish(emit, status="fail", summary=fail_summary, steps=steps, t0=t0, pack=_pack)

        if cap_id == "tap_element" and getattr(result, "status", None) == EventStatus.PASS:
            cursor.remember_tap(params)

        if cap_id == "assert_visual" and status_val == "pass":
            cursor.mark_checked()
            if not cursor.advance():
                return _finish_case(emit, cursor=cursor, steps=steps, t0=t0, pack=_pack)
            continue

        if decision.status == "done":
            if in_prep:
                cursor.finish_prep()
            elif in_check:
                if cursor.step_checked and not cursor.advance():
                    return _finish_case(emit, cursor=cursor, steps=steps, t0=t0, pack=_pack)
            else:
                cursor.enter_check()
            continue

    summary = f"超过 {max_steps} 步仍未完成"
    return _finish(emit, status="fail", summary=summary, steps=steps, t0=t0, pack=_pack)


def _finish_case(emit, *, cursor: StepCursor, steps: list, t0: float, pack=None) -> dict[str, Any]:
    if cursor.all_uncheckable() or not cursor.saw_assert:
        summary = "未写预期，无法校验"
        return _finish(emit, status="unverifiable", summary=summary, steps=steps, t0=t0, pack=pack)
    last = next((s for s in reversed(steps) if s.get("capability_id") == "assert_visual"), None)
    summary = (last or {}).get("summary") or "全部步骤已校验"
    return _finish(emit, status="pass", summary=summary, steps=steps, t0=t0, pack=pack)
