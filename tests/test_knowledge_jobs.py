"""沉淀知识 / 知识机审：方法契约 + 按应用登录账号隔离。

    python tests/test_knowledge_jobs.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="mino-knowledge-jobs-")
os.environ["MINO_NEXUS_DATA_DIR"] = _TMP
os.environ["MINO_BOOTSTRAP_PASSWORD"] = "Mino@local"
os.environ["MINO_NEXUS_MDNS"] = "0"

from mino_nexus import knowledge_jobs_store as kjs  # noqa: E402
from mino_nexus import settings_store as ss  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}{' — ' + detail if detail else ''}")
        failures.append(name)


print("== store：开关按应用登录账号隔离，不跟 Mino 管理员走 ==")
try:
    kjs.save_knowledge_job_settings(capture_enabled=False, review_enabled=True)
    check("无账号写入应失败", False)
except ValueError as exc:
    check("无账号写入报错", "应用登录账号" in str(exc), str(exc))

kjs.save_knowledge_job_settings(
    account_id="acc-a",
    account_ident="17633312000",
    capture_enabled=False,
    review_enabled=True,
)
kjs.save_knowledge_job_settings(
    account_id="acc-b",
    account_ident="pre_test_546@mathmagical.com",
    capture_enabled=True,
    review_enabled=False,
)
a = kjs.get_knowledge_job_settings(account_id="acc-a")
b = kjs.get_knowledge_job_settings(account_id="acc-b")
check("A 关沉淀", a["capture_enabled"] is False, str(a))
check("A 开机审", a["review_enabled"] is True, str(a))
check("B 开沉淀", b["capture_enabled"] is True, str(b))
check("B 关机审", b["review_enabled"] is False, str(b))
check("A 的 ident 留下", a["account_ident"] == "17633312000", str(a))
listed = kjs.list_knowledge_job_settings()
check("列表有两条", len(listed) == 2, str(listed))
check("helper 跟账号走", kjs.knowledge_capture_enabled(account_id="acc-a") is False)
check("另一账号 helper 独立", kjs.knowledge_review_enabled(account_id="acc-b") is False)

print("== store：旧全局 knowledge_jobs 当缺省，不删导入知识 ==")
root = ss._root()
root["knowledge"] = [
    {"id": "legacy-1", "title": "导入知识", "content": "旧条目", "app_ids": ["app-1"]},
    {"id": "acc-only", "title": "账号知识", "content": "新条目", "account_id": "acc-a", "account_ident": "17633312000"},
]
prev_jobs = root.get("knowledge_jobs") if isinstance(root.get("knowledge_jobs"), dict) else {}
root["knowledge_jobs"] = {
    "capture_enabled": False,
    "review_enabled": False,
    "accounts": prev_jobs.get("accounts") or {},
}
ss._save(root)
unscoped = kjs.get_knowledge_job_settings(account_id="acc-new")
check("未写过的账号沿用旧缺省", unscoped["capture_enabled"] is False and unscoped["review_enabled"] is False, str(unscoped))
legacy_items = ss.list_testing_knowledge(account_id="acc-a")
check("无账号旧条目仍可见", any(x["id"] == "legacy-1" for x in legacy_items), str([x["id"] for x in legacy_items]))
check("本账号条目可见", any(x["id"] == "acc-only" for x in legacy_items), str([x["id"] for x in legacy_items]))
other_items = ss.list_testing_knowledge(account_id="acc-b")
check("其它账号看不到别人的条目", all(x["id"] != "acc-only" for x in other_items), str([x["id"] for x in other_items]))
check("其它账号仍看得到导入知识", any(x["id"] == "legacy-1" for x in other_items))

print("== HTTP：PUT/POST/PATCH 都收，按号池账号落盘 ==")
try:
    from fastapi.testclient import TestClient
except ImportError:
    print("  skip HTTP（fastapi 未安装）")
else:
    from mino_nexus.app import app

    with TestClient(app) as client:
        login = client.post("/auth/login", json={"username": "admin", "password": "Mino@local"})
        token = ((login.json() or {}).get("data") or {}).get("token") or ""
        auth = {"Authorization": f"Bearer {token}", "X-Mino-Client": "studio"}
        check("login", bool(token), login.text[:160])

        created_p = client.post("/project/create", json={"name": "造好物", "description": ""}, headers=auth)
        pid = (created_p.json() or {}).get("id") or ""
        created_a = client.post(
            "/project/app/create",
            json={"project_id": pid, "name": "造物相机", "platforms": "android", "env": {}},
            headers=auth,
        )
        aid = (created_a.json() or {}).get("id") or ""
        acc = client.put(
            f"/project/{pid}/accounts",
            json={"accounts": [
                {"phone": "17633312000", "env": "test", "password": "888888"},
                {"email": "pre_test_546@mathmagical.com", "env": "prod", "password": "123456"},
            ]},
            headers=auth,
        )
        rows = ((acc.json() or {}).get("data") or {}).get("accounts") or []
        check("号池写入", len(rows) == 2, acc.text[:200])
        acc_phone = next((x for x in rows if x.get("phone") == "17633312000"), {})
        acc_mail = next((x for x in rows if x.get("email") == "pre_test_546@mathmagical.com"), {})

        missing = client.put(
            "/settings/knowledge/jobs",
            json={"capture_enabled": False, "review_enabled": False},
            headers=auth,
        )
        check("无账号 PUT 400", missing.status_code == 400, f"status={missing.status_code} {missing.text[:160]}")

        ghost = client.put(
            "/settings/knowledge/jobs",
            json={"account_id": "not-a-real-account", "capture_enabled": False, "review_enabled": True},
            headers=auth,
        )
        check("假账号 PUT 400", ghost.status_code == 400, f"status={ghost.status_code} {ghost.text[:160]}")

        put = client.put(
            "/settings/knowledge/jobs",
            json={
                "account_id": acc_phone.get("id"),
                "project_id": pid,
                "app_id": aid,
                "capture_enabled": False,
                "review_enabled": True,
            },
            headers=auth,
        )
        check("PUT /settings/knowledge/jobs 200", put.status_code == 200 and put.json().get("code") == 200, put.text[:200])
        pdata = (put.json() or {}).get("data") or {}
        check("PUT 记下手机号账号", pdata.get("account_id") == acc_phone.get("id"), str(pdata))
        check("PUT 关沉淀", pdata.get("capture_enabled") is False, str(pdata))
        check("响应不是 Mino user_id", "user_id" not in pdata, str(pdata))

        post = client.post(
            "/settings/knowledge/jobs",
            json={
                "account_id": acc_mail.get("id"),
                "project_id": pid,
                "capture_enabled": True,
                "review_enabled": False,
            },
            headers=auth,
        )
        check("POST /settings/knowledge/jobs 不是 405", post.status_code != 405, f"status={post.status_code}")
        check("POST /settings/knowledge/jobs 200", post.status_code == 200, post.text[:200])
        mdata = (post.json() or {}).get("data") or {}
        check("POST 记下邮箱账号", mdata.get("account_id") == acc_mail.get("id"), str(mdata))
        check("两账号互不影响", mdata.get("review_enabled") is False and pdata.get("review_enabled") is True)

        got = client.get(
            "/settings/knowledge/jobs",
            params={"account_id": acc_phone.get("id"), "project_id": pid},
            headers=auth,
        )
        check("GET 读回手机号账号", got.status_code == 200 and ((got.json() or {}).get("data") or {}).get("capture_enabled") is False, got.text[:200])

        patch = client.patch(
            "/settings/knowledge/jobs",
            json={"account_id": acc_phone.get("id"), "project_id": pid, "capture_enabled": True, "review_enabled": True},
            headers=auth,
        )
        check("PATCH 不是 405", patch.status_code != 405, f"status={patch.status_code}")
        check("PATCH 200", patch.status_code == 200, patch.text[:200])

        auto = client.post("/settings/knowledge/auto-review", json={"app_id": aid, "account_id": acc_phone.get("id")}, headers=auth)
        check("POST auto-review 不是 405", auto.status_code != 405, f"status={auto.status_code} {auto.text[:160]}")
        check("POST auto-review 200", auto.status_code == 200, auto.text[:200])

        fail = client.post("/settings/knowledge/analyze-failure", json={"app_id": aid, "account_id": acc_phone.get("id")}, headers=auth)
        check("POST analyze-failure 不是 405", fail.status_code != 405, f"status={fail.status_code} {fail.text[:160]}")
        check("POST analyze-failure 200", fail.status_code == 200, fail.text[:200])

print()
if failures:
    print(f"FAILED {len(failures)}: {', '.join(failures)}")
    sys.exit(1)
print("ALL OK — knowledge jobs methods + app-account key")
