"""计划用到的逻辑块摘要：只让相关块变更使编译失效。"""
from __future__ import annotations

from typing import Any


def block_dependencies(plan: dict[str, Any], app_id: str) -> dict[str, str]:
    """只摘要本计划用到的逻辑块（含应用覆盖），不使无关块修改失效。"""
    import hashlib
    import json

    from mino_nexus.navigation.atlas.nav_flow_block_catalog import load_block_row, resolve_effective_steps

    result: dict[str, str] = {}
    def visit(block_id: str) -> None:
        if not block_id or block_id in result:
            return
        row = load_block_row(app_id=app_id, block_id=block_id) or {}
        effective = resolve_effective_steps(app_id=app_id, block_id=block_id)
        data = {"version": row.get("version"), "enabled": row.get("enabled"), "steps": effective}
        result[block_id] = hashlib.sha256(
            json.dumps(data, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
        ).hexdigest()[:16]
        for child in effective:
            if isinstance(child, dict):
                visit(str(child.get("block_id") or "").strip())

    for step in plan.get("steps") or []:
        if isinstance(step, dict):
            visit(str(step.get("block_id") or "").strip())
    return result
