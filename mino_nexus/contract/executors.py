"""设备类型 -> 允许的通道家族。纯映射，不含节点状态。

从 `loop/router_proxy.py` 下沉：`services` 需要它拼绑定，但不能因此 import 网关。
"""
from __future__ import annotations


def executors_for_platform(platform: str) -> tuple[str, ...]:
    """这台设备类型允许的通道。家族互斥：Web 没有 adb，安卓/iOS 没有 playwright。"""
    plat = str(platform or "").strip().lower()
    if plat in ("web", "browser", "playwright"):
        return ("playwright",)
    if plat in ("ios",):
        return ("ios_wda", "remote")
    if plat in {"desktop", "mac", "win"}:
        return ("desktop",)
    if plat == "android":
        return ("adb", "remote")
    return ()
