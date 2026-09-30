"""机态和前置登录字段的对应。不新增枚举。

账号 facet `session` 已有：
- logged_out = 未登录
- guest = 游客
- logged_in = 已登录

前置「未登录」「已登出」「已退出」编译成 logged_out（未登录这一档）。
「游客」编译成 guest。
设备登记簿里的 guest 与 logged_out 互相满足这两档，不要求前置改字段名。
"""
from __future__ import annotations

import re

DEVICE_SESSIONS = frozenset({"logged_in", "logged_out", "guest"})
UNSIGNED_DEVICE_SESSIONS = frozenset({"logged_out", "guest"})
_UNSIGNED_TEXT_RE = re.compile(r"未登录|已登出|已退出")
_GUEST_TEXT_RE = re.compile(r"游客")
_LOGGED_IN_TEXT_RE = re.compile(r"已登录")


def precondition_device_session(value: str) -> str:
    """前置登录态原文 → guest | logged_in | any。未登录与已登出都是 guest 这一档。"""
    v = str(value or "").strip()
    if _UNSIGNED_TEXT_RE.search(v) or _GUEST_TEXT_RE.search(v):
        return "guest"
    if _LOGGED_IN_TEXT_RE.search(v):
        return "logged_in"
    return "any"


def precondition_account_session(value: str) -> str:
    """账号登录字段。未登录/已登出用已有 logged_out；只有游客时用已有 guest。"""
    v = str(value or "").strip()
    if _GUEST_TEXT_RE.search(v) and not _UNSIGNED_TEXT_RE.search(v):
        return "guest"
    if _UNSIGNED_TEXT_RE.search(v) or _GUEST_TEXT_RE.search(v):
        return "logged_out"
    if _LOGGED_IN_TEXT_RE.search(v):
        return "logged_in"
    return "any"


def normalize_device_session(value: str) -> str:
    """机态只有 logged_in / logged_out / guest。空值和 unknown 都是 guest。"""
    sess = str(value or "").strip().lower()
    if sess in DEVICE_SESSIONS:
        return sess
    return "guest"


def device_session_meets(required: str, current: str, allow: list[str] | None = None) -> bool:
    """当前机态是否满足前置要求。guest 承接未登录(logged_out) 和游客(guest)。"""
    req = str(required or "").strip().lower()
    cur = normalize_device_session(current)
    if not req or req == "any":
        return True
    if cur == req:
        return True
    allowed = [str(x).strip().lower() for x in (allow or []) if str(x).strip()]
    allowed = [normalize_device_session(x) if x == "unknown" else x for x in allowed]
    if cur in allowed:
        return True
    if req in UNSIGNED_DEVICE_SESSIONS and cur in UNSIGNED_DEVICE_SESSIONS:
        return True
    return False
