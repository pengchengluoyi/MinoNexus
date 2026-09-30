"""安卓载荷把 point_policy 交给 Scout。coordinate 禁止锚点和吸附，node 禁止坐标兜底。"""
from __future__ import annotations

from typing import Any


def apply_android_point(params: dict[str, Any], *, mode: str) -> dict[str, Any]:
    out = dict(params or {})
    out["point_policy"] = "node" if mode == "node" else "coordinate"
    return out
