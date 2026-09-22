"""系统权限弹窗：优先点「使用时允许」类；其次点厂商 grant 文案；不点拒绝/不允许。"""
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

_GRANT_LIKE_RE = re.compile(
    r"使用.{0,8}应用|使用.{0,6}时|应用时|仅此一次|始终允许|允许访问|"
    r"授予.{0,4}权限|授权|权限|Allow|While using|Only this time|All the time|"
    r"Grant|Permission",
    re.I,
)

_DENY_RE = re.compile(
    r"拒绝|不允许|禁止|勿允许|Deny|Don't allow|Do not allow|Never",
    re.I,
)


def _node_texts(node: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for key in ("text", "content_desc", "label"):
        val = str(node.get(key) or "").strip()
        if val:
            out.append(val)
    return out


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
        if t and _ALLOW_RE.search(t) and not _DENY_RE.search(t):
            candidates.append(t)
    for node in hierarchy_nodes or []:
        if not isinstance(node, dict):
            continue
        if node.get("clickable") is False:
            continue
        for val in _node_texts(node):
            if val and _ALLOW_RE.search(val) and not _DENY_RE.search(val):
                candidates.append(val)
    if not candidates:
        return None
    ranked: list[tuple[int, str]] = []
    for raw in candidates:
        for i, pref in enumerate(_PREFERRED_ALLOW_ORDER):
            if pref in raw or raw in pref:
                ranked.append((i, pref if len(raw) > 24 else raw[:32]))
                break
        else:
            ranked.append((len(_PREFERRED_ALLOW_ORDER), raw[:32]))
    ranked.sort(key=lambda x: x[0])
    return ranked[0][1]


def pick_permission_grant_text(
    *,
    screen_texts: list[str] | None = None,
    hierarchy_nodes: list[dict[str, Any]] | None = None,
    match_reasons: list[str] | None = None,
) -> Optional[str]:
    """Allow 正则未命中时，匹配厂商 grant 类按钮（含「使用应用时」等）。"""
    pool: list[str] = []
    for r in match_reasons or []:
        text = str(r or "")
        if text.startswith("screen_text~"):
            pool.append(text.split("~", 1)[-1].strip())
    for t in screen_texts or []:
        t = str(t or "").strip()
        if t and _GRANT_LIKE_RE.search(t) and not _DENY_RE.search(t):
            pool.append(t)
    for node in hierarchy_nodes or []:
        if not isinstance(node, dict):
            continue
        if node.get("clickable") is False:
            continue
        for val in _node_texts(node):
            if len(val) > 48:
                continue
            if val and _GRANT_LIKE_RE.search(val) and not _DENY_RE.search(val):
                pool.append(val)
    if not pool:
        return None
    ranked: list[tuple[int, str]] = []
    for raw in pool:
        score = 50
        if "使用" in raw and "应用" in raw:
            score = 5
        elif "使用时" in raw or "应用时" in raw:
            score = 8
        elif "允许" in raw:
            score = 12
        elif re.search(r"Allow|While using", raw, re.I):
            score = 10
        ranked.append((score, raw[:32]))
    ranked.sort(key=lambda x: x[0])
    return ranked[0][1]


def pick_permission_tap_for_grant(
    *,
    screen_texts: list[str] | None = None,
    hierarchy_nodes: list[dict[str, Any]] | None = None,
    match_reasons: list[str] | None = None,
) -> tuple[Optional[str], str]:
    """返回 (tap_text, kind) kind ∈ allow|grant|''。"""
    allow = pick_permission_allow_text(
        screen_texts=screen_texts,
        hierarchy_nodes=hierarchy_nodes,
        match_reasons=match_reasons,
    )
    if allow:
        return allow, "allow"
    grant = pick_permission_grant_text(
        screen_texts=screen_texts,
        hierarchy_nodes=hierarchy_nodes,
        match_reasons=match_reasons,
    )
    if grant:
        return grant, "grant"
    return None, ""


def pick_permission_dismiss_text(
    *,
    hierarchy_nodes: list[dict[str, Any]] | None = None,
) -> Optional[str]:
    """仅用于非权限挡屏的「取消/关闭」，绝不返回拒绝/不允许。"""
    pool = [n for n in (hierarchy_nodes or []) if isinstance(n, dict)]
    found: list[str] = []
    for node in pool:
        if not node.get("clickable"):
            continue
        for val in _node_texts(node):
            if not val or len(val) > 8:
                continue
            if _DENY_RE.search(val):
                continue
            for pref in ("取消", "Cancel", "关闭", "Close"):
                if pref in val or val == pref:
                    found.append(val if len(val) <= 8 else pref)
                    break
    if not found:
        return None
    for pref in ("取消", "Cancel", "关闭", "Close"):
        if pref in found:
            return pref
    return found[0]
