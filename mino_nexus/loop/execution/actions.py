"""单次动作执行：派发到 router，恢复前缀走 recovery。"""
from __future__ import annotations

from typing import Any

from mino_nexus.catalog import registry as catalog
from mino_nexus.core.protocol import EventStatus
from mino_nexus.core.schemas import EventResult
from mino_nexus.loop.recovery import RuleMatch, apply_rule
from mino_nexus.loop.web.web_env import agent_step_idx

RECOVER_PREFIX = "recover_"

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
    if cap.startswith(RECOVER_PREFIX):
        rule_id = cap[len(RECOVER_PREFIX):]
        rule = catalog.get_recovery_rule(rule_id)
        if rule is None:
            return EventResult(
                seq=event.seq,
                capability_id=cap,
                event_kind=cap,
                status=EventStatus.FAIL,
                executor_used="recovery",
                summary=f"没有恢复规则 {rule_id}",
                error="missing rule",
            )
        return apply_rule(
            RuleMatch(rule=rule, reasons=["agent"]),
            ctx, proxy,
            target_package=str(getattr(ctx, "target_package", "") or ""),
            agent_turn=seq,
        )
    return proxy.dispatch(
        event,
        run_id=scout_run_id,
        step_idx=agent_step_idx(case_seq, seq),
        ctx=ctx,
        shot=shot,
    )
