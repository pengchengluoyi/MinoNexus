"""UI 节点的属性归一：布尔标记与整型数组（纯函数）。"""
from __future__ import annotations

from typing import Any

_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def node_flag(node: dict[str, Any] | None, key: str) -> bool:
    """checkable / checked / focused：协议可能给 bool 或 0/1 字符串。"""
    if not isinstance(node, dict):
        return False
    val = node.get(key)
    if isinstance(val, bool):
        return val
    if val is None:
        return False
    text = str(val).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    return bool(val)


def int_list(raw: Any, size: int) -> list[int]:
    """只接受 list/tuple；字符串不当字符序列迭代。缺位补 0。"""
    if size <= 0:
        return []
    if not isinstance(raw, (list, tuple)):
        return [0] * size
    out = [safe_int(item, 0) for item in list(raw)[:size]]
    if len(out) < size:
        out.extend([0] * (size - len(out)))
    return out
