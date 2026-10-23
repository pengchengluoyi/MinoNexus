"""按块 id 执行逻辑块步骤（模型与程序共用一条通路）。

L0 恢复以前是「recover_<rule_id> 虚拟能力 + 规则自带 actions」，
动作写两遍、名字也只有规则知道。现在统一成：块目录声明步骤，这里按顺序派发，
模型侧只暴露一个 run_flow_block(block_id)。
"""
from __future__ import annotations

from typing import Any

from mino_nexus.core.log import SLog
from mino_nexus.core.protocol import EventStatus
from mino_nexus.core.schemas import PlanEvent

TAG = "block_dispatch"


def recovery_block_ids() -> list[str]:
    """允许模型直接执行的块（目前只有 L0 恢复对应的那几块）。"""
    from mino_nexus.catalog.flow_block_seed import RECOVERY_RULE_BLOCKS

    return sorted(set(RECOVERY_RULE_BLOCKS.values()))


def resolve_block_steps(block_id: str, *, app_id: str = "") -> list[dict[str, Any]]:
    from mino_nexus.navigation.atlas.nav_flow_block_catalog import resolve_effective_steps

    return list(resolve_effective_steps(app_id=str(app_id or ""), block_id=str(block_id or "")) or [])


def run_block(
    block_id: str,
    *,
    ctx: Any,
    router: Any,
    run_id: str = "",
    purpose: str = "plan_action",
    target_package: str = "",
) -> dict[str, Any]:
    """按块步骤顺序派发，遇失败即停；返回聚合结果（不抛异常）。"""
    block = str(block_id or "").strip()
    if not block:
        return {"ok": False, "status": "fail", "error": "missing_block_id",
                "summary": "没有给出逻辑块 id", "actions": []}
    steps = resolve_block_steps(block, app_id=str(getattr(ctx, "app_id", "") or ""))
    if not steps:
        SLog.w(TAG, f"逻辑块 {block} 没有可执行步骤")
        return {"ok": False, "status": "fail", "error": "block_empty",
                "summary": f"逻辑块 {block} 没有可执行步骤", "actions": []}
    pkg = str(target_package or getattr(ctx, "target_package", "") or "")
    rid = str(run_id or getattr(ctx, "run_id", "") or "")
    actions: list[dict[str, Any]] = []
    last_error = ""
    for idx, step in enumerate(steps, 1):
        if not isinstance(step, dict):
            continue
        cap = str(step.get("hook_cap") or step.get("cap") or "").strip()
        if not cap:
            continue
        params = dict(step.get("params") or {})
        if cap in ("launch_app", "close_app") and pkg:
            params.setdefault("package", pkg)
        if cap in ("launch_app", "open_app"):
            params.setdefault("package", pkg)
        event = PlanEvent(
            seq=idx,
            capability_id=cap,
            event_kind=cap,
            params=params,
            ai_reasoning=f"逻辑块 {block}：{step.get('title') or cap}",
            label=str(step.get("title") or cap),
        )
        try:
            res = router.dispatch(event, run_id=rid, step_idx=idx, ctx=ctx, diagnostic_purpose=purpose)
        except Exception as exc:  # noqa: BLE001
            SLog.w(TAG, f"逻辑块 {block} 步骤 {cap} 派发异常：{exc!r}")
            last_error = str(exc)
            actions.append({"capability": cap, "status": "fail", "summary": last_error[:200]})
            break
        status = str(getattr(getattr(res, "status", None), "value", getattr(res, "status", "")) or "")
        ok = status == EventStatus.PASS.value
        actions.append({"capability": cap, "status": status or "unknown",
                        "summary": str(getattr(res, "summary", "") or "")[:200]})
        if not ok:
            last_error = str(getattr(res, "error", "") or getattr(res, "summary", "") or status)
            break
    ok_all = bool(actions) and all(a["status"] == EventStatus.PASS.value for a in actions)
    return {
        "ok": ok_all,
        "status": "pass" if ok_all else "fail",
        "error": "" if ok_all else (last_error or "block_step_failed"),
        "summary": "；".join(f"{a['capability']}:{a['status']}" for a in actions)[:300],
        "actions": actions,
        "block_id": block,
    }
