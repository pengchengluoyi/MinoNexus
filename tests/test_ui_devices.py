"""UI device rows: online follows Scout channels, not merely node_alive."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="mino-nexus-ui-dev-")
os.environ["MINO_NEXUS_DATA_DIR"] = _TMP

from mino_nexus import protocol as P  # noqa: E402
from mino_nexus.node_registry import NodeRegistry  # noqa: E402
from mino_nexus.ui_devices import device_ui_online, ui_devices  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}{' — ' + detail if detail else ''}")
        failures.append(name)


def _noop_send(*_a, **_k):
    return None


print("== device_ui_online ==")
check("node+adb", device_ui_online(True, {"adb": "connected"}) is True)
check("node+disconnected", device_ui_online(True, {"adb": "disconnected"}) is False)
check("node+unauthorized", device_ui_online(True, {"adb": "unauthorized"}) is False)
check("node+offline adb", device_ui_online(True, {"adb": "offline"}) is False)
check("dead node connected", device_ui_online(False, {"adb": "connected"}) is False)
check("empty channels", device_ui_online(True, {}) is False)
check("playwright up", device_ui_online(True, {"playwright": "connected"}) is True)

print("== registry row after unplug ==")
reg = NodeRegistry()
reg.register(
    P.Register(
        node_id="box-1", token="t", platform="darwin", scout_version="0.1.0",
        executors=[P.ExecutorManifest(id="adb", available=True, provides=["tap"])],
        devices=[P.DeviceManifest(sn="5fda2f6d", platform="android", model="25102RKBEC",
                                  channels={"adb": "connected"})],
    ),
    send=_noop_send,
)
reg.heartbeat(P.Heartbeat(
    node_id="box-1", uptime_sec=2, busy=False,
    device_delta=[P.DeviceManifest(sn="5fda2f6d", platform="android", model="25102RKBEC",
                                   channels={"adb": "disconnected"})],
))
rows = {d["sn"]: d for d in ui_devices(reg)}
phone = rows.get("5fda2f6d") or {}
check("still listed", "5fda2f6d" in rows)
check("status offline", phone.get("status") == "offline", str(phone))
check("adb_state copied", phone.get("channels", {}).get("adb_state") == "disconnected", str(phone.get("channels")))
check("no plaintext password", "password" not in phone or not phone.get("password"), str(phone))
check("password_configured flag", phone.get("password_configured") is False, str(phone))
check("last_online set", bool(phone.get("last_online")), str(phone.get("last_online")))

print()
if failures:
    print(f"FAILED {len(failures)}: {', '.join(failures)}")
    sys.exit(1)
print("ALL OK — ui devices")
