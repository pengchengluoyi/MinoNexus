"""这台设备此刻能走哪些 channel。

从 `loop/router_proxy.py` 下沉：绑定校验需要它，但不能为了一个映射 import Scout 网关。
依赖只有 `runtime.run_context.is_web_slot` 与节点快照。
"""
from __future__ import annotations

from typing import Any

from mino_nexus.contract.executors import executors_for_platform

_CHANNEL_EXEC = (("desktop", "desktop"), ("playwright", "playwright"), ("adb", "adb"), ("remote", "remote"), ("ios", "ios_wda"))
_ONLINE = frozenset({"connected", "online", "available", "authenticated"})


def device_platform(sn: str, node) -> str:
    from mino_nexus.contract.execution.device import is_web_slot

    if is_web_slot(sn):
        return "web"
    dev = node.devices.get(sn) if node is not None else None
    plat = str(getattr(dev, "platform", "") or "").strip().lower()
    if plat:
        return plat
    return "unknown"


def channel_state(raw: Any) -> str:
    if isinstance(raw, dict):
        return str(raw.get("state") or raw.get("status") or "").strip().lower()
    return str(raw or "").strip().lower()


def executors_for_device(sn: str, node) -> list[str]:
    """这台 sn 此刻能走的 executor。"""
    plat = device_platform(sn, node)
    family = list(executors_for_platform(plat))
    avail = set(node.available_executors())
    ordered = [ex for ex in family if ex in avail]
    dev = node.devices.get(sn)
    connected: list[str] = []
    if dev is not None and isinstance(dev.channels, dict):
        for ch, ex in _CHANNEL_EXEC:
            if ex not in family:
                continue
            if channel_state((dev.channels or {}).get(ch)) in _ONLINE:
                connected.append(ex)
    if plat in {"desktop", "mac", "win"}:
        return [ex for ex in ordered if ex in connected] if dev and dev.desktop_state == "ready" else []
    if connected:
        return [ex for ex in ordered if ex in set(connected)]
    return ordered
