"""系统权限弹窗：优先点屏上已识别到的「使用时允许」类文案。"""
from __future__ import annotations

import re
from typing import Any, Optional

_PREFERRED_ALLOW_ORDER = (
    "仅在使用中允许",
    "仅在使用该应用时允许",
    "使用时允许",
    "使用应用时允许",
    "Allow while using the app",
    "While using the app",
    "允许",
)

_ALLOW_RE = re.compile(
    r"仅在使用中允许|仅在使用该应用时允许|使用时允许|使用应用时允许|"
    r"Allow while using the app|While using the app|允许",
    re.I,
)


def pick_permission_allow_text(
    *,
    screen_texts: list[str] | None = None,
    hierarchy_nodes: list[dict[str, Any]] | None = None,
    match_reasons: list[str] | None = None,
) -> Optional[str]:
    """从 match reasons / 屏上 OCR / hierarchy 挑最可信的一条 allow 文案。"""
    candidates: list[str] = []
    for r in match_reasons or []:
        text = str(r or "")
        if text.startswith("screen_text~"):
            candidates.append(text.split("~", 1)[-1].strip())
    for t in screen_texts or []:
        t = str(t or "").strip()
        if t and _ALLOW_RE.search(t):
            candidates.append(t)
    for node in hierarchy_nodes or []:
        if not isinstance(node, dict):
            continue
        for key in ("text", "content_desc", "label"):
            val = str(node.get(key) or "").strip()
            if val and _ALLOW_RE.search(val):
                candidates.append(val)
    if not candidates:
        return None
    ranked: list[tuple[int, str]] = []
    for raw in candidates:
        for i, pref in enumerate(_PREFERRED_ALLOW_ORDER):
            if pref in raw or raw in pref:
                ranked.append((i, pref))
                break
        else:
            ranked.append((len(_PREFERRED_ALLOW_ORDER), raw[:32]))
    ranked.sort(key=lambda x: x[0])
    return ranked[0][1]


_DISMISS_ORDER = ("取消", "拒绝", "不允许", "禁止", "Deny", "Cancel", "CLOSE")


def pick_permission_dismiss_text(
    *,
    hierarchy_nodes: list[dict[str, Any]] | None = None,
) -> Optional[str]:
    """权限/系统挡屏上的关闭/拒绝控件（无 allow 文案时）。"""
    pool = [n for n in (hierarchy_nodes or []) if isinstance(n, dict)]
    found: list[str] = []
    for node in pool:
        if not node.get("clickable"):
            continue
        for key in ("text", "content_desc", "label"):
            val = str(node.get(key) or "").strip()
            if not val or len(val) > 8:
                continue
            for pref in _DISMISS_ORDER:
                if pref in val or val == pref:
                    found.append(pref if len(val) > 6 else val)
                    break
    if not found:
        return None
    for pref in _DISMISS_ORDER:
        if pref in found:
            return pref
    return found[0]
