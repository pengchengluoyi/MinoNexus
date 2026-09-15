"""用例步骤效果探针：预期文案是否已在屏上出现（纯 hierarchy，不调 LLM）。"""
from __future__ import annotations

import re
from typing import Any

from mino_nexus.loop.hierarchy_slots import match_any

_PREFIXES = (
    "跳转到",
    "切换到",
    "进入",
    "显示",
    "出现",
    "底部",
    "界面",
    "tab",
    "Tab",
)


def _clean_chunk(text: str) -> str:
    s = str(text or "").strip()
    for prefix in _PREFIXES:
        if s.lower().startswith(prefix.lower()):
            s = s[len(prefix) :].strip()
    if s.endswith("页面") and len(s) > 2:
        s = s[:-2]
    elif s.endswith("页") and len(s) > 2:
        s = s[:-1]
    if s.endswith("提示") and len(s) > 3:
        s = s[:-2]
    return s.strip("：:，,、 ")


def _probe_terms(expected: str) -> list[str]:
    raw = str(expected or "").strip()
    if not raw:
        return []
    terms: list[str] = []
    for chunk in re.split(r"[、,/；;|｜]", raw):
        cleaned = _clean_chunk(chunk)
        if len(cleaned) >= 2 and cleaned not in terms:
            terms.append(cleaned)
    if not terms:
        cleaned = _clean_chunk(raw)
        if len(cleaned) >= 2:
            terms.append(cleaned)
    extra: list[str] = []
    for term in list(terms):
        if len(term) >= 4:
            for width in (2, 3):
                piece = term[-width:]
                if len(piece) >= 2 and piece not in terms and piece not in extra:
                    extra.append(piece)
    terms.extend(extra)
    return terms[:12]


def probe(expected: str, nodes: list[dict[str, Any]]) -> tuple[bool, list[str]]:
    """本步预期文案是否已在屏上出现。返回 (命中, 命中的关键词)。"""
    terms = _probe_terms(expected)
    if not terms or not nodes:
        return False, []
    hits: list[str] = []
    for term in terms:
        conds = [
            {"text_contains": term},
            {"content_desc_contains": term},
        ]
        if match_any(nodes, conds):
            hits.append(term)
    return bool(hits), hits


def achievement_hint(keywords: list[str]) -> str:
    if not keywords:
        return ""
    shown = " / ".join(str(k) for k in keywords[:6])
    return (
        f"【达成提示】本步预期已在屏上出现（命中：{shown}）。"
        f"若无其它待办，立即 signal_done。"
    )
