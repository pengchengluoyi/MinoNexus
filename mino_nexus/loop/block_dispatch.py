"""按块 id 执行逻辑块步骤（模型与程序共用一条通路）。

L0 恢复以前是「recover_<rule_id> 虚拟能力 + 规则自带 actions」，
动作写两遍、名字也只有规则知道。现在统一成：块目录声明步骤，
由 ``loop/event_interpreter.py`` 这一个解释器按顺序派发；
模型侧只暴露一个 run_flow_block(block_id)。
"""
from __future__ import annotations

from typing import Any

TAG = "block_dispatch"


def recovery_block_ids() -> list[str]:
    """L0 恢复规则当前引用的块。仅用于菜单摘要，不再是派发白名单。"""
    from mino_nexus.catalog.flow_block_seed import RECOVERY_RULE_BLOCKS

    return sorted(set(RECOVERY_RULE_BLOCKS.values()))


def dispatchable_block_ids(*, app_id: str = "") -> list[str]:
    """所有已启用、可在当前应用下解析到步骤的块。

    规划 §4/§6：登录、协议、私有分支由意图图调度，不再靠 recovery 白名单藏能力。
    这里只按目录（块是否存在、是否 enabled）判断，不按块名猜用途。
    """
    from mino_nexus.navigation.atlas.nav_flow_block_catalog import list_catalog, resolve_effective_steps

    out: list[str] = []
    for row in list_catalog(app_id=str(app_id or "")):
        bid = str(row.get("block_id") or "").strip()
        if not bid or bid in out:
            continue
        try:
            steps = resolve_effective_steps(app_id=str(app_id or ""), block_id=bid)
        except Exception:  # noqa: BLE001 - 单个坏块不能清空整个菜单
            continue
        if steps:
            out.append(bid)
    return sorted(out)


def is_block_dispatchable(block_id: str, *, app_id: str = "") -> bool:
    from mino_nexus.navigation.atlas.nav_flow_block_catalog import load_block_row

    row = load_block_row(app_id=str(app_id or ""), block_id=str(block_id or ""))
    return bool(row and row.get("enabled") and row.get("steps_json"))


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
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """委托给单一事件解释器；保留此入口给恢复/本地 executor 使用。"""
    from mino_nexus.loop.event_interpreter import run_block as _run_block

    out = _run_block(block_id, ctx=ctx, router=router, run_id=run_id, purpose=purpose,
                     target_package=target_package, params=params)
    out.setdefault("block_id", str(block_id or ""))
    return out
