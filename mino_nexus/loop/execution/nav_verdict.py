"""导航守卫的否决：nav.check() 说"这一步不该做"时怎么办。

三种处理：可改道的改成 `nav_cap` 继续、可停的按进度闸门停、其余记为跳过并结束本轮。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from mino_nexus.loop.execution.turn_log import _log_turn_end
from mino_nexus.loop.step_pointer import StepCursor


@dataclass
class NavVerdictFlow:
    cont: bool = False
    leave: dict[str, Any] | None = None


def run_nav_verdict(
    *,
    ctx: Any,
    cursor: Any,
    cur: Any,
    nav: Any,
    cap_id: str,
    params: dict[str, Any],
    thought: str,
    thumb: Any,
    writer: Any,
    seq: int,
    run_type: str,
    rec: Callable[..., Any],
    emit: Callable[..., None],
    leave: Callable[..., dict[str, Any]],
) -> NavVerdictFlow:
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
        return NavVerdictFlow(leave=leave(status=st, summary=verdict.reason, reason_code="navigation_guard_blocked"))
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
                return NavVerdictFlow(leave=leave(status="blocked", summary=nav_stop, reason_code="navigation_guard_blocked"))
        _log_turn_end(writer, cap=nav_cap, status="skipped", guard_id=verdict.guard_id)
        return NavVerdictFlow(cont=True)
    return NavVerdictFlow()
