"""本用例+本阶段允许的能力菜单：渠道过滤后的可用集合。"""
from __future__ import annotations

from typing import Any

from mino_nexus.catalog.skill_channel import canonical_cap


def case_menu_ids(ctx: Any, phase: str) -> set[str]:
    from mino_nexus.runtime.menu import available_menu_brief

    if ctx is None:
        return set()
    extra: list[str] = []
    if getattr(ctx, "allow_model_recovery", False):
        pass  # Navigation is a plan policy, never a model-callable event.
    rows = available_menu_brief(
        ctx,
        audience="case",
        phase=str(phase or "do"),
        platform=str(getattr(ctx, "platform", "") or ""),
        extra_ids=extra,
    )
    return {canonical_cap(str(row.get("id") or "")) for row in rows if str(row.get("id") or "").strip()}
