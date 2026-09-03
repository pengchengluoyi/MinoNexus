"""内网域名：Nexus 启动后用 mDNS 注册 `mino.local`。

没有公网域名时，Console / Studio / Scout 都连 `http://mino.local:10104`
（Scout 的 WS 是 `ws://mino.local:10104/node`）。客户端不填、不发现 IP。

A 记录仍要带本机地址（mDNS 协议如此），但 HTTP / WS 对外只报主机名。
测试里设 `MINO_NEXUS_MDNS=0` 跳过广播。
"""
from __future__ import annotations

import os
import socket
from dataclasses import dataclass, field
from typing import Any, Optional

from mino_nexus.log import SLog

TAG = "MDNS"

LAN_HOST = "mino.local"
SERVICE_TYPE = "_mino-nexus._tcp.local."
DEFAULT_PORT = 10104

_bind_host = "0.0.0.0"
_bind_port = DEFAULT_PORT
_handle: Optional["BeaconHandle"] = None


@dataclass
class BeaconHandle:
    aiozc: Any
    services: list[Any] = field(default_factory=list)
    port: int = DEFAULT_PORT


def set_bind(host: str, port: int) -> None:
    global _bind_host, _bind_port
    _bind_host = str(host or "0.0.0.0")
    _bind_port = int(port or DEFAULT_PORT)


def bind_host() -> str:
    return _bind_host


def bind_port() -> int:
    return _bind_port


def http_origin(port: int | None = None) -> str:
    p = DEFAULT_PORT if port is None else int(port)
    return f"http://{LAN_HOST}:{p}"


def node_ws_url(port: int | None = None) -> str:
    p = DEFAULT_PORT if port is None else int(port)
    return f"ws://{LAN_HOST}:{p}/node"


def public_urls(port: int | None = None) -> dict[str, str]:
    p = bind_port() if port is None else int(port)
    return {
        "lan_host": LAN_HOST,
        "http_url": http_origin(p),
        "node_ws_url": node_ws_url(p),
        "ui_ws_url": f"ws://{LAN_HOST}:{p}/ws",
    }


def mdns_enabled() -> bool:
    raw = str(os.environ.get("MINO_NEXUS_MDNS") or "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def mdns_status() -> dict[str, Any]:
    return {
        "registered": bool(_handle and _handle.services),
        "hostname": LAN_HOST,
        "service": SERVICE_TYPE.rstrip("."),
    }


def configure_proxy_bypass() -> None:
    """连 mino.local 时不要走系统 HTTP 代理。"""
    extra = ["localhost", "127.0.0.1", "::1", LAN_HOST, "0.0.0.0"]
    for key in ("no_proxy", "NO_PROXY"):
        cur = [p.strip() for p in str(os.environ.get(key) or "").split(",") if p.strip()]
        for host in extra:
            if host not in cur:
                cur.append(host)
        os.environ[key] = ",".join(cur)


def _lan_ipv4(bind: str) -> str:
    """只给 mDNS A 记录用，禁止写进 HTTP 响应。"""
    if bind in ("127.0.0.1", "localhost", "::1"):
        return "127.0.0.1"
    candidates: list[str] = []
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        if ip and not ip.startswith("127.") and not ip.startswith("198.18."):
            candidates.append(ip)
    except OSError:
        pass
    finally:
        sock.close()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip.startswith("127.") or ip.startswith("198.18.") or ip in candidates:
                continue
            candidates.append(ip)
    except OSError:
        pass

    def _rank(ip: str) -> int:
        if ip.startswith(("192.168.", "10.")):
            return 0
        parts = ip.split(".")
        if ip.startswith("172.") and len(parts) > 1 and parts[1].isdigit() and 16 <= int(parts[1]) <= 31:
            return 0
        if ip.startswith("100."):
            return 1
        return 2

    candidates.sort(key=_rank)
    return candidates[0] if candidates else "127.0.0.1"


async def register_beacon(port: int | None = None) -> Optional[BeaconHandle]:
    global _handle
    configure_proxy_bypass()
    if not mdns_enabled():
        SLog.i(TAG, "mDNS 已关闭（MINO_NEXUS_MDNS=0）")
        return None
    p = bind_port() if port is None else int(port)
    try:
        from zeroconf import IPVersion, ServiceInfo
        from zeroconf.asyncio import AsyncZeroconf
    except ImportError as exc:
        SLog.w(TAG, f"zeroconf 未安装，跳过 {LAN_HOST} 注册: {exc}")
        return None

    ip = _lan_ipv4(bind_host())
    txt = {
        "role": "nexus",
        "lanHost": LAN_HOST,
        "http": http_origin(p),
        "node": node_ws_url(p),
        "version": "1",
    }
    info = ServiceInfo(
        SERVICE_TYPE,
        f"mino.{SERVICE_TYPE}",
        addresses=[socket.inet_aton(ip)],
        port=p,
        properties={k: v.encode("utf-8") for k, v in txt.items()},
        server=f"{LAN_HOST}.",
    )
    aiozc = AsyncZeroconf(ip_version=IPVersion.V4Only)
    try:
        await aiozc.async_register_service(info, allow_name_change=False)
    except Exception as exc:
        SLog.w(TAG, f"mDNS 注册失败（局域网仍可试 {http_origin(p)}）: {exc}")
        try:
            await aiozc.async_close()
        except Exception:
            pass
        return None

    handle = BeaconHandle(aiozc=aiozc, services=[info], port=p)
    _handle = handle
    SLog.i(TAG, f"已注册 {http_origin(p)}  ·  Scout {node_ws_url(p)}")
    return handle


async def unregister_beacon(handle: Optional[BeaconHandle] = None) -> None:
    global _handle
    target = handle or _handle
    if not target:
        return
    for info in target.services:
        try:
            await target.aiozc.async_unregister_service(info)
        except Exception as exc:
            SLog.w(TAG, f"mDNS 注销失败: {exc}")
    try:
        await target.aiozc.async_close()
    except Exception as exc:
        SLog.w(TAG, f"mDNS 关闭失败: {exc}")
    if _handle is target:
        _handle = None
