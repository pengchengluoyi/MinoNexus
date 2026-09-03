"""两个节点各自登记 web{scout_id}，不抢同一个全局槽。"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="mino-nexus-web-")
os.environ["MINO_NEXUS_DATA_DIR"] = _TMP

from mino_nexus import protocol as P  # noqa: E402
from mino_nexus.node_registry import NodeRegistry  # noqa: E402
from mino_nexus.runtime.run_context import (  # noqa: E402
    is_legacy_web_sn,
    is_per_node_web_sn,
    is_web_slot,
)

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}{' — ' + detail if detail else ''}")
        failures.append(name)


def _noop_send(*_a, **_k):
    return None


def _reg(node_id: str, *sns: str) -> P.Register:
    devices = [
        P.DeviceManifest(sn=sn, platform="web", channels={"playwright": "connected"})
        for sn in sns
    ]
    return P.Register(
        node_id=node_id, token="t", platform="darwin", scout_version="0.1.0",
        executors=[P.ExecutorManifest(id="playwright", available=True, provides=["ui_screenshot"])],
        devices=devices,
    )


print("== is_web_slot ==")
check("legacy", is_web_slot("web-local") is True)
check("legacy underscore", is_web_slot("web_local") is True)
check("legacy helper", is_legacy_web_sn("web-local") is True)
check("legacy helper underscore", is_legacy_web_sn("web_local") is True)
check("per-node", is_web_slot("web3f8a1c0e9b2d4f71") is True)
check("per-node helper", is_per_node_web_sn("web3f8a1c0e9b2d4f71") is True)
check("legacy not per-node", is_per_node_web_sn("web-local") is False)
check("underscore not per-node", is_per_node_web_sn("web_local") is False)
check("phone", is_web_slot("R5CT30xxxx") is False)
check("phone not legacy", is_legacy_web_sn("5fda2f6d") is False)

print("== two nodes each own a web slot ==")
reg = NodeRegistry()
a_id, b_id = "aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb"
a_sn, b_sn = f"web{a_id}", f"web{b_id}"
reg.register(_reg(a_id, a_sn), send=_noop_send)
reg.register(_reg(b_id, b_sn), send=_noop_send)
na, ra = reg.resolve(a_sn)
nb, rb = reg.resolve(b_sn)
check("A owns A web", na is not None and na.node_id == a_id, ra)
check("B owns B web", nb is not None and nb.node_id == b_id, rb)
check("no owner clash", na is not nb)
owners = {row["sn"]: row["node_id"] for row in reg.devices()}
check("owner map A", owners.get(a_sn) == a_id, str(owners))
check("owner map B", owners.get(b_sn) == b_id, str(owners))
check("no web-local leftover", "web-local" not in owners, str(owners))

print("== upgrade: this node drops its own web-local ==")
reg2 = NodeRegistry()
old = "cccccccccccccccc"
reg2.register(_reg(old, "web-local"), send=_noop_send)
check("old owner", reg2.resolve("web-local")[0] is not None and reg2.resolve("web-local")[0].node_id == old)
reg2.disconnect(old)
reg2.register(_reg(old, f"web{old}"), send=_noop_send)
sns = {row["sn"] for row in reg2.devices()}
check("new sn owned", reg2.resolve(f"web{old}")[0] is not None)
check("legacy dropped", "web-local" not in sns, str(sns))

print("== upgrade while still connected (re-REGISTER) ==")
reg3 = NodeRegistry()
nid = "dddddddddddddddd"
reg3.register(_reg(nid, "web-local"), send=_noop_send)
reg3.register(_reg(nid, f"web{nid}"), send=_noop_send)
node = reg3.get_node(nid)
check("session dropped web-local", node is not None and "web-local" not in node.devices, str(node.devices if node else {}))
check("session has new web", node is not None and f"web{nid}" in node.devices)

print("== do not steal another live node's web-local ==")
reg4 = NodeRegistry()
old_node, new_node = "eeeeeeeeeeeeeeee", "ffffffffffffffff"
reg4.register(_reg(old_node, "web-local"), send=_noop_send)
reg4.register(_reg(new_node, f"web{new_node}"), send=_noop_send)
legacy, why = reg4.resolve("web-local")
check("legacy still old node", legacy is not None and legacy.node_id == old_node, why)
fresh, why2 = reg4.resolve(f"web{new_node}")
check("new node has its own", fresh is not None and fresh.node_id == new_node, why2)

print("== dead owner leftover is purged ==")
reg5 = NodeRegistry()
dead = "deadnode00000001"
reg5.register(_reg(dead, "web-local"), send=_noop_send)
reg5.disconnect(dead)
sns = {row["sn"] for row in reg5.devices()}
check("gone after owner disconnect", "web-local" not in sns, str(sns))
check("not resolvable", reg5.resolve("web-local")[0] is None)

print("== listing purges leftover owned by a missing node ==")
reg6 = NodeRegistry()
reg6._owner["web-local"] = "ghost-old-hostname"
reg6._owner["web_local"] = "ghost-old-hostname"
sns = {row["sn"] for row in reg6.devices()}
check("web-local not listed", "web-local" not in sns, str(sns))
check("web_local not listed", "web_local" not in sns, str(sns))
check("owner map cleaned", "web-local" not in reg6._owner and "web_local" not in reg6._owner)

print()
if failures:
    print(f"FAILED {len(failures)}: {', '.join(failures)}")
    sys.exit(1)
print("ALL OK — web slots")
