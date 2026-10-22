"""case_start / phase_enter 检查的触发条件。

`ran_case_start_inspection` 是跨回合状态：只在第一次进循环时跑 case_start，
之后每次换阶段才跑 phase_enter。登录模块用例整段跳过——它的会话事实由登录链自己写。
"""
from __future__ import annotations

from typing import Any


def run_phase_inspections(
    *,
    ctx: Any,
    cursor: Any,
    inspections: Any,
    shot: Any,
    provider_id: str,
    case: dict[str, Any],
    inspect_slots: dict[str, str],
    login_module_case: bool,
    ran_case_start_inspection: bool,
    last_phase_seen: Any,
    run_inspections: Any,
) -> bool:
    if login_module_case and cursor.phase in ("prep", "do"):
        inspect_slots["session_block"] = ""
        return True

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
    return True
