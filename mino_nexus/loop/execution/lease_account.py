"""`lease_account` 成功之后：标记会话已脏、刷新会话块。

填写登录账号时的租号只取凭证，不做资产转移——资产转移只属于前置里的租号步。
"""
from __future__ import annotations

from typing import Any

from mino_nexus.casework.resource_transition_engine import maybe_fire_cap_transition
from mino_nexus.loop.milestones import read_state as _read_ms
from mino_nexus.loop.observe.session_persist import effective_session_block, mark_session_dirty
from mino_nexus.loop.step_pointer import StepCursor


def note_lease_account(
    *,
    ctx: Any,
    cursor: Any,
    cap_id: str,
    status_val: str,
    in_prep: bool,
    inspect_slots: dict[str, str],
) -> None:
    if (
        status_val == "pass"
        and cap_id == "lease_account"
        and isinstance(cursor, StepCursor)
        and cursor.phase == "prep"
    ):
        ctx.prep_lease_account_done = True
        from mino_nexus.loop.milestones import read_state as _read_ms

        _ms_now = (_read_ms(cursor).get("milestones") or [])
        _filling = any(
            isinstance(row, dict)
            and str(row.get("id") or "") == "account_fill"
            and str(row.get("status") or "pending") not in ("pass", "skipped", "failed")
            for row in _ms_now
        )
        # 填写登录账号时的租号只取凭证。资产转移只属于前置里的租号步。
        if not _filling:
            if in_prep:
                ctx.app_launch_confirmed = False
            from mino_nexus.loop.observe.session_persist import mark_session_dirty

            # Selecting credentials did not clear cache or log out the UI.
            mark_session_dirty(ctx, reason="account_lease_changed")
        from mino_nexus.loop.observe.session_persist import effective_session_block

        inspect_slots["session_block"] = effective_session_block(ctx, "")
    elif status_val == "pass":
        from mino_nexus.casework.resource_transition_engine import maybe_fire_cap_transition

        maybe_fire_cap_transition(ctx, cap_id, status_val)
