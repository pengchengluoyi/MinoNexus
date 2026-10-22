"""回合里的「事实」读取：控件、效应、前台上下文。纯读，不动作。"""
from __future__ import annotations

from typing import Any

from mino_nexus.core.log import SLog

_TAG = "AgentLoop"

def _control_from_turn(cap_id: str, params: dict[str, Any], cursor: Any) -> tuple[str, str]:
    control_id = str(params.get("control_id") or "")
    target_desc = str(params.get("selector_text") or params.get("text") or "")
    from mino_nexus.loop.milestone_orchestrator import in_progress_milestone_id

    focus_id = in_progress_milestone_id(cursor)
    plan = getattr(cursor, "do_program_plan", None)
    if isinstance(plan, dict):
        for step in plan.get("steps") or []:
            if not isinstance(step, dict):
                continue
            if not focus_id or str(step.get("id") or "") != focus_id:
                continue
            if str(step.get("kind") or "") == "flow_block":
                # 逻辑块作者步骤的控件身份不能套到发码、输入、勾选等独立子事件。
                continue
            same = str(step.get("hook_cap") or "") == cap_id or (
                cap_id == "tap_element" and str(step.get("action_type") or "") in ("click", "check")
            )
            if not same:
                continue
            control_id = control_id or str(step.get("control_id") or "")
            target_desc = target_desc or str(step.get("target_desc") or "")
            break
    return control_id, target_desc


def _locator_blocks_action(ctx: Any, cap_id: str, params: dict[str, Any], cursor: Any) -> str:
    # 整页滑动和刷新不以某个控件为目标，不能套控件定位门槛。
    if cap_id not in ("tap_element", "input_text"):
        return ""
    control_id, target_desc = _control_from_turn(cap_id, params, cursor)
    if not control_id:
        return ""
    from mino_nexus.action_space.scheme import execution_scheme as action_scheme
    from mino_nexus.knowledge.control_registry import locator_stop_reason, runtime_readonly

    platform = str(getattr(ctx, "platform", "") or getattr(ctx, "device_platform", "") or "")
    ordinary = not runtime_readonly()
    if action_scheme(ctx) == "dom" and ordinary and cap_id in ("tap_element", "input_text"):
        from mino_nexus.action_space.scheme import structure_target_available
        from mino_nexus.knowledge.control_registry import get_control

        project_id = str(getattr(ctx, "project_id", "") or "")
        control = get_control(project_id, control_id)
        scoped = bool(control) and str(control.get("project_id") or "") == project_id
        typed_locator = bool(control) and bool(control.get("locator_spec"))
        if scoped and not typed_locator and structure_target_available(cap_id, params, ctx):
            # 普通任务的本轮唯一结构锚点可以执行；不把文本/ref 冒充已保存的 CSS 定位。
            return ""
    dom_locator = str(params.get("selector") or params.get("css") or params.get("xpath") or params.get("resource_id") or "")
    return locator_stop_reason(
        project_id=str(getattr(ctx, "project_id", "") or ""),
        control_id=control_id,
        scheme=action_scheme(ctx),
        platform=platform,
        dom_locator=dom_locator,
        target_desc=target_desc,
    )


def _note_control_locator(writer: Any, ctx: Any, cap_id: str, params: dict[str, Any], cursor: Any) -> None:
    """按执行渠道和设备渠道记下用了哪一格定位。看图不把坐标写进控件。"""
    if writer is None or cap_id not in ("tap_element", "input_text"):
        return
    control_id, target_desc = _control_from_turn(cap_id, params, cursor)
    if not control_id:
        return
    from mino_nexus.action_space.scheme import execution_scheme as action_scheme
    from mino_nexus.knowledge.control_registry import note_execution_locator

    platform = str(getattr(ctx, "platform", "") or getattr(ctx, "device_platform", "") or "")
    if action_scheme(ctx) == "dom":
        from mino_nexus.action_space.scheme import structure_target_available

        if structure_target_available(cap_id, params, ctx) and cap_id in ("tap_element", "input_text"):
            writer.append("control/locator", {
                "control_id": control_id, "scheme": "dom", "platform": platform,
                "resolved": True, "source": "current_observation", "persisted": False,
            })
            return
    dom_locator = str(params.get("selector") or params.get("css") or params.get("xpath") or "")
    note_execution_locator(
        writer,
        project_id=str(getattr(ctx, "project_id", "") or ""),
        control_id=control_id,
        scheme=action_scheme(ctx),
        platform=platform,
        dom_locator=dom_locator,
        target_desc=target_desc,
    )


def _effect_nodes(nav, ctx, extra_vlm: Any = None) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    snap = getattr(nav, "snapshot", None) if nav is not None else None
    if snap is not None:
        nodes = [n for n in (getattr(snap, "nodes", None) or []) if isinstance(n, dict)]
    vlm = extra_vlm if isinstance(extra_vlm, dict) else getattr(ctx, "nav_vlm_hierarchy", None)
    vlm_nodes = list((vlm or {}).get("nodes") or []) if isinstance(vlm, dict) else []
    if not vlm_nodes:
        return nodes
    try:
        from mino_nexus.navigation.atlas.nav_vlm_hierarchy import merge_vlm_into_nodes

        merged = merge_vlm_into_nodes(nodes, vlm_nodes)
        return [n for n in merged if isinstance(n, dict)]
    except Exception as exc:  # noqa: BLE001
        SLog.w(_TAG, f"merge vlm hierarchy failed: {type(exc).__name__}: {exc}")
        return nodes
