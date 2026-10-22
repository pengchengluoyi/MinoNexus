"""一次派发返回 fail / declined 之后的全部处置。

顺序是硬的：焦点恢复 → 契约拒绝 → 里程碑失败记账 → 验证码误填计数。
前两步决定"要不要再试一次"，后两步只记账。任何一步返回 `FailFlow`，
由主循环决定 `return` 还是 `continue`。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

from mino_nexus.core.log import SLog
from mino_nexus.core.schemas import EventStatus
from mino_nexus.loop.execution.turn_log import _log_turn_end
from mino_nexus.loop.input_contract import contract_reject_summary, focus_reject_summary
from mino_nexus.loop.milestones import note_tool_fail_milestone
from mino_nexus.loop.step_phase_fsm import (
    first_active_milestone_id,
    mark_milestone_program_fail,
    milestone_phase_active,
)
from mino_nexus.loop.step_pointer import StepCursor
from mino_nexus.loop.vision_flags import execution_phase_active
from mino_nexus.loop.visual_review import (
    input_focus_menu_ids,
    recover_input_focus,
)

TAG = "AgentLoop"


@dataclass
class FailFlow:
    cont: bool = False
    leave: dict[str, Any] | None = None


def handle_fail_branch(
    *,
    ctx: Any,
    cursor: Any,
    cur: Any,
    cap_id: str,
    params: Any,
    summary: Any,
    error: Any,
    status_val: str,
    result: Any,
    event: Any,
    in_check: bool,
    writer: Any,
    seq: int,
    scout_run_id: str,
    leave: Callable[..., dict[str, Any]],
) -> FailFlow:
    if hasattr(result, "status") and result.status in (EventStatus.FAIL, EventStatus.DECLINED):
        from mino_nexus.loop.input_contract import contract_reject_summary, focus_reject_summary

        if cap_id == "tap_element":

            input_menu = input_focus_menu_ids(cursor, ctx)
            if input_menu and "tap_element" in input_menu and "input_text" not in input_menu:
                recovery = recover_input_focus(
                    cursor, ctx, reason="refocus_click_failed", execution_started=True, writer=writer,
                )
                if recovery == "stop":
                    return FailFlow(leave=leave(status="blocked", summary=str(cursor.semantic_stop)[:500], reason_code="execution_contract_blocked"))
        focus_error = focus_reject_summary(str(summary or ""), str(error or ""))
        if cap_id == "input_text" and focus_error and str((params or {}).get("target_mode") or "") == "current_focus":

            recovery = recover_input_focus(cursor, ctx, reason=focus_error, writer=writer)
            if recovery == "retry":
                _log_turn_end(writer, cap=cap_id, status="pending")
                return FailFlow(cont=True)
            msg = str(getattr(cursor, "semantic_stop", "") or "") if recovery == "stop" else f"当前焦点无效，无法安全输入：{focus_error}"
            if writer:
                writer.append("input/blocked", {
                    "code": "focus_target_unavailable", "failure_stage": "focus_validation",
                    "retryable": False, "execution_started": False, "reason": focus_error,
                })
            return FailFlow(leave=leave(status="blocked", summary=msg[:500], reason_code="execution_contract_blocked"))

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
            return FailFlow(leave=leave(status="blocked", summary=msg[:500], reason_code="execution_contract_blocked"))
        SLog.w(TAG, f"[{scout_run_id[:8]}] step {seq} {event.capability_id} {status_val}: {error or summary}")
        if in_check and cap_id == "assert_visual":
            fail_summary = f"步骤 {cur.n} 预期未成立：{cur.expected}。{summary}".strip()
            return FailFlow(leave=leave(status="fail", summary=fail_summary, reason_code="check_evidence_incomplete"))
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
                            return FailFlow(leave=leave(
                                status="fail",
                                summary=(
                                    f"验证码填入连续失败 {streak} 次："
                                    "input_text 落在邮箱框。请检查 Web 坐标或 signal_ask_human。"
                                ),
                             reason_code="input_target_mismatch"))
    return FailFlow()
