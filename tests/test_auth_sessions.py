"""同一账号可以同时持有多份登录票据（Studio + Console 互不踢）。

    python tests/test_auth_sessions.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="mino-auth-sess-")
os.environ["MINO_NEXUS_DATA_DIR"] = _TMP
os.environ["MINO_BOOTSTRAP_PASSWORD"] = "Mino@local"

from mino_nexus import auth_store as auth  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}{' — ' + detail if detail else ''}")
        failures.append(name)


print("== 同一账号多次登录 ==")
a = auth.login(username="admin", password="Mino@local")
b = auth.login(username="admin", password="Mino@local")
check("两次都发了 token", bool(a.get("token") and b.get("token")))
check("token 不同", a.get("token") != b.get("token"), str(a.get("token"))[:12])
check("第一份仍有效", bool(auth.session_of(a["token"])))
check("第二份有效", bool(auth.session_of(b["token"])))
check("status A logged_in", auth.status(a["token"]).get("logged_in") is True)
check("status B logged_in", auth.status(b["token"]).get("logged_in") is True)

auth.logout(a["token"])
check("退出 A 不影响 B", bool(auth.session_of(b["token"])))
check("A 已失效", auth.session_of(a["token"]) is None)

if failures:
    print(f"\nFAILED {len(failures)}: {', '.join(failures)}")
    sys.exit(1)
print("\nALL OK — auth sessions")
