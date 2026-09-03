"""锁屏密码：落盘可取，GET 给 UI 只回 configured。"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="mino-nexus-dev-secret-")
os.environ["MINO_NEXUS_DATA_DIR"] = _TMP

from mino_nexus.device_secrets import (  # noqa: E402
    get_lock_password,
    lock_password_public,
    set_lock_password,
)
from mino_nexus import protocol as P  # noqa: E402
from mino_nexus.node_registry import NodeRegistry  # noqa: E402
from mino_nexus.ui_devices import ui_devices  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}{' — ' + detail if detail else ''}")
        failures.append(name)


print("== lock password store ==")
public = set_lock_password("5fda2f6d", "123456")
check("set returns configured", public.get("password_configured") is True, str(public))
check("set does not echo secret", "password" not in public, str(public))
check("internal get still has it", get_lock_password("5fda2f6d") == "123456")
check("public flag only", lock_password_public("5fda2f6d") == {"password_configured": True})

cleared = set_lock_password("5fda2f6d", "")
check("clear drops configured", cleared.get("password_configured") is False, str(cleared))
check("clear empties store", get_lock_password("5fda2f6d") == "")

set_lock_password("5fda2f6d", "654321")

print("== ui_devices redacts ==")
reg = NodeRegistry()
reg.register(
    P.Register(
        node_id="box-1", token="t", platform="darwin", scout_version="0.1.0",
        executors=[P.ExecutorManifest(id="adb", available=True, provides=["tap"])],
        devices=[P.DeviceManifest(sn="5fda2f6d", platform="android", model="25102RKBEC",
                                  channels={"adb": "connected"})],
    ),
    send=lambda *_a, **_k: None,
)
rows = {d["sn"]: d for d in ui_devices(reg)}
phone = rows.get("5fda2f6d") or {}
check("row listed", bool(phone))
check("configured true", phone.get("password_configured") is True, str(phone))
check("no password field", not phone.get("password"), str(phone))

print()
if failures:
    print(f"FAILED {len(failures)}: {', '.join(failures)}")
    sys.exit(1)
print("ALL OK — device secrets")
