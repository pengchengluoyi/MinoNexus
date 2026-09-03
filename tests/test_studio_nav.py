"""Studio 入口允许名单：默认切面 + Console 可写、Studio 不可写。

    python tests/test_studio_nav.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="mino-studio-nav-")
os.environ["MINO_NEXUS_DATA_DIR"] = _TMP
os.environ["MINO_BOOTSTRAP_PASSWORD"] = "Mino@local"
os.environ["MINO_NEXUS_MDNS"] = "0"

from mino_nexus import studio_nav  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}{' — ' + detail if detail else ''}")
        failures.append(name)


print("== store 默认切面 ==")
nav = studio_nav.public_nav()
ids = {e["id"] for e in nav["entries"]}
check("有 plugins 条目", "plugins" in ids, str(ids))
check("有 scout 条目", "scout" in ids, str(ids))
check("默认不含 plugins", "plugins" not in nav["allowed"], str(nav["allowed"]))
for entry in ("testing", "runtime", "scout", "keys", "dispatch", "knowledge"):
    check(f"默认含 {entry}", entry in nav["allowed"])

print("== HTTP bootstrap + 门禁 ==")
try:
    from fastapi.testclient import TestClient
except ImportError:
    print("  skip HTTP（fastapi 未安装）")
else:
    from mino_nexus.app import app

    with TestClient(app) as client:
        login = client.post("/auth/login", json={"username": "admin", "password": "Mino@local"})
        token = ((login.json() or {}).get("data") or {}).get("token") or ""
        check("login", bool(token), login.text[:160])
        studio = {"Authorization": f"Bearer {token}", "X-Mino-Client": "studio"}
        console = {"Authorization": f"Bearer {token}", "X-Mino-Client": "console"}

        boot = client.get("/me/bootstrap", headers=studio)
        data = (boot.json() or {}).get("data") or {}
        allowed = (data.get("studio_nav") or {}).get("allowed") or []
        check("bootstrap 带 studio_nav", isinstance(data.get("studio_nav"), dict), str(data.get("studio_nav"))[:200])
        check("bootstrap 默认无 plugins", "plugins" not in allowed, str(allowed))
        check("bootstrap 默认有 scout", "scout" in allowed, str(allowed))

        denied = client.put("/me/studio-nav", json={"allowed": ["testing", "plugins"]}, headers=studio)
        check("Studio 不能改 studio-nav", denied.status_code == 403, f"status={denied.status_code}")

        saved = client.put("/me/studio-nav", json={"allowed": ["testing", "plugins"]}, headers=console)
        check("Console 能改 studio-nav", saved.status_code == 200, saved.text[:200])
        got = ((saved.json() or {}).get("data") or {}).get("allowed") or []
        check("已写入 plugins", "plugins" in got, str(got))

        plugin_deny = client.put("/settings/plugins/zentao", json={"enabled": True}, headers=studio)
        check("Studio 不能写插件配置", plugin_deny.status_code == 403, f"status={plugin_deny.status_code}")

        plugin_ok = client.put("/settings/plugins/zentao", json={"enabled": True}, headers=console)
        check("Console 能写插件配置", plugin_ok.status_code == 200, plugin_ok.text[:200])

if failures:
    print(f"\nFAILED: {', '.join(failures)}")
    sys.exit(1)
print("\nALL OK — studio nav")
