"""项目 / 应用归属：创建人写入、列表透出、另一用户不冒领。

    python tests/test_project_ownership.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="mino-ownership-")
os.environ["MINO_NEXUS_DATA_DIR"] = _TMP
os.environ["MINO_BOOTSTRAP_PASSWORD"] = "Mino@local"
os.environ["MINO_NEXUS_MDNS"] = "0"

from mino_nexus import auth_store as auth  # noqa: E402
from mino_nexus import project_store as ps  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}{' — ' + detail if detail else ''}")
        failures.append(name)


print("== store：创建写入 created_by，缺省回填 admin ==")
auth.ensure_seed_users()
admin = auth.seed_admin_user()
check("种子 admin 存在", bool(admin.get("user_id")), str(admin))

orphan = {
    "id": "p-imported",
    "uid": "p-imported",
    "name": "导入项目",
    "description": "",
    "env": {},
}
root = ps._root()
root["projects"].append(orphan)
ps._save(root)
again = ps.find_project("p-imported")
# _root 再读会回填
listed = {p["id"]: p for p in ps.list_projects()}
imp = listed.get("p-imported") or {}
check("导入项目回填 created_by", bool(imp.get("created_by")), str(imp))
check("导入项目回填名是 admin", imp.get("created_by") == admin.get("user_id"), str(imp.get("created_by")))
check("导入项目显示名", bool(imp.get("created_by_name")), str(imp.get("created_by_name")))

print("== store：user A 创建，user B 的 payload 不冒领 ==")
user_a = {"user_id": "aaa111", "name": "甲", "username": "jia"}
user_b = {"user_id": "bbb222", "name": "乙", "username": "yi"}
proj = ps.create_project("甲的项目", "desc", owner=user_a)
app = ps.create_app(proj["id"], name="甲的应用", platforms="android", owner=user_a)
check("项目 created_by=A", proj.get("created_by") == "aaa111", str(proj))
check("项目 created_by_name=甲", proj.get("created_by_name") == "甲", str(proj.get("created_by_name")))
check("应用 created_by=A", app.get("created_by") == "aaa111", str(app))

rows = ps.list_projects()
hit = next((p for p in rows if p.get("id") == proj["id"]), {})
check("列表带创建人", hit.get("created_by") == "aaa111" and hit.get("created_by_name") == "甲", str(hit))
app_hit = next((a for a in (hit.get("apps") or []) if a.get("id") == app["id"]), {})
check("应用列表带创建人", app_hit.get("created_by") == "aaa111", str(app_hit))
check("B 不是创建人", hit.get("created_by") != user_b["user_id"], str(hit.get("created_by")))

detail = ps.get_app_detail(app["id"])
check("详情 created_by=A", detail.get("created_by") == "aaa111", str(detail.get("created_by")))
check("详情不写 B", detail.get("created_by") != user_b["user_id"])

print("== HTTP：A 创建，B 登录后列表仍是 A ==")
try:
    from fastapi.testclient import TestClient
except ImportError:
    print("  skip HTTP（fastapi 未安装）")
else:
    from mino_nexus.app import app as fastapi_app

    with TestClient(fastapi_app) as client:
        login_a = client.post("/auth/login", json={"username": "admin", "password": "Mino@local"})
        token_a = ((login_a.json() or {}).get("data") or {}).get("token") or ""
        uid_a = ((login_a.json() or {}).get("data") or {}).get("user_id") or ""
        check("admin 登录", bool(token_a), login_a.text[:160])
        auth_a = {"Authorization": f"Bearer {token_a}", "X-Mino-Client": "studio"}

        created = client.post("/project/create", json={"name": "A项目", "description": ""}, headers=auth_a)
        check("A 创建项目", created.status_code == 200 and created.json().get("created_by") == uid_a, created.text[:200])
        pid = (created.json() or {}).get("id") or ""

        created_app = client.post(
            "/project/app/create",
            json={"project_id": pid, "name": "A应用", "platforms": "android", "env": {}},
            headers=auth_a,
        )
        check(
            "A 创建应用",
            created_app.status_code == 200 and created_app.json().get("created_by") == uid_a,
            created_app.text[:200],
        )

        made = client.post(
            "/auth/users",
            json={"username": "otherqa", "password": "Password1", "name": "乙"},
            headers={"Authorization": f"Bearer {token_a}", "X-Mino-Client": "console"},
        )
        check("Console 建乙", made.status_code == 200, made.text[:160])

        login_b = client.post("/auth/login", json={"username": "otherqa", "password": "Password1"})
        token_b = ((login_b.json() or {}).get("data") or {}).get("token") or ""
        uid_b = ((login_b.json() or {}).get("data") or {}).get("user_id") or ""
        check("乙登录", bool(token_b) and uid_b and uid_b != uid_a, login_b.text[:160])
        auth_b = {"Authorization": f"Bearer {token_b}", "X-Mino-Client": "studio"}

        listed = client.get("/project/list", headers=auth_b)
        rows = listed.json() if listed.status_code == 200 else []
        row = next((p for p in rows if isinstance(p, dict) and p.get("id") == pid), {})
        check("乙看见的创建人仍是 A", row.get("created_by") == uid_a, str(row)[:200])
        check("乙不冒领", row.get("created_by") != uid_b, str(row.get("created_by")))
        check("乙显示名不是自己", row.get("created_by_name") != "乙", str(row.get("created_by_name")))

        b_proj = client.post("/project/create", json={"name": "B项目", "description": ""}, headers=auth_b)
        check("B 创建项目归属 B", b_proj.status_code == 200 and b_proj.json().get("created_by") == uid_b, b_proj.text[:200])

print("== studio_nav 默认不含 plugins ==")
from mino_nexus import studio_nav  # noqa: E402

nav = studio_nav.public_nav()
check("默认允许测试", "testing" in nav["allowed"], str(nav["allowed"]))
check("默认不含插件配置", "plugins" not in nav["allowed"], str(nav["allowed"]))
studio_nav.save_allowed(["testing", "plugins"])
check("Console 可打开 plugins", "plugins" in studio_nav.get_allowed())
studio_nav.save_allowed(list(studio_nav.DEFAULT_ALLOWED))

if failures:
    print(f"\nFAILED: {', '.join(failures)}")
    sys.exit(1)
print("\nALL OK — project ownership")
