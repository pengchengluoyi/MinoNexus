"""用例渠道 / 平台与执行设备是否匹配。"""
from __future__ import annotations

import copy
import re
from typing import Any, Optional

_SUPPORTED = frozenset({"android", "ios", "web", "desktop", "macos", "windows", "linux"})
_NATIVE_ALIASES = {
    "mac": "macos", "macos": "macos", "darwin": "macos", "osx": "macos",
    "pc": "windows", "win": "windows", "win32": "windows", "windows": "windows",
    "linux": "linux", "desktop": "desktop", "桌面": "desktop", "桌面端": "desktop",
}

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
    if p in _NATIVE_ALIASES:
        return _NATIVE_ALIASES[p]
    if p in _WEB_TOKENS or "web" in p:
        return "web"
    if p in _IOS_TOKENS or "ios" in p or "iphone" in p:
        return "ios"
    if p in _ANDROID_TOKENS or "android" in p:
        return "android"
    return p or "unknown"


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
        if tl in _NATIVE_ALIASES:
            out.add(_NATIVE_ALIASES[tl])
            continue
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
    order = ("android", "ios", "web", "desktop", "macos", "windows", "linux")
    names = {"android": "Android", "ios": "iOS", "web": "Web", "desktop": "PC 桌面",
             "macos": "macOS", "windows": "Windows", "linux": "Linux"}
    bits = [names[p] for p in order if p in platforms]
    return " / ".join(bits) if bits else "未知"


def bind_case_execution_target(
    case: dict[str, Any], target: dict[str, Any], *, surface: str,
    action_scheme: str = "dom_preferred", package: str = "",
) -> dict[str, Any]:
    """为明确选中的原生应用生成运行副本，原用例及任务快照保持不变。

    通用 Web 视觉用例可复用于原生应用；指定 Web 渠道、DOM 方案或
    Web 专属机态的用例不能借此绕过设备/应用的准入校验。
    """
    if target.get("platform") != "desktop" or action_scheme not in ("visual", "dom", "dom_preferred"):
        return case
    native = _norm_device_platform(str(target.get("target_platform") or ""))
    if native not in {"macos", "windows", "linux"}:
        return case
    if native != _norm_device_platform(str(target.get("os") or "")):
        return case
    if not surface or surface != target.get("target_surface") or not target.get("executable"):
        return case
    raw = str(case.get("platform") or "").strip()
    want = parse_case_platforms(case)
    # 只有通用 Web 标签可复用；browser / playwright 等明确执行端不改写。
    reusable_web = raw.lower() == "web"
    if raw and want is None and raw.lower() not in {"any", "all", "*"}:
        return case
    if want and native not in want and "desktop" not in want and not reusable_web:
        return case
    scenes = [case.get(key) for key in ("case_scene", "scene")]
    meta = case.get("meta") if isinstance(case.get("meta"), dict) else {}
    claims = [case.get("resource_key"), meta.get("resource_key")]
    scenes += [claim.get("case_scene") for claim in claims if isinstance(claim, dict)]
    for row in [*scenes, *claims]:
        if not isinstance(row, dict):
            continue
        if row.get("device_need") in {"web", "app_web", "ab_pair"}:
            return case
        declared = str(row.get("platform") or "").strip().lower()
        if declared and declared not in {"any", "all", "*"}:
            if _norm_device_platform(declared) not in {native, "desktop"}:
                if not reusable_web or declared != "web":
                    return case
    out = copy.deepcopy(case)
    out["platform"] = native
    previous = case.get("execution_target") if isinstance(case.get("execution_target"), dict) else {}
    out["execution_target"] = {
        "source_platform": str(previous.get("source_platform", raw)),
        "platform": native, "device_platform": "desktop", "os": native,
        "surface": surface, "sn": str(target.get("sn") or ""), "action_scheme": action_scheme,
    }
    for key in ("case_scene", "scene"):
        if isinstance(out.get(key), dict):
            out[key]["platform"] = native
    out_meta = out.get("meta") if isinstance(out.get("meta"), dict) else {}
    for claim in (out.get("resource_key"), out_meta.get("resource_key")):
        if not isinstance(claim, dict):
            continue
        claim["platform"] = native
        if isinstance(claim.get("case_scene"), dict):
            claim["case_scene"]["platform"] = native
        if isinstance(claim.get("device_app"), dict) and "platform" in claim["device_app"]:
            claim["device_app"]["platform"] = native
        if package:
            claim.setdefault("target_app", {})["package"] = package
    return out


def platform_skip_reason(
    case: dict[str, Any], device_platform: str, *, device_os: str = "",
) -> Optional[str]:
    """设备平台与用例渠道不兼容时返回 skip 说明。"""
    # New native targets never interpret Mobile/pc/desktop/empty as permission.
    if device_platform in {"mac", "win"}:
        from mino_nexus.core.platform_contract import TARGET_PLATFORMS, coverage
        if "platform_scope" in case:
            try:
                declared = coverage(case)
                if declared["platform_scope"] == "all" or device_platform in declared["platforms"]:
                    return None
            except ValueError:
                return "用例平台合同无效，请明确平台后重新发布"
        elif case.get("platform") in TARGET_PLATFORMS:
            if case["platform"] == device_platform:
                return None
        else:
            return "用例平台尚未迁移确认，请明确 mac / win / android / web 后重新发布"
        return f"用例平台与执行设备 {device_platform} 不匹配"
    want = parse_case_platforms(case)
    if not want:
        return None
    have = _norm_device_platform(device_platform)
    native = _norm_device_platform(device_os) if have == "desktop" else have
    if have in want or native in want or ("desktop" in want and native in {"macos", "windows", "linux"}):
        return None
    case_plat = str(case.get("platform") or "").strip() or platform_label(want)
    device_name = platform_label({native}) if native in _SUPPORTED else native
    if have == "desktop" and native not in {"macos", "windows", "linux"}:
        device_name = "PC 桌面（系统未上报）"
    name = str(case.get("name") or case.get("case_id") or "用例").strip()
    return (
        f"渠道不匹配，跳过：{name} 要求 {case_plat}，当前设备为 {device_name}。"
        f"请将用例分配到 {platform_label(want)} 设备后再跑。"
    )


__all__ = [
    "parse_case_platforms",
    "platform_label",
    "platform_skip_reason",
    "bind_case_execution_target",
]
