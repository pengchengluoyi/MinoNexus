"""视觉载荷：只留坐标。不写 target，Scout 不得按文案改点，也不得吸附。"""
from __future__ import annotations

from typing import Any

_LABEL_KEYS = (
    "target",
    "selector_text",
    "description",
    "label",
    "content_desc",
    "anchor_between",
    "fallback_xy",
)


def apply_visual_point(params: dict[str, Any], *, keep_text: bool = False) -> dict[str, Any]:
    out = dict(params or {})
    for key in _LABEL_KEYS:
        out.pop(key, None)
    if not keep_text and "field" not in out:
        out.pop("text", None)
    out["point_policy"] = "coordinate"
    return out
