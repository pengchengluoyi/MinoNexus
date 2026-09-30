"""一个回合只推进一次焦点。重复 cap 改为把当前里程碑标 pass，不空转守卫。"""
from __future__ import annotations

from typing import Any

# 这些守卫只说明「已经做过」，不改里程碑，模型会原样再派。
SATISFY_GUARD_IDS = (
    "skip_repeat_launch_app",
    "skip_repeat_clear_app_cache",
    "skip_repeat_check_run_env",
    "skip_repeat_get_app_version",
    "skip_repeat_read_device",
    "skip_repeat_structural_cap",
    "skip_repeat_swipe_stuck",
    "skip_repeat_tap",
    "skip_repeat_fsm_open_loop",
    "skip_repeat_fsm_declined",
    "skip_recover_bring_when_foreground",
    "skip_recover_screen_when_display_guard",
    "skip_repeat_satisfied_step_action",
)


def _focus_cap(row: dict[str, Any]) -> str:
    return str(row.get("hook_cap") or row.get("device_cap") or row.get("cap") or "").strip()


def try_satisfy_focus_instead_of_repeat(
    cursor: Any,
    cap_id: str,
    guard_ctx: dict[str, Any],
    writer: Any = None,
) -> bool:
    """当前焦点的 cap 已经满足时标 pass，抬起下一条 pending，本回合不再下发。"""
    from mino_nexus.loop.milestone_orchestrator import (
        complete_in_progress_on_tool,
        in_progress_milestone,
    )
    from mino_nexus.loop.milestones import read_state
    from mino_nexus.loop.registry import GUARDS

    cap = str(cap_id or "").strip()
    if not cap:
        return False
    focus = in_progress_milestone(read_state(cursor))
    if not isinstance(focus, dict):
        return False
    if _focus_cap(focus) != cap:
        return False
    hit_id = ""
    hit_reason = ""
    for gid in SATISFY_GUARD_IDS:
        fn = GUARDS.get(gid)
        if fn is None:
            continue
        reason = fn(guard_ctx)
        if reason:
            hit_id = gid
            hit_reason = str(reason)
            break
    if not hit_id:
        return False
    summary = f"已满足，不再重复执行：{hit_reason}"[:400]
    complete_in_progress_on_tool(
        cursor,
        capability_id=cap,
        ok=True,
        summary=summary,
        writer=writer,
    )
    if writer is not None:
        try:
            writer.append(
                "milestone/already_satisfied",
                {"capability_id": cap, "guard_id": hit_id, "summary": summary},
            )
        except Exception:  # noqa: BLE001
            pass
    return True
