"""从 recovery 规则生成 interrupt 子里程碑（P2+）。"""
from __future__ import annotations

from typing import Any

from mino_nexus.loop.recovery import collect_evidence, match_rules

_MAX_STEPS = 32

_DISMISS_STEP: dict[str, Any] = {
    "id": "interrupt_tap_dismiss",
    "title": "点击关闭/跳过挡屏文案",
    "kind": "hook",
    "hook_cap": "tap_element",
    "optional": True,
}



# Explicit ordinary-runtime policy, shared by requirement construction and recovery.
# These actions remain subject to the committed binding, current epoch and write guard.
BUILTIN_INTERRUPT_CAPS = ("tap_element", "wait_ms", "wait_screen_ready")


def builtin_obstacle_steps(kind: str) -> list[dict[str, Any]]:
    if kind == "consent":
        return [{"id": "interrupt_accept_consent", "title": "确认当前应用协议弹窗，使弹窗关闭并恢复被遮挡页面",
                 "kind": "visual_tap", "hook_cap": "tap_element", "optional": False}]
    if kind == "loading":
        return [{"id": "interrupt_screen_ready", "title": "等待当前页面加载完成",
                 "kind": "hook", "hook_cap": "wait_screen_ready", "optional": False}]
    return []


def recovery_step_diagnostics(steps: list[dict[str, Any]], bound_ids: set[str]) -> list[dict[str, Any]]:
    """An ordered recovery is atomic at admission: never silently drop a child."""
    issues = []
    for step in steps:
        required = {str(step[k]) for k in ("hook_cap", "device_cap", "cap") if step.get(k)}
        alternatives = {str(cid) for cid in step.get("exec_caps", []) if cid}
        missing = required - bound_ids
        if missing or (alternatives and not alternatives & bound_ids) or not (required or alternatives):
            issues.append({"milestone_id": str(step.get("id") or ""),
                "reason_code": "recovery_operation_not_bound" if required or alternatives else "recovery_step_invalid",
                "missing_capabilities": sorted(missing | (alternatives if not alternatives & bound_ids else set()))})
    return issues


def bound_recovery_ids(ctx: Any, phase: str) -> set[str]:
    check = getattr(ctx, "execution_binding_menu", None)
    return set(check(phase) or ()) if callable(check) else set()


def _trace_selection(kind: str, payload: dict[str, Any]) -> None:
    from mino_nexus.loop.observe.session_log import active_writer
    writer = active_writer()
    if writer is not None:
        writer.append(kind, payload)


def _action_to_milestone(action: Any, idx: int, rule_id: str) -> dict[str, Any] | None:
    cap = str(getattr(action, "capability", "") or "").strip()
    if not cap:
        return None
    params = dict(getattr(action, "params", None) or {})
    target = dict(getattr(action, "target", None) or {})
    if target:
        params["target"] = target
    point = getattr(action, "fallback_xy", None)
    if isinstance(point, list) and len(point) == 2:
        params.setdefault("x", int(point[0]))
        params.setdefault("y", int(point[1]))
    sel = str(target.get("text") or target.get("selector_text") or params.get("selector_text") or "")[:120]
    row: dict[str, Any] = {
        "id": f"interrupt_{rule_id}_{idx}"[:64],
        "title": sel or cap,
        "kind": "hook",
        "hook_cap": cap,
        "optional": False,
        "source": "interrupt_recovery",
        "recovery_rule_id": rule_id,
    }
    if params:
        row["hook_params"] = params
    if sel:
        row["selector_hint"] = sel
    return row


def resolve_interrupt_steps(
    cursor: Any,
    ctx: Any,
    *,
    router: Any = None,
    target_package: str = "",
    screen_texts: list[str] | None = None,
    reason: str = "",
    allow_fallback: bool = True,
) -> list[dict[str, Any]]:
    """匹配 recovery 规则 actions；无则 dismiss + 返回/等待。"""
    steps: list[dict[str, Any]] = []
    from mino_nexus.core.platform_contract import target_platform
    from mino_nexus.loop.llm_step_context import step_scope_key
    plat = target_platform(getattr(ctx, "platform", ""))
    _, phase = step_scope_key(cursor)
    bound = bound_recovery_ids(ctx, phase)
    pkg = str(target_package or getattr(ctx, "target_package", "") or "").strip()
    try:
        if router is not None:
            ev = collect_evidence(ctx, router, target_package=pkg)
            hits = match_rules(ev, screen_texts=screen_texts, platform=plat)
            for hit in hits:
                rule = hit.rule
                mode = str(getattr(rule, "mode", "") or "").strip().lower()
                actions = list(getattr(rule, "actions", None) or [])
                if mode == "advise" or not actions:
                    continue
                if len(actions) > _MAX_STEPS:
                    _trace_selection("recovery/selection_rejected", {"rule_id": hit.rule_id,
                        "reason_code": "recovery_sequence_too_long", "action_count": len(actions)})
                    continue
                from mino_nexus.loop.recovery import _forbidden
                if any(_forbidden(rule, action) for action in actions):
                    _trace_selection("recovery/selection_rejected", {"rule_id": hit.rule_id,
                        "reason_code": "recovery_action_forbidden", "execution_started": "no"})
                    continue
                candidate = []
                for i, act in enumerate(actions):
                    row = _action_to_milestone(act, i, hit.rule_id)
                    if row:
                        candidate.append(row)
                issues = recovery_step_diagnostics(candidate, bound)
                if len(candidate) != len(actions):
                    issues.append({"reason_code": "recovery_step_invalid"})
                if issues:
                    _trace_selection("recovery/selection_rejected", {"rule_id": hit.rule_id,
                        "phase": phase, "diagnostics": issues, "execution_started": "no"})
                    continue
                if candidate:
                    steps = candidate
                    _trace_selection("recovery/selected", {"rule_id": hit.rule_id, "phase": phase,
                        "capability_ids": [row["hook_cap"] for row in steps]})
                    break
    except Exception as exc:  # Selection failure remains visible; it is not a no-match.
        _trace_selection("recovery/selection_error", {"phase": phase,
            "reason_code": getattr(exc, "reason_code", "recovery_selection_failed"),
            "error_type": type(exc).__name__, "execution_started": "no"})
        return []

    if not steps and allow_fallback:
        # 未命中配置规则时只等待；猜测关闭按钮或系统返回可能改变业务状态。
        steps.append(
            {
                "id": "interrupt_wait",
                "title": "等待界面稳定",
                "kind": "hook",
                "hook_cap": "wait_ms",
                "hook_params": {"ms": 800},
                "optional": True,
            }
        )
    issues = recovery_step_diagnostics(steps, bound)
    if issues:
        _trace_selection("recovery/selection_rejected", {"phase": phase, "diagnostics": issues,
                         "execution_started": "no"})
        return []
    return steps


__all__ = ["resolve_interrupt_steps"]
