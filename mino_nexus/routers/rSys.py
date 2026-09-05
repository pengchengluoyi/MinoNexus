"""登录页 ping 与运行态。"""
from __future__ import annotations

from fastapi import APIRouter, Request

from mino_nexus.core.http_util import ok
from mino_nexus.core.mdns import LAN_HOST, mdns_status, node_ws_url, public_urls
from mino_nexus.services.node_registry import get_registry
from mino_nexus.services.ui_devices import ui_nodes
from mino_nexus.websocket.node import NEXUS_VERSION

router = APIRouter(tags=["Sys"])


@router.get("/sys/server_info")
def sys_server_info():
    urls = public_urls()
    return ok({
        "service": "MinoNexus",
        "version": NEXUS_VERSION,
        **urls,
        "mdns": mdns_status(),
    })


@router.get("/sys/runtime")
def sys_runtime(request: Request):
    port = request.url.port or 10104
    host = request.url.hostname or LAN_HOST
    urls = public_urls(port)
    nodes = get_registry().nodes()
    alive = sum(1 for n in nodes if n.alive)
    return ok({
        "electron": {"online": False, "pid": None, "version": None, "platform": "web"},
        "embeddedServer": {"running": True, "pid": None},
        "endpoints": [{"name": LAN_HOST, "url": urls["http_url"], "online": True}],
        "isLocalGateway": host in ("127.0.0.1", "localhost", LAN_HOST),
        "lan_host": urls["lan_host"],
        "http_url": urls["http_url"],
        "node_ws_url": node_ws_url(port),
        "mdns": mdns_status(),
        "node": {
            "role": "nexus",
            "connected": True,
            "sn": "",
            "is_master": True,
            "candidates": [],
            "node_count": len(nodes),
            "nodes_alive": alive,
        },
        "nodes": ui_nodes(),
        "node_count": len(nodes),
        "nexus_version": NEXUS_VERSION,
    })
