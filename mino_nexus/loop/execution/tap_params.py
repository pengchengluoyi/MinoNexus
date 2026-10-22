"""派发前的参数装配与契约自检（六个"带坐标/文本"的能力）。

顺序不能换：装配 → 凭证绑定检查 → 焦点引用检查 → 坐标缺失计数 → 清计数。
每一项失败都必须在**任何设备动作之前**返回，否则会把缺参数的调用发出去，
Scout 那边只能自己猜默认值，出错后很难回溯到底是哪一步丢的。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from mino_nexus.action_space import assemble_execute_params
from mino_nexus.action_space.assemble import visual_point_missing
from mino_nexus.action_space.scheme import execution_scheme as run_scheme
from mino_nexus.loop.execution.turn_log import _log_turn_end
from mino_nexus.loop.visual_review import recover_input_focus


@dataclass
class TapParamsFlow:
    cont: bool = False
    leave: dict[str, Any] | None = None
    params: Any = None


def run_tap_params(
    *,
    ctx: Any,
    cursor: Any,
    cap_id: str,
    params: Any,
    shot: Any,
    seq: int,
    writer: Any,
    emit: Callable[..., None],
    leave: Callable[..., dict[str, Any]],
) -> TapParamsFlow:
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
        return TapParamsFlow(leave=leave(status="blocked", summary=summary, reason_code="execution_contract_blocked"))
    if cap_id == "input_text" and params.pop("_focus_unresolved", None):
        from mino_nexus.loop.visual_review import recover_input_focus

        recovery = recover_input_focus(cursor, ctx, reason="focus_reference_missing", writer=writer)
        if recovery == "retry":
            _log_turn_end(writer, cap="", status="pending")
            return TapParamsFlow(cont=True)
        if recovery == "stop":
            return TapParamsFlow(leave=leave(status="blocked", summary=str(cursor.semantic_stop)[:500], reason_code="execution_contract_blocked"))
        summary = "当前焦点输入缺少有效的聚焦动作引用，不能静默写入"
        if writer:
            writer.append(
                "contract/blocked",
                {"code": "parameter_contract_mismatch", "reason": summary, "retryable": False},
            )
        return TapParamsFlow(leave=leave(status="blocked", summary=summary, reason_code="execution_contract_blocked"))
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
            return TapParamsFlow(leave=leave(status="fail", summary=stop_msg[:500], reason_code="target_resolution_failed"))
        emit("think", thought=hint, step=seq, status="continue")
        return TapParamsFlow(cont=True)
    if gate is not None:
        gate.clear_missing_coordinate()
    return TapParamsFlow(params=params)
