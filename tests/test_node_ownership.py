"""节点归属：REGISTER 记下 studio_id / owner；列表按账号过滤；离线仍列出。

    python tests/test_node_ownership.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="mino-node-own-")
os.environ["MINO_NEXUS_DATA_DIR"] = _TMP
os.environ["MINO_BOOTSTRAP_PASSWORD"] = "Mino@local"
os.environ["MINO_NEXUS_MDNS"] = "0"

from mino_nexus import auth_store as auth  # noqa: E402
from mino_nexus import node_registry as nr  # noqa: E402
from mino_nexus import node_store  # noqa: E402
from mino_nexus import protocol as P  # noqa: E402
from mino_nexus.node_registry import NodeRegistry  # noqa: E402
from mino_nexus.runtime_tokens import issue, peek_token  # noqa: E402
from mino_nexus.ui_devices import ui_nodes  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}{' — ' + detail if detail else ''}")
        failures.append(name)


def _send(*_a, **_k):
    return None


def _reg(node_id: str, **kw) -> P.Register:
    return P.Register(
        node_id=node_id,
        token=str(kw.get("token") or "t"),
        platform="darwin",
        scout_version="0.1.0",
        hostname=str(kw.get("hostname") or "box-a"),
        studio_id=str(kw.get("studio_id") or ""),
        devices=[P.DeviceManifest(sn=f"web{node_id}", platform="web", channels={"playwright": "connected"})],
    )


print("== REGISTER 写入 studio_id 与 owner_user_id ==")
reg = NodeRegistry()
reg.register(
    _reg("aabbccddeeff0011", studio_id="a1b2c3d4e5f67890", hostname="mac-one"),
    _send,
    owner_user_id="aaa111",
)
live = reg.get_node("aabbccddeeff0011")
check("session studio_id", live is not None and live.studio_id == "a1b2c3d4e5f67890", str(getattr(live, "studio_id", None)))
check("session owner", live is not None and live.owner_user_id == "aaa111", str(getattr(live, "owner_user_id", None)))
check("session hostname", live is not None and live.hostname == "mac-one", str(getattr(live, "hostname", None)))
snap = node_store.get_node("aabbccddeeff0011") or {}
check("persist studio_id", snap.get("studio_id") == "a1b2c3d4e5f67890", str(snap))
check("persist owner", snap.get("owner_user_id") == "aaa111", str(snap))

print("== 断开后仍列出 ==")
reg.disconnect("aabbccddeeff0011")
check("live gone", reg.get_node("aabbccddeeff0011") is None)
rows = {n["node_id"]: n for n in ui_nodes(reg)}
offline = rows.get("aabbccddeeff0011") or {}
check("offline listed", bool(offline), str(rows.keys()))
check("status offline", offline.get("status") == "offline", str(offline))
check("studio kept", offline.get("studio_id") == "a1b2c3d4e5f67890", str(offline))

print("== 过滤：未归属仅管理员；其他人只见自己的 ==")
node_store.upsert("orphan0011223344", hostname="old", platform="linux", scout_version="0.0.1")
reg2 = NodeRegistry()
reg2.register(_reg("bbbbccccddddeeee", studio_id="ffffffffffffffff"), _send, owner_user_id="bbb222")
all_rows = ui_nodes(reg2)
admin = {"user_id": "adm1", "role": "admin"}
op = {"user_id": "bbb222", "role": "user"}
other = {"user_id": "ccc333", "role": "user"}
admin_view = node_store.filter_nodes(all_rows, admin)
op_view = node_store.filter_nodes(all_rows, op, studio_id="ffffffffffffffff")
other_view = node_store.filter_nodes(all_rows, other)
admin_ids = {n["node_id"] for n in admin_view}
op_ids = {n["node_id"] for n in op_view}
other_ids = {n["node_id"] for n in other_view}
check("admin sees orphan", "orphan0011223344" in admin_ids, str(admin_ids))
check("admin sees owned", "bbbbccccddddeeee" in admin_ids)
check("operator sees own", "bbbbccccddddeeee" in op_ids, str(op_ids))
check("operator hides orphan", "orphan0011223344" not in op_ids, str(op_ids))
check("other hides both", "bbbbccccddddeeee" not in other_ids and "orphan0011223344" not in other_ids, str(other_ids))
orphan_row = next(n for n in admin_view if n["node_id"] == "orphan0011223344")
check("orphan label 未归属", orphan_row.get("ownership") == "未归属", str(orphan_row.get("ownership")))

print("== install token 带 user_id ==")
tok = issue(user_id="ddd444")
info = peek_token(tok["token"])
check("peek user_id", (info or {}).get("user_id") == "ddd444", str(info))

print("== HTTP 列表按登录用户过滤 ==")
try:
    from fastapi.testclient import TestClient
except ImportError:
    print("  skip HTTP（fastapi 未安装）")
else:
    auth.ensure_seed_users()
    from mino_nexus.app import app

    nr._registry = None
    with TestClient(app) as client:
        login = client.post("/auth/login", json={"username": "admin", "password": "Mino@local"})
        token = ((login.json() or {}).get("data") or {}).get("token") or ""
        admin_uid = ((login.json() or {}).get("data") or {}).get("user_id") or ""
        check("admin login", bool(token), login.text[:160])
        studio = {"Authorization": f"Bearer {token}", "X-Mino-Client": "studio"}
        op_user = auth.create_local_user(username="qaop", password="Qaop@1234", name="操作员", role="user")
        op_login = client.post("/auth/login", json={"username": "qaop", "password": "Qaop@1234"})
        op_tok = ((op_login.json() or {}).get("data") or {}).get("token") or ""
        op_uid = ((op_login.json() or {}).get("data") or {}).get("user_id") or op_user.get("user_id") or ""
        op_h = {"Authorization": f"Bearer {op_tok}", "X-Mino-Client": "studio"}

        live_reg = nr.get_registry()
        live_reg.register(_reg("1122334455667788", studio_id="s1s1s1s1s1s1s1s1"), _send, owner_user_id=op_uid)
        live_reg.register(_reg("99aabbccddeeff00"), _send, owner_user_id="")
        live_reg.disconnect("99aabbccddeeff00")

        admin_list = client.get("/runtime/nodes", headers=studio)
        admin_data = (admin_list.json() or {}).get("data") or {}
        admin_nodes = {n["node_id"]: n for n in (admin_data.get("nodes") or [])}
        check("admin HTTP 200", admin_list.status_code == 200, admin_list.text[:160])
        check("admin sees operator node", "1122334455667788" in admin_nodes, str(admin_nodes.keys()))
        check("admin sees unowned offline", "99aabbccddeeff00" in admin_nodes, str(admin_nodes.keys()))
        check("unowned stays offline", (admin_nodes.get("99aabbccddeeff00") or {}).get("status") == "offline")

        op_list = client.get("/runtime/nodes", headers=op_h, params={"studio_id": "s1s1s1s1s1s1s1s1"})
        op_data = (op_list.json() or {}).get("data") or {}
        op_nodes = {n["node_id"]: n for n in (op_data.get("nodes") or [])}
        check("operator sees own", "1122334455667788" in op_nodes, str(op_nodes.keys()))
        check("operator hides unowned", "99aabbccddeeff00" not in op_nodes, str(op_nodes.keys()))
        check("count matches", op_data.get("count") == len(op_nodes), str(op_data.get("count")))

        start = client.post("/runtime/nodes/1122334455667788/command", json={"command": "start"}, headers=studio)
        check("start rejected", start.status_code == 400, start.text[:160])
        offline_cmd = client.post("/runtime/nodes/99aabbccddeeff00/command", json={"command": "stop"}, headers=studio)
        check("offline command 409", offline_cmd.status_code == 409, offline_cmd.text[:160])
        console_cmd = client.post(
            "/runtime/nodes/1122334455667788/command",
            json={"command": "stop"},
            headers={"Authorization": f"Bearer {token}", "X-Mino-Client": "console"},
        )
        check("console cannot command", console_cmd.status_code == 403, console_cmd.text[:160])

        sent: list = []

        async def fake_send(mtype, payload, timeout=30.0):
            sent.append((mtype, payload))
            return P.Result(run_id="", status=P.EventStatus.PASS, summary="已接受 stop")

        node = live_reg.get_node("1122334455667788")
        if node is not None:
            node.send = fake_send
        ok_cmd = client.post("/runtime/nodes/1122334455667788/command", json={"command": "stop"}, headers=op_h)
        check("operator stop 200", ok_cmd.status_code == 200, ok_cmd.text[:200])
        check("NODE_COMMAND sent", bool(sent) and sent[0][0] is P.MsgType.NODE_COMMAND, str(sent))
        check("command=stop", bool(sent) and sent[0][1].command == "stop", str(sent))

if failures:
    print(f"\nFAILED: {', '.join(failures)}")
    sys.exit(1)
print("\nALL OK — node ownership")
