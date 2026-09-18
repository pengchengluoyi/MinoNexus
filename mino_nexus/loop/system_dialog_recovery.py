"""系统权限弹窗统一处理（AOSP / MIUI / 厂商变体）。"""
from __future__ import annotations

from typing import Any, Optional

from mino_nexus.core.schemas import PlanEvent

SYSTEM_PERMISSION_RULE_PREFIX = "system_permission_dialog"

SYSTEM_PERMISSION_RULE_IDS = frozenset(
    {
        "system_permission_dialog_while_using_deterministic",
        "system_permission_dialog",
        "system_permission_dialog_unified",
    }
)


def is_system_permission_rule(rule_id: str) -> bool:
    rid = str(rule_id or "").strip()
    if rid in SYSTEM_PERMISSION_RULE_IDS:
        return True
    return rid.startswith(SYSTEM_PERMISSION_RULE_PREFIX)


def try_unified_system_permission_recovery(
    match: Any,
    *,
    ctx: Any,
    router: Any,
    target_package: str,
    agent_turn: int,
    rule: Any,
    out: Any,
) -> bool:
    if router is None:
        return False
    from mino_nexus.core.log import SLog
    from mino_nexus.loop.recovery_permission import pick_permission_allow_text, pick_permission_dismiss_text

    def _dispatch(event, action_idx):
        from mino_nexus.loop.recovery import _dispatch as rd

        return rd(router, ctx, event, agent_turn=agent_turn, action_idx=action_idx)

    def _evidence():
        from mino_nexus.loop.recovery import collect_evidence

        return collect_evidence(ctx, router, target_package=target_package)

    def _verify(ev, verify):
        from mino_nexus.loop.recovery import _match_conditions

        return _match_conditions(verify, ev, [])

    TAG = "SystemDialogRecovery"
    ev = _evidence()
    nodes = list(getattr(ctx, "nav_hierarchy_nodes", None) or [])
    vlm = getattr(ctx, "nav_vlm_hierarchy", None)
    if isinstance(vlm, dict):
        nodes = nodes + [n for n in (vlm.get("nodes") or []) if isinstance(n, dict)]
    allow = pick_permission_allow_text(
        hierarchy_nodes=nodes,
        match_reasons=list(getattr(match, "reasons", None) or []),
    )
    dismiss = pick_permission_dismiss_text(hierarchy_nodes=nodes) if not allow else None
    tap_text = allow or dismiss
    if not tap_text:
        return False
    kind = "allow" if allow else "dismiss"
    pre = PlanEvent(
        seq=0,
        capability_id="tap_element",
        event_kind="tap_element",
        params={"selector_text": tap_text},
        ai_reasoning=f"系统权限弹窗统一处理({kind})：{tap_text}",
        label=tap_text[:40],
    )
    res = _dispatch(pre, 1)
    st = getattr(res.status, "value", res.status)
    out.actions.append(
        {
            "capability": "tap_element",
            "status": str(st),
            "summary": res.summary,
            "unified_kind": kind,
            "preferred_text": tap_text,
        }
    )
    out.applied = True
    _dispatch(
        PlanEvent(
            seq=0,
            capability_id="wait_ms",
            event_kind="wait_ms",
            params={"duration_ms": 450},
            ai_reasoning="系统弹窗点击后等待",
            label="wait",
        ),
        2,
    )
    if hasattr(router, "observe"):
        from mino_nexus.loop.screen_capture import merge_shot_evidence

        merge_shot_evidence(ev, router.observe("screenshot", force_fresh=True))
    verify = rule.verify
    ok, _ = _verify(ev, verify)
    if ok:
        out.recovered = True
        SLog.i(TAG, f"[{rule.id}] unified {kind}「{tap_text}」恢复成功")
        return True
    if kind == "allow" and dismiss and dismiss != tap_text:
        res2 = _dispatch(
            PlanEvent(
                seq=0,
                capability_id="tap_element",
                event_kind="tap_element",
                params={"selector_text": dismiss},
                ai_reasoning="allow 未过 verify，尝试 dismiss",
                label=dismiss[:40],
            ),
            3,
        )
        out.actions.append(
            {
                "capability": "tap_element",
                "status": str(getattr(res2.status, "value", res2.status)),
                "summary": res2.summary,
                "unified_kind": "dismiss_fallback",
            }
        )
        ev2 = _evidence()
        if hasattr(router, "observe"):
            from mino_nexus.loop.screen_capture import merge_shot_evidence

            merge_shot_evidence(ev2, router.observe("screenshot", force_fresh=True))
        ok2, _ = _verify(ev2, verify)
        if ok2:
            out.recovered = True
            return True
    return False
