"""执行槽位的基础判断：Web 并发数与版本串有效性（纯函数）。"""
from __future__ import annotations

WEB_PLAYWRIGHT_PARALLEL_LANES = 4


def version_string_looks_invalid(ver: str) -> bool:
    v = str(ver or "").strip().lower()
    if not v:
        return True
    if "强停" in ver or "force-stop" in v or "force_stop" in v:
        return True
    if v.startswith("fail") or "error" in v[:24]:
        return True
    return False
