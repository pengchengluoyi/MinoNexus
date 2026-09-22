"""经 Scout 切换 Android 默认输入法（Nexus 不直连 adb）。"""
from __future__ import annotations

import inspect
from typing import Any

from mino_nexus.core import protocol as P
from mino_nexus.services.device_secrets import get_lock_password
from mino_nexus.services.node_registry import NodeSession, get_registry


def _device_hint(sn: str, node: NodeSession) -> dict[str, Any]:
    dev = node.devices.get(sn)
    hint: dict[str, Any] = {"platform": "android"}
    if dev is not None:
        hint["model"] = dev.model
        serial = (dev.channels or {}).get("serial") or ""
        hint["adb_serial"] = serial or sn
    else:
        hint["adb_serial"] = sn
    password = get_lock_password(sn)
    if password:
        hint["password"] = password
    return hint


async def ensure_adb_keyboard(sn: str) -> dict[str, Any]:
    return await _dispatch_set_input_method(sn, ime="adbkeyboard")


async def restore_system_input_method(sn: str) -> dict[str, Any]:
    """经 Scout 关闭 ADB Keyboard，恢复其它已启用输入法。"""
    return await _dispatch_set_input_method(sn, ime="system")


async def _dispatch_set_input_method(sn: str, *, ime: str) -> dict[str, Any]:
    key = str(sn or "").strip()
    if not key:
        raise ValueError("请指定设备 sn")

    node, why = get_registry().resolve(key)
    if node is None:
        raise RuntimeError(why or "找不到在线 Scout 节点")

    dev = node.devices.get(key)
    plat = str(getattr(dev, "platform", "") or "android").lower()
    if plat not in ("android", "other"):
        raise ValueError(f"设备 {key} 不是 Android（platform={plat}）")

    req = P.Execute(
        run_id="",
        step_idx=-1,
        sn=key,
        capability_id="set_input_method",
        params={"ime": ime},
        executor_order=["adb"],
        low_level={},
        selected_impl={},
        device_hint=_device_hint(key, node),
        timeout_sec=30.0,
        device_id=key,
        platform="android",
    )
    if node.send is None:
        raise RuntimeError("节点未就绪，无法下发")

    result = node.send(P.MsgType.EXECUTE, req, timeout=35.0)
    if inspect.isawaitable(result):
        result = await result
    if result is None:
        raise RuntimeError("Scout 未应答")

    raw = dict(getattr(result, "raw_response", None) or {})
    status = getattr(getattr(result, "status", None), "value", None) or getattr(result, "status", "")
    ok = str(status).lower() in ("pass", "passed", "ok", "success")
    return {
        "sn": key,
        "node_id": node.node_id,
        "status": status,
        "ok": ok,
        "summary": getattr(result, "summary", "") or "",
        "error": getattr(result, "error", "") or "",
        **raw,
    }
