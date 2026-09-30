"""DOM 载荷：只认节点锚点。锚点没命中时 Scout 不得改用模型坐标。"""
from __future__ import annotations

from typing import Any


def apply_dom_point(params: dict[str, Any]) -> dict[str, Any]:
    out = dict(params or {})
    out["point_policy"] = "node"
    return out
