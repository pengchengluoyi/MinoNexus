"""执行方案只由调用方指定。看图和 DOM 不因对方失败而改道。"""
from __future__ import annotations

from typing import Any


def action_scheme(ctx: Any) -> str:
    case = getattr(ctx, "case", None) if ctx is not None else None
    raw = ""
    if ctx is not None:
        raw = str(getattr(ctx, "action_scheme", "") or "").strip().lower()
    if not raw and isinstance(case, dict):
        raw = str(case.get("action_scheme") or "").strip().lower()
    if raw in ("dom", "visual"):
        return raw
    return "visual"
