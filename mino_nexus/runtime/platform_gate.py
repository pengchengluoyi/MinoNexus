"""用例渠道 / 平台与执行设备是否匹配。"""
from __future__ import annotations

import re
from typing import Any, Optional

_SUPPORTED = frozenset({"android", "ios", "web"})

_DUAL_TOKENS = frozenset({
    "双端", "双平台", "ios+android", "android+ios", "ios/android", "android/ios",
    "mobile", "app", "native",
})
_IOS_TOKENS = frozenset({"ios", "iphone", "ipad", "苹果", "apple", "appleid", "apple id"})
_ANDROID_TOKENS = frozenset({"android", "安卓", "andriod"})
_WEB_TOKENS = frozenset({"web", "h5", "browser", "网页", "浏览器", "playwright"})


def _norm_device_platform(raw: str) -> str:
    p = str(raw or "").strip().lower()
    if p in _SUPPORTED:
        return p
    if p in _WEB_TOKENS or "web" in p:
        return "web"
    if p in _IOS_TOKENS or "ios" in p or "iphone" in p:
        return "ios"
    return "android"


def _split_tokens(text: str) -> list[str]:
    raw = str(text or "").strip()
    if not raw:
        return []
    parts = re.split(r"[,，/、\s+|]+", raw)
    return [p.strip() for p in parts if p.strip()]


def parse_case_platforms(case: dict[str, Any]) -> Optional[set[str]]:
    """用例声明的渠道集合。None 表示未限制（任意已支持平台可跑）。"""
    raw = str(case.get("platform") or "").strip()
    if not raw:
        scene = case.get("case_scene") if isinstance(case.get("case_scene"), dict) else {}
        raw = str((scene or {}).get("platform") or "").strip()
    if not raw or raw.lower() in {"any", "all", "*"}:
        return None

    lower = raw.lower()
    if raw in _DUAL_TOKENS or lower in _DUAL_TOKENS or "双端" in raw:
        return {"android", "ios"}

    out: set[str] = set()
    for token in _split_tokens(raw):
        tl = token.lower()
        if token in _DUAL_TOKENS or "双端" in token:
            out.update({"android", "ios"})
            continue
        if tl in _WEB_TOKENS or any(x in tl for x in ("web", "h5", "browser", "网页")):
            out.add("web")
            continue
        if tl in _IOS_TOKENS or "ios" in tl or "iphone" in tl or "苹果" in token:
            out.add("ios")
            continue
        if tl in _ANDROID_TOKENS or "安卓" in token or "android" in tl:
            out.add("android")
            continue
    if not out:
        if any(x in lower for x in ("web", "h5", "网页")):
            out.add("web")
        elif any(x in lower for x in ("ios", "iphone", "ipad", "苹果", "apple")):
            out.add("ios")
        elif any(x in lower for x in ("android", "安卓")):
            out.add("android")
    return out or None


def platform_label(platforms: set[str]) -> str:
    order = ("android", "ios", "web")
    names = {"android": "Android", "ios": "iOS", "web": "Web"}
    bits = [names[p] for p in order if p in platforms]
    return " / ".join(bits) if bits else "未知"


def platform_skip_reason(case: dict[str, Any], device_platform: str) -> Optional[str]:
    """设备平台与用例渠道不兼容时返回 skip 说明。"""
    want = parse_case_platforms(case)
    if not want:
        return None
    have = _norm_device_platform(device_platform)
    if have in want:
        return None
    case_plat = str(case.get("platform") or "").strip() or platform_label(want)
    device_name = {"android": "Android", "ios": "iOS", "web": "Web"}.get(have, have)
    name = str(case.get("name") or case.get("case_id") or "用例").strip()
    return (
        f"渠道不匹配，跳过：{name} 要求 {case_plat}，当前设备为 {device_name}。"
        f"请将用例分配到 {platform_label(want)} 设备后再跑。"
    )


__all__ = [
    "parse_case_platforms",
    "platform_label",
    "platform_skip_reason",
]
