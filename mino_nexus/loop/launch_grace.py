"""launch_app 后短时豁免 bring_target_app_foreground，避免异步启动误触发 recovery。"""
from __future__ import annotations

import time
from typing import Any

DEFAULT_LAUNCH_GRACE_SEC = 18.0


def stamp_launch_grace(ctx: Any, *, seconds: float = DEFAULT_LAUNCH_GRACE_SEC) -> None:
    if ctx is None:
        return
    sec = max(3.0, min(float(seconds or DEFAULT_LAUNCH_GRACE_SEC), 60.0))
    setattr(ctx, "launch_grace_until_mono", time.monotonic() + sec)


def clear_launch_grace(ctx: Any) -> None:
    if ctx is None:
        return
    setattr(ctx, "launch_grace_until_mono", 0.0)


def in_launch_grace(ctx: Any) -> bool:
    if ctx is None:
        return False
    until = float(getattr(ctx, "launch_grace_until_mono", 0) or 0)
    if until <= 0:
        return False
    if time.monotonic() > until:
        clear_launch_grace(ctx)
        return False
    if str(getattr(ctx, "app_foreground", "") or "").strip().lower() == "yes":
        clear_launch_grace(ctx)
        return False
    return True


def filter_foreground_recovery_hits(ctx: Any, hits: list[Any]) -> list[Any]:
    if not in_launch_grace(ctx):
        return hits
    out: list[Any] = []
    for h in hits:
        rid = str(getattr(getattr(h, "rule", None), "id", "") or "")
        if rid == "bring_target_app_foreground":
            continue
        out.append(h)
    return out
