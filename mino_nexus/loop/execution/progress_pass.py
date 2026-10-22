"""成功后给进展门记一条 pass：截图指纹、登录流标记、里程碑焦点。

这三件事必须一起发生——指纹比较了却没记里程碑，`ProgressGate` 会判"进展未发生"，
后面就重复点同一屏。集中在这里，避免循环里再散落一份。
"""
from __future__ import annotations

from typing import Any

from mino_nexus.loop.execution.page_ready import _progress_gate_web_kwargs, _screen_fp
from mino_nexus.loop.fuse.action_fuse import fuseable_cap
from mino_nexus.loop.step_pointer import StepCursor


def record_progress_pass(
    *,
    ctx: Any,
    proxy: Any,
    cursor: Any,
    cur: Any,
    cap_id: str,
    params: Any,
    status_val: str,
    screen_fp: Any,
    visual_scheme: bool,
    login_module_case: bool,
    inspect_slots: dict[str, str],
) -> None:
    if status_val == "pass" and (fuseable_cap(cap_id) or cap_id in ("fsm_navigate", "recover_fsm_navigate")):
        if visual_scheme:
            # 纯视觉的进展键不看像素。操作后再截一张会占住截图线程，下一回合决策图报 scout timeout。
            post_fp = screen_fp
        else:
            post_shot = proxy.observe("screenshot", force_fresh=True)
            post_fp = _screen_fp(
                post_shot, inspect_slots.get("hierarchy_text") or "", ctx=ctx
            )
        _lf_record = False
        if isinstance(cursor, StepCursor) and cur and cursor.phase == "do":
            from mino_nexus.loop.step_contract import instruction_allows_login_flow

            _lf_record = instruction_allows_login_flow(
                str(cur.instruction or ""),
                login_module_case=bool(login_module_case),
            )
        _focus_mid = ""
        if isinstance(cursor, StepCursor):
            from mino_nexus.loop.milestone_orchestrator import in_progress_milestone
            from mino_nexus.loop.milestones import read_state

            _focus_row = in_progress_milestone(read_state(cursor))
            if isinstance(_focus_row, dict):
                _focus_mid = str(_focus_row.get("id") or "")
        cursor.progress_gate.record_pass(
            cap_id=cap_id if fuseable_cap(cap_id) else "tap_element",
            params=params if fuseable_cap(cap_id) else {},
            pre_fp=screen_fp,
            post_fp=post_fp,
            intents_done=set(getattr(cursor, "step_intents_done", None) or set()),
            login_flow_step=_lf_record,
            fingerprint_compared=not visual_scheme,
            milestone_id=_focus_mid,
            **_progress_gate_web_kwargs(ctx, proxy),
        )
        if isinstance(cursor, StepCursor):
            cursor.clear_recovery_block()
            if cursor.phase == "prep" and cap_id not in ("wait_ms", "wait_screen_ready"):
                cursor.prep_guard_streak = 0
            if cap_id == "swipe_direction" and status_val == "pass":
                cursor.note_swipe_pass(
                    direction=str((params or {}).get("direction") or ""),
                    pre_fp=screen_fp,
                    post_fp=post_fp,
                )
