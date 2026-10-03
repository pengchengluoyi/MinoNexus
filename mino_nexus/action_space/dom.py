"""DOM 载荷：只认节点锚点。不带坐标，Scout 不得改用模型坐标。"""
from __future__ import annotations

from typing import Any

_COORD_KEYS = ("x", "y", "fallback_xy", "from_x", "from_y", "to_x", "to_y")


def apply_dom_point(params: dict[str, Any]) -> dict[str, Any]:
    out = dict(params or {})
    for key in _COORD_KEYS:
        out.pop(key, None)
    out["point_policy"] = "node"
    return out
