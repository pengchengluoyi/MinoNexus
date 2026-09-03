"""mino.local 对外只报主机名，不报 IPv4。

    python tests/test_mdns.py
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("MINO_NEXUS_MDNS", "0")

from mino_nexus.mdns import (  # noqa: E402
    LAN_HOST,
    SERVICE_TYPE,
    configure_proxy_bypass,
    http_origin,
    mdns_status,
    node_ws_url,
    public_urls,
)

failures: list[str] = []
IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}{' — ' + detail if detail else ''}")
        failures.append(name)


print("== constants ==")
check("lan host", LAN_HOST == "mino.local")
check("service type", SERVICE_TYPE.startswith("_mino-nexus._tcp"))
check("http origin", http_origin(10104) == "http://mino.local:10104")
check("node ws", node_ws_url(10104) == "ws://mino.local:10104/node")

print("== public_urls 不含 IPv4 ==")
urls = public_urls(10104)
blob = " ".join(str(v) for v in urls.values())
check("keys", set(urls) >= {"lan_host", "http_url", "node_ws_url"})
check("no ipv4", not IPV4.search(blob), blob)
check("status hostname", mdns_status().get("hostname") == LAN_HOST)
check("status not registered in tests", mdns_status().get("registered") is False)

print("== no_proxy ==")
os.environ["no_proxy"] = "example.com"
os.environ.pop("NO_PROXY", None)
configure_proxy_bypass()
check("no_proxy has mino.local", LAN_HOST in os.environ.get("no_proxy", ""))
check("NO_PROXY synced", LAN_HOST in os.environ.get("NO_PROXY", ""))

print()
if failures:
    print(f"FAILED {len(failures)}: {', '.join(failures)}")
    sys.exit(1)
print("ALL OK — mdns public names")
