"""单次动作执行：派发到 router，恢复前缀走 recovery。"""
from __future__ import annotations

from typing import Any

from mino_nexus.core.protocol import EventStatus
from mino_nexus.core.schemas import EventResult
from mino_nexus.loop.web.web_env import agent_step_idx

def _run_action(*, event: PlanEvent, shot, proxy: RouterProxy, ctx, scout_run_id: str, case_seq: int, seq: int) -> Any:
    from mino_nexus.loop.resolved_case import dispatch_gate
    dispatch_gate(ctx, event.capability_id)
    cap = event.capability_id
    from mino_nexus.catalog.skill_channel import capability_dispatch_ok

    if not capability_dispatch_ok(cap):
        return EventResult(
            seq=event.seq,
            capability_id=cap,
            event_kind=cap,
            status=EventStatus.FAIL,
            executor_used="local",
            summary=f"能力未启用或当前不可覆盖: {cap}",
            error="capability_disabled",
        )
    # 恢复不再有 recover_* 虚拟能力：模型改派 run_flow_block(block_id)，
    # 由 local_executors 按块目录执行步骤（见 loop/block_dispatch.py）。
    return proxy.dispatch(
        event,
        run_id=scout_run_id,
        step_idx=agent_step_idx(case_seq, seq),
        ctx=ctx,
        shot=shot,
    )
