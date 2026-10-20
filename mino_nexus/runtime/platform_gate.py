"""Case admission against the current target-platform contract."""
from __future__ import annotations
from typing import Any
from mino_nexus.core.platform_contract import covered_platforms, target_platform, TARGET_PLATFORMS


def parse_case_platforms(case: dict[str, Any]) -> set[str]:
    return set(covered_platforms(case))


def platform_label(platforms: set[str]) -> str:
    names = {"mac": "macOS", "win": "Windows", "android": "Android", "web": "Web"}
    return " / ".join(names[p] for p in TARGET_PLATFORMS if p in platforms) or "无适用平台"


def platform_skip_reason(case: dict[str, Any], device_platform: str, *, device_os: str = "") -> str | None:
    try:
        have = target_platform(device_platform)
        allowed = covered_platforms(case)
    except ValueError as exc:
        return f"平台合同无效：{exc}；请明确 mac / win / android / web 或平台覆盖范围"
    if have not in allowed:
        return f"用例适用平台 {platform_label(set(allowed))} 与执行设备 {platform_label({have})} 不匹配"
    return None
