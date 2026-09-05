"""用例文本的规则解析。

port(nexus): 从上游 `server/services/shared/semantic/case_text_semantic_service.py`
（417 行）里**只摘出 `parse_numbered_items_rules`** —— planner 里唯一用到的那个。

那个文件其余部分依赖 `expectation_semantic_service`（会调 LLM 做语义比对），
属于 shared/semantic 整体，等它那一批一起搬。这里先取纯正则的部分。
"""
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

