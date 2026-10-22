"""`fsm_degraded` 回执：把降级提示与开环提示并到 correction_hint，并记录导航尝试。

降级提示必须叠加，不能覆盖——覆盖会把上一条更有价值的纠正信息冲掉。
"""
from __future__ import annotations

from typing import Any

from mino_nexus.loop.nav_onboarding_open_loop import (
    format_open_loop_hint,
    note_fsm_degrade_for_open_loop,
)
from mino_nexus.loop.step_pointer import StepCursor


def note_nav_degraded(
    *,
    ctx: Any,
    cursor: Any,
    cur: Any,
    params: Any,
    case: dict[str, Any],
    raw_resp: Any,
    writer: Any,
) -> None:
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
