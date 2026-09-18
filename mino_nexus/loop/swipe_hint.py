"""步骤原文 → swipe_direction 参数（与用例「上滑/下滑」一致，不硬编码 App）。"""
from __future__ import annotations

import re

_DOWN_RE = re.compile(r"向?下(?:滑|拉|滚动)|下滑", re.I)
_UP_RE = re.compile(r"向?上(?:滑|拉|滚动)|上滑", re.I)
_LEFT_RE = re.compile(r"向?左(?:滑|拉)|左滑", re.I)
_RIGHT_RE = re.compile(r"向?右(?:滑|拉)|右滑", re.I)


def instruction_swipe_direction(instruction: str) -> str:
    """返回 swipe_direction.direction：up/down/left/right，未写明则 \"\"。"""
    text = str(instruction or "").strip()
    if not text:
        return ""
    if _DOWN_RE.search(text):
        return "down"
    if _UP_RE.search(text):
        return "up"
    if _LEFT_RE.search(text):
        return "left"
    if _RIGHT_RE.search(text):
        return "right"
    return ""


def swipe_direction_label(direction: str) -> str:
    return {
        "down": "向下滑动（direction=down）",
        "up": "向上滑动（direction=up）",
        "left": "向左滑动（direction=left）",
        "right": "向右滑动（direction=right）",
    }.get(str(direction or "").strip().lower(), "")
