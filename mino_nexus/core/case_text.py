"""用例文本解析：编号列、引擎事件判别（纯函数，编译期与资源域都要用）。"""
from __future__ import annotations

import re
from typing import Any, Dict, List

_NUMBERED_PATTERN = re.compile(r"(?:^|\n)\s*(\d+)[.、．)\）]\s*")


def _strip_number_prefix(text: str) -> str:
    return re.sub(r"^\s*\d+[.、．)\）]\s*", "", text or "").strip()


def parse_numbered_items_rules(text: str) -> List[Dict[str, Any]]:
    """规则解析带编号的单元格（步骤/预期列）。"""
    raw = (text or "").strip()
    if not raw:
        return []
    matches = list(_NUMBERED_PATTERN.finditer(raw))
    if not matches:
        return [{"num": 1, "text": raw, "parse_method": "rules"}]
    items: List[Dict[str, Any]] = []
    prefix = raw[: matches[0].start()].strip()
    if prefix:
        items.append({"num": 1, "text": prefix, "parse_method": "rules"})
    for idx, match in enumerate(matches):
        try:
            num = int(match.group(1))
        except ValueError:
            continue
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(raw)
        body = _strip_number_prefix(raw[start:end].strip())
        if body:
            items.append({"num": num, "text": body, "parse_method": "rules"})
    if not items:
        return [{"num": 1, "text": _strip_number_prefix(raw), "parse_method": "rules"}]
    return items


def line_text(item: Any) -> str:
    """用例步骤/预期必须是给人看的字符串。执行事件对象不能写进这里。"""
    if item is None:
        return ""
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, (int, float)):
        return str(item)
    if isinstance(item, dict):
        if item.get("capability_id") or item.get("executor_used"):
            return ""
        for key in ("text", "title", "step", "content", "name", "label"):
            val = item.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        return ""
    return str(item).strip()


def looks_like_engine_step(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    if item.get("capability_id") or item.get("executor_used"):
        return True
    return item.get("seq") is not None and bool(item.get("status")) and "summary" in item


def spec_lines(items: Any) -> list[str]:
    if not isinstance(items, list):
        return []
    engine = [x for x in items if looks_like_engine_step(x)]
    if items and (all(looks_like_engine_step(x) for x in items) or len(engine) >= max(2, int(len(items) * 0.6))):
        return []
    return [t for t in (line_text(x) for x in items) if t]
