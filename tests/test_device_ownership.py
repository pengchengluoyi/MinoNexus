"""ADB 设备搬家：只留一行，账号跟首次出现的人，当前位置跟当前 Scout。

    python tests/test_device_ownership.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="mino-dev-own-")
os.environ["MINO_NEXUS_DATA_DIR"] = _TMP

from mino_nexus import device_store as ds  # noqa: E402
from mino_nexus import protocol as P  # noqa: E402
from mino_nexus.node_registry import NodeRegistry  # noqa: E402
from mino_nexus.ui_devices import ui_assets  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}{' — ' + detail if detail else ''}")
        failures.append(name)


def _noop(*_a, **_k):
    return None


def _phone(sn: str = "5fda2f6d") -> P.DeviceManifest:
    return P.DeviceManifest(sn=sn, platform="android", model="25102RKBEC", channels={"adb": "connected"})


def _reg(node_id: str, *, studio_id: str = "", devices: list[P.DeviceManifest] | None = None) -> P.Register:
    return P.Register(
        node_id=node_id,
        token="t",
        platform="darwin",
        scout_version="0.1.0",
        hostname=node_id,
        studio_id=studio_id,
        executors=[P.ExecutorManifest(id="adb", available=True, provides=["ui_screenshot"])],
        devices=list(devices or []),
    )


print("== 同一 ADB 从 Scout A 挪到 Scout B ==")
reg = NodeRegistry()
phone = _phone()
reg.register(_reg("scoutaaa1111111", studio_id="studioaaa1111111", devices=[phone]), send=_noop, owner_user_id="usera")
first = ds.get_device(phone.sn)
check("首次落盘", first is not None and first.get("owner_user_id") == "usera", str(first))
check("首次 Scout", first.get("first_scout_id") == "scoutaaa1111111", str(first))

reg.register(_reg("scoutbbb2222222", studio_id="studiobbb2222222", devices=[phone]), send=_noop, owner_user_id="userb")
again = ds.get_device(phone.sn)
rows = [r for r in ds.list_devices().values() if r.get("sn") == phone.sn]
check("仍然一行", len(rows) == 1, str(rows))
check("账号仍是 A", again.get("owner_user_id") == "usera", str(again))
check("当前位置是 B", again.get("current_scout_id") == "scoutbbb2222222", str(again))
check("首次 Scout 不变", again.get("first_scout_id") == "scoutaaa1111111", str(again))

node, why = reg.resolve(phone.sn)
check("派单跟当前 Scout B", node is not None and node.node_id == "scoutbbb2222222", why)
check("A 名下不再挂这台", phone.sn not in (reg.get_node("scoutaaa1111111").devices if reg.get_node("scoutaaa1111111") else {}), str(reg.get_node("scoutaaa1111111")))

print("== 资产计数 ==")
assets = ui_assets(reg)
check("2 个 Scout", assets.get("scout_count") == 2, str(assets.get("scout_count")))
check("设备不重复计数", assets.get("device_count") == 1, str(assets.get("device_count")))
check("有 Studio 行", assets.get("studio_count") >= 1, str(assets.get("studios")))

studios = {str(s.get("studio_id") or ""): s for s in (assets.get("studios") or [])}
studio_hits = [
    (sid, d)
    for sid, studio in studios.items()
    for d in (studio.get("devices") or [])
    if d.get("sn") == phone.sn
]
check("Studio 设备不重复", len(studio_hits) == 1, str(studio_hits))
if studio_hits:
    sid, dev = studio_hits[0]
    check("设备在当前 Studio B", sid == "studiobbb2222222", sid)
    check("Studio 设备账号仍是 A", dev.get("owner_user_id") == "usera", str(dev))
    check("Studio device_count", studios[sid].get("device_count") == 1, str(studios[sid].get("device_count")))
studio_a = studios.get("studioaaa1111111") or {}
check(
    "原 Studio 不再挂这台",
    phone.sn not in {d.get("sn") for d in (studio_a.get("devices") or [])},
    str(studio_a.get("devices")),
)

if failures:
    print(f"\nFAILED {len(failures)}: {', '.join(failures)}")
    sys.exit(1)
print("\nALL OK — device ownership")
