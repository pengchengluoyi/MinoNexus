"""从 recovery 规则生成 interrupt 子里程碑（P2+）。"""
from __future__ import annotations

from typing import Any

from mino_nexus.loop.recovery import collect_evidence, match_rules

_MAX_STEPS = 4

_DISMISS_STEP: dict[str, Any] = {
    "id": "interrupt_tap_dismiss",
    "title": "点击关闭/跳过挡屏文案",
    "kind": "hook",
    "hook_cap": "tap_element",
    "optional": True,
}


def _action_to_milestone(action: Any, idx: int, rule_id: str) -> dict[str, Any] | None:
    cap = str(getattr(action, "capability", "") or "").strip()
    if not cap:
        return None
    params = dict(getattr(action, "params", None) or {})
    target = dict(getattr(action, "target", None) or {})
    sel = str(target.get("text") or target.get("selector_text") or params.get("selector_text") or "")[:120]
    row: dict[str, Any] = {
        "id": f"interrupt_{rule_id}_{idx}"[:64],
        "title": sel or cap,
        "kind": "hook",
        "hook_cap": cap,
        "optional": True,
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
) -> list[dict[str, Any]]:
    """匹配 recovery 规则 actions；无则 dismiss + 返回/等待。"""
    steps: list[dict[str, Any]] = []
    plat = str(getattr(ctx, "platform", "") or "android").strip().lower()
    pkg = str(target_package or getattr(ctx, "target_package", "") or "").strip()
    try:
        if router is not None:
            ev = collect_evidence(ctx, router, target_package=pkg)
            hits = match_rules(ev, screen_texts=screen_texts, platform=plat)
            for hit in hits[:2]:
                rule = hit.rule
                mode = str(getattr(rule, "mode", "") or "").strip().lower()
                actions = list(getattr(rule, "actions", None) or [])
                if mode == "advise" or not actions:
                    continue
                for i, act in enumerate(actions[:_MAX_STEPS]):
                    row = _action_to_milestone(act, i, hit.rule_id)
                    if row:
                        steps.append(row)
                if steps:
                    break
    except Exception:  # noqa: BLE001
        steps = []

    if not steps:
        steps.append(dict(_DISMISS_STEP))
        steps.append(
            {
                "id": "interrupt_back",
                "title": "系统返回",
                "kind": "hook",
                "hook_cap": "press_key",
                "hook_params": {"key": "BACK"},
                "optional": True,
            }
        )
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
    return steps[:_MAX_STEPS]


__all__ = ["resolve_interrupt_steps"]
