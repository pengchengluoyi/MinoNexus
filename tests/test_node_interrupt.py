"""节点 shutting_down / 断开时立刻中断在途 run。"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="mino-nexus-node-")
os.environ["MINO_NEXUS_DATA_DIR"] = _TMP

from mino_nexus import protocol as P  # noqa: E402
from mino_nexus import run_store  # noqa: E402
from mino_nexus.node_registry import NodeRegistry  # noqa: E402
from mino_nexus.ui_devices import ui_devices  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}{' — ' + detail if detail else ''}")
        failures.append(name)


def _noop_send(*_a, **_k):
    return None


print("== register + vanished device stays disconnected ==")
reg = NodeRegistry()
first = P.Register(
    node_id="box-1", token="t", platform="darwin", scout_version="0.1.0",
    executors=[P.ExecutorManifest(id="adb", available=True, provides=["tap"])],
    devices=[P.DeviceManifest(sn="old", platform="android", channels={"adb": "connected"}),
             P.DeviceManifest(sn="keep", platform="android", channels={"adb": "connected"})],
)
reg.register(first, send=_noop_send)
second = P.Register(
    node_id="box-1", token="t", platform="darwin", scout_version="0.1.0",
    executors=[P.ExecutorManifest(id="adb", available=True, provides=["tap"])],
    devices=[P.DeviceManifest(sn="keep", platform="android", channels={"adb": "connected"})],
)
reg.register(second, send=_noop_send)
node = reg.get_node("box-1")
check("keep still there", node is not None and "keep" in node.devices)
check("old stubbed", node is not None and node.devices["old"].channels.get("adb") == "disconnected")

print("== heartbeat device_delta adds a phone plugged in after REGISTER ==")
reg.heartbeat(P.Heartbeat(
    node_id="box-1", uptime_sec=2, busy=False,
    device_delta=[P.DeviceManifest(sn="5fda2f6d", platform="android", model="25102RKBEC",
                                   channels={"adb": "connected"})],
))
node = reg.get_node("box-1")
check("new sn in cache", node is not None and "5fda2f6d" in node.devices)
check("adb connected", node is not None and node.devices["5fda2f6d"].channels.get("adb") == "connected")
owned, reason = reg.resolve("5fda2f6d")
check("resolve to box-1", owned is not None and owned.node_id == "box-1", reason)
rows = {d["sn"]: d for d in ui_devices(reg)}
check("new phone ui online", rows.get("5fda2f6d", {}).get("status") == "online", str(rows.get("5fda2f6d")))
check("new phone adb_state", rows.get("5fda2f6d", {}).get("channels", {}).get("adb_state") == "connected")

print("== heartbeat device_delta unplug marks phone offline ==")
reg.heartbeat(P.Heartbeat(
    node_id="box-1", uptime_sec=3, busy=False,
    device_delta=[P.DeviceManifest(sn="5fda2f6d", platform="android", model="25102RKBEC",
                                   channels={"adb": "disconnected"})],
))
node = reg.get_node("box-1")
check("cache still has sn", node is not None and "5fda2f6d" in node.devices)
check("adb disconnected", node is not None and node.devices["5fda2f6d"].channels.get("adb") == "disconnected")
rows = {d["sn"]: d for d in ui_devices(reg)}
check("ui offline after unplug", rows.get("5fda2f6d", {}).get("status") == "offline", str(rows.get("5fda2f6d")))
check("keep still ui online", rows.get("keep", {}).get("status") == "online", str(rows.get("keep")))

print("== shutting_down returns active runs ==")
reg.heartbeat(P.Heartbeat(node_id="box-1", uptime_sec=4, busy=True, active_runs=["cr-aaa"]))
got = reg.node_event(P.NodeEvent(node_id="box-1", event="shutting_down", detail="active_runs=cr-aaa"))
check("interrupt list", got == ["cr-aaa"])
check("cleared after event", reg.get_node("box-1").active_runs == [])
check("draining", reg.get_node("box-1").draining is True)

print("== disconnect returns leftover runs ==")
reg.heartbeat(P.Heartbeat(node_id="box-1", uptime_sec=4, busy=True, active_runs=["cr-bbb"]))
dropped = reg.disconnect("box-1")
check("disconnect runs", dropped == ["cr-bbb"])
check("node gone", reg.get_node("box-1") is None)

print("== run_store.interrupt_runs ==")
doc = {
    "run_id": "cr-int",
    "status": "running",
    "app_id": "app-1",
    "cases": [{"case_id": "c1", "status": "running", "name": "c1"}],
}
run_store.put(doc)
done = run_store.interrupt_runs(["cr-int", "missing"], reason="节点断开")
check("interrupted", done == ["cr-int"])
again = run_store.get("cr-int")
check("failed status", again.get("status") == "failed")
check("pending case cancelled", again["cases"][0]["status"] == "cancelled")
check("idempotent", run_store.interrupt_runs(["cr-int"], reason="again") == [])

print()
if failures:
    print(f"FAILED {len(failures)}: {', '.join(failures)}")
    sys.exit(1)
print("ALL OK — node interrupt")
