"""登出类 capability 白名单（cap_id → cap_logout trigger）。勿用子串匹配，避免误伤。"""
from __future__ import annotations

# 与 registry / recovery_seed 对齐；新增登出能力时在此登记。
LOGOUT_CAPABILITY_IDS: frozenset[str] = frozenset(
    {
        "clear_app_cache",
        "system_pkg_clear",
    }
)


def is_logout_capability(cap_id: str) -> bool:
    cid = str(cap_id or "").strip()
    if not cid:
        return False
    if cid in LOGOUT_CAPABILITY_IDS:
        return True
    low = cid.lower()
    if low.endswith("_logout") or low.startswith("logout_"):
        return True
    if low.endswith("_log_out") or low.startswith("log_out_"):
        return True
    if low.endswith("_sign_out") or low.startswith("sign_out_"):
        return True
    return False
