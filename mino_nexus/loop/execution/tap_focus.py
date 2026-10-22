"""点输入框之后，把"这次到底聚焦了哪个输入位"绑到 ctx 上。

Scout 用 `EXECUTE.step_idx` 作为 focus 编号并精确比对，而 `step_idx` 是 RouterProxy
按 run 分配的线上序号——只有回执里的才是真值。所以编号只从回执里取，取不到就不猜，
让输入菜单去要求重新聚焦。这条规则藏在这 40 行里，搬出来才看得见。
"""
from __future__ import annotations

from typing import Any

from mino_nexus.action_space.scheme import execution_scheme
from mino_nexus.loop.milestone_orchestrator import in_progress_milestone_id
from mino_nexus.loop.step_pointer import StepCursor
from mino_nexus.loop.visual_review import input_focus_field, input_focus_milestone_id


def bind_tap_focus(
    *,
    status_val: str,
    cap_id: str,
    ctx: Any,
    cursor: Any,
    params: dict[str, Any],
    result: Any,
    writer: Any,
    scout_run_id: str,
) -> None:
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
        ctx.focus_action_ref = f"focus:{scout_run_id}:{wire_idx}" if input_click and wire_idx is not None else ""
        ctx.focus_action_field = tap_field if input_click else ""
        if writer and input_click and wire_idx is not None:
            writer.append("input/focus_bound", {
                "milestone_id": focus_target, "field": tap_field,
                "source": "current_input_pair", "wire_step_idx": wire_idx,
                "verification_status": "pending",
            })
        from mino_nexus.action_space.scheme import execution_scheme
        ctx.focus_action_scheme = execution_scheme(ctx) if input_click and wire_idx is not None else ""
