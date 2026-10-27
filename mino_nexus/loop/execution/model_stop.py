"""模型主动收手：give_up / ask_human / skip 三种结束语的处理。

必须区分三种"停"的语义：
- `give_up` 在 do 阶段且子里程碑已齐时**不算放弃**，自动转进校验；
- `ask_human` 是人机协同，不算失败；
- 其余按观察缺失处理。

出口只有一个 `leave`：无论哪种，最终都结束整个运行，不会回到循环。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from mino_nexus.loop.execution.turn_log import _log_turn_end
from mino_nexus.loop.step_pointer import StepCursor


@dataclass
class StopFlow:
    leave: dict[str, Any] | None = None


def run_model_stop(
    *,
    ctx: Any,
    cursor: Any,
    cur: Any,
    decision: Any,
    trace: dict[str, Any],
    seq: int,
    thought: str,
    thumb: Any,
    writer: Any,
    rec: Callable[..., Any],
    emit: Callable[..., None],
    leave: Callable[..., dict[str, Any]],
) -> StopFlow:
    if (
        decision.status == "give_up"
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
            return StopFlow(cont=True)
    if decision.status == "skip":
        st = "skip"
        summary = thought or "当前渠道跳过本条用例"
        if writer:
            writer.append("decision/skip", {"thought": thought, "phase": cursor.phase})
            writer.append("turn/end", {"decision_cap": "signal_skip", "decision_status": st})
        return StopFlow(leave=leave(status=st, summary=summary, reason_code="model_gave_up"))
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
    # 观察缺失只是「这一回合没东西可看」，不是业务结论；如果本会话里已经有
    # 程序的真实失败原因（锚点未命中、能力版本变更等），退出原因必须用它。
    program_reason = str(getattr(cursor, "last_program_fail_reason", "") or "").strip()[:120]
    reason = (str(getattr(decision, "error_kind", "") or program_reason or "observation_missing")
              if "visual_observation_unavailable" in decision.parse_warnings
              else "model_human_required" if decision.status == "ask_human" else "model_gave_up")
    return StopFlow(leave=leave(status=st, summary=summary, reason_code=reason))
    return StopFlow()
