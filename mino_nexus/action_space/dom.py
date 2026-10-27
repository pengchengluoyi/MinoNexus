"""DOM 载荷：锚点优先，坐标作为 Scout 必须执行的兜底。"""
from __future__ import annotations

from typing import Any

_COORD_KEYS = ("x", "y", "from_x", "from_y", "to_x", "to_y")


def _pair(value: Any) -> list[int] | None:
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        try:
            return [int(value[0]), int(value[1])]
        except (TypeError, ValueError):
            return None
    return None


def apply_dom_point(params: dict[str, Any]) -> dict[str, Any]:
    """唯一执行方式是 DOM：``point_policy=node``，坐标留在 ``fallback_xy``。

    Nexus 已经算出的点必须跟指令一起下发。锚点没命中时 Scout 按这个点执行，
    不再因为「DOM 不用坐标」把整条用例判死。
    """
    src = dict(params or {})
    fallback = _pair(src.get("fallback_xy"))
    if fallback is None and src.get("x") is not None and src.get("y") is not None:
        fallback = _pair((src.get("x"), src.get("y")))
    out = dict(src)
    for key in _COORD_KEYS:
        out.pop(key, None)
    out.pop("fallback_xy", None)
    if fallback is not None:
        out["fallback_xy"] = fallback
    out["point_policy"] = "node"
    return out
