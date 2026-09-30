"""Web 载荷仍按坐标点击。DOM 路径的坐标是节点中心，不是模型另给的点。"""
from __future__ import annotations

from typing import Any


def apply_web_point(params: dict[str, Any]) -> dict[str, Any]:
    out = dict(params or {})
    out["web_coordinate_only"] = True
    out["tap_prefer_coordinates"] = True
    return out
