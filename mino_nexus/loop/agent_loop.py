"""单用例看图闭环。循环在 Nexus，设备动作走 RouterProxy。"""
from __future__ import annotations

import time
from typing import Any

from mino_nexus.ai.planner import decide_next_action
from mino_nexus.log import SLog
from mino_nexus.loop.agent_stream import emit_agent_event, make_thumb
from mino_nexus.loop.local_executors import dispatch_local, is_local_cap
from mino_nexus.loop.router_proxy import RouterProxy
from mino_nexus.protocol import EventStatus
from mino_nexus.runtime.run_context import build_run_context
from mino_nexus.schemas import PlanEvent

TAG = "AgentLoop"
_MAX_STEPS = 24


def _goal_of(case: dict[str, Any]) -> str:
    name = str(case.get("name") or case.get("case_id") or "用例")
    steps = case.get("steps") if isinstance(case.get("steps"), list) else []
    expected = case.get("expected") if isinstance(case.get("expected"), list) else []
    body = "\n".join(str(x) for x in steps if str(x).strip()) or str(case.get("steps_raw") or "")
    want = "\n".join(str(x) for x in expected if str(x).strip()) or str(case.get("expected_raw") or "")
    bits = [name]
    if body:
        bits.append(f"步骤：{body}")
    if want:
        bits.append(f"预期：{want}")
    pre = str(case.get("precondition") or "").strip()
    if pre:
        bits.append(f"前置：{pre}")
    return "\n".join(bits)


def _history(rows: list[str]) -> str:
    return "\n".join(rows[-12:]) if rows else "（还没有动作）"


def run_case(
    *,
    run_id: str,
    case: dict[str, Any],
    sn: str,
    app_id: str,
    package: str = "",
    provider_id: str = "",
    playbook: dict | None = None,
    cancel_check=None,
) -> dict[str, Any]:
    """跑一条用例。返回 {status, summary, steps, elapsed_ms}。"""
    cid = str(case.get("case_id") or "")
    goal = _goal_of(case)
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
    proxy = RouterProxy(sn, run_id=run_id)
    history: list[str] = []
    steps: list[dict[str, Any]] = []

    emit_agent_event({
        "run_id": run_id, "case_id": cid, "goal": goal,
        "phase": "start", "thought": "开始看图执行", "step": 0,
    })

    if not ctx.has_control_channel:
        summary = "没有可用设备通道。请确认 Scout 在线并已上报这台设备。"
        emit_agent_event({"run_id": run_id, "case_id": cid, "phase": "done", "overall": summary, "status": "fail"})
        return {"status": "fail", "summary": summary, "steps": steps, "elapsed_ms": int((time.time() - t0) * 1000)}

    for seq in range(1, _MAX_STEPS + 1):
        if cancel_check and cancel_check():
            summary = "任务已取消"
            emit_agent_event({"run_id": run_id, "case_id": cid, "phase": "done", "overall": summary, "status": "cancelled"})
            return {"status": "cancelled", "summary": summary, "steps": steps, "elapsed_ms": int((time.time() - t0) * 1000)}

        shot = proxy.observe("screenshot", force_fresh=True)
        thumb = make_thumb(shot.image_base64) if shot.has_image() else ""
        if not shot.has_image():
            summary = shot.error or "Scout 截图失败"
            emit_agent_event({
                "run_id": run_id, "case_id": cid, "phase": "observe",
                "thought": summary, "step": seq, "status": "fail",
            })
            return {"status": "fail", "summary": summary, "steps": steps, "elapsed_ms": int((time.time() - t0) * 1000)}

        decision = decide_next_action(
            goal=goal,
            checkpoints_block="",
            run_context=ctx,
            history_block=_history(history),
            width=shot.width or 1080,
            height=shot.height or 1920,
            image_base64=shot.image_base64,
            image_mime=shot.image_mime or "image/png",
            success_criteria=str(case.get("expected_raw") or ""),
            provider_id=provider_id or None,
        )
        thought = decision.thought or ""
        emit_agent_event({
            "run_id": run_id, "case_id": cid, "goal": goal,
            "phase": "think", "thought": thought, "step": seq,
            "status": decision.status, "thumb": thumb,
            "confidence": decision.confidence,
        })

        if decision.status == "done":
            summary = thought or "模型判定用例已完成"
            emit_agent_event({"run_id": run_id, "case_id": cid, "phase": "done", "overall": summary, "status": "pass"})
            return {"status": "pass", "summary": summary, "steps": steps, "elapsed_ms": int((time.time() - t0) * 1000)}
        if decision.status in ("give_up", "ask_human"):
            st = "blocked" if decision.status == "ask_human" else "fail"
            summary = thought or ("需要人介入" if st == "blocked" else "模型放弃")
            emit_agent_event({"run_id": run_id, "case_id": cid, "phase": "done", "overall": summary, "status": st})
            return {"status": st, "summary": summary, "steps": steps, "elapsed_ms": int((time.time() - t0) * 1000)}

        action = decision.action
        if action is None or not action.capability_id:
            summary = thought or "模型没有给出下一步动作"
            emit_agent_event({"run_id": run_id, "case_id": cid, "phase": "done", "overall": summary, "status": "fail"})
            return {"status": "fail", "summary": summary, "steps": steps, "elapsed_ms": int((time.time() - t0) * 1000)}

        event = PlanEvent(
            seq=seq,
            capability_id=action.capability_id,
            event_kind=action.capability_id,
            params=dict(action.params or {}),
            ai_reasoning=thought,
            label=thought[:80],
        )
        if is_local_cap(event.capability_id):
            result = dispatch_local(event, shot=shot)
        else:
            result = proxy.dispatch(event, run_id=run_id, step_idx=seq)

        status_val = result.status.value if hasattr(result.status, "value") else str(result.status)
        row = {
            "seq": seq,
            "capability_id": event.capability_id,
            "status": status_val,
            "summary": result.summary,
            "error": result.error,
            "executor_used": result.executor_used,
            "elapsed_ms": result.elapsed_ms,
            "thought": thought,
        }
        steps.append(row)
        history.append(f"{seq}. {event.capability_id} → {status_val}: {result.summary or result.error}")
        emit_agent_event({
            "run_id": run_id, "case_id": cid, "phase": "act",
            "thought": thought, "step": seq, "action": event.capability_id,
            "status": status_val, "summary": result.summary, "thumb": thumb,
        })

        if result.status in (EventStatus.BLOCKED,):
            summary = result.error or result.summary or "被阻塞"
            emit_agent_event({"run_id": run_id, "case_id": cid, "phase": "done", "overall": summary, "status": "blocked"})
            return {"status": "blocked", "summary": summary, "steps": steps, "elapsed_ms": int((time.time() - t0) * 1000)}
        if result.status in (EventStatus.FAIL, EventStatus.DECLINED):
            SLog.w(TAG, f"[{run_id[:8]}] step {seq} {event.capability_id} {status_val}: {result.error or result.summary}")
            # 失败不立刻停：让模型看下一张图决定是重试还是放弃

    summary = f"超过 {_MAX_STEPS} 步仍未完成"
    emit_agent_event({"run_id": run_id, "case_id": cid, "phase": "done", "overall": summary, "status": "fail"})
    return {"status": "fail", "summary": summary, "steps": steps, "elapsed_ms": int((time.time() - t0) * 1000)}
