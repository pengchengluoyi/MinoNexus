"""UI HTTP 契约：登录、节点、Scout 安装凭证。需要已安装本仓依赖。"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

tmpdir = tempfile.mkdtemp(prefix="mino-nexus-http-")
os.environ["MINO_NEXUS_DATA_DIR"] = tmpdir
os.environ["MINO_BOOTSTRAP_PASSWORD"] = "Mino@local"
os.environ["MINO_NEXUS_MDNS"] = "0"


def fail(msg: str) -> int:
    print(f"FAIL check_http_contract\n  {msg}")
    return 1


def main() -> int:
    try:
        from fastapi.testclient import TestClient
    except ImportError:
        return fail("fastapi 未安装。先 `pip install -e .` 或 `uv sync`")

    from mino_nexus.app import app

    hits: list[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        if not cond:
            hits.append(f"{name}: {detail}" if detail else name)

    with TestClient(app) as client:
        r = client.get("/sys/server_info")
        check("GET /sys/server_info", r.status_code == 200 and r.json().get("code") == 200, str(r.text)[:200])
        check("CORS", "MinoNexus" in str(r.json().get("data")))
        info = (r.json() or {}).get("data") or {}
        check("lan_host mino.local", info.get("lan_host") == "mino.local", str(info.get("lan_host")))
        check("http_url mino.local", info.get("http_url") == "http://mino.local:10104", str(info.get("http_url")))
        check("node_ws_url mino.local", info.get("node_ws_url") == "ws://mino.local:10104/node", str(info.get("node_ws_url")))

        origin = "http://127.0.0.1:5174"
        opt = client.options(
            "/auth/status",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization,x-mino-client",
            },
        )
        check("OPTIONS CORS", opt.status_code in (200, 204), f"status={opt.status_code}")
        check(
            "CORS allow origin",
            opt.headers.get("access-control-allow-origin") in ("*", origin),
            dict(opt.headers),
        )

        st = client.get("/auth/status")
        check("GET /auth/status", st.status_code == 200, st.text[:200])
        data = (st.json() or {}).get("data") or {}
        check("needs_setup true until an email user exists", data.get("needs_setup") is True, str(data))
        check("logged_in false", data.get("logged_in") is False, str(data))

        bad = client.post("/auth/login", json={"username": "admin", "password": "wrong-password-here"})
        check("bad password 401", bad.status_code == 401, f"status={bad.status_code} body={bad.text[:160]}")

        login = client.post("/auth/login", json={"username": "admin", "password": "Mino@local"})
        check("POST /auth/login", login.status_code == 200 and login.json().get("code") == 200, login.text[:200])
        token = ((login.json() or {}).get("data") or {}).get("token") or ""
        ws_token = ((login.json() or {}).get("data") or {}).get("ws_token") or ""
        check("login token", bool(token), str(login.json())[:200])
        check("login ws_token", bool(ws_token), str(login.json())[:200])

        auth = {"Authorization": f"Bearer {token}"}
        me = client.get("/me/bootstrap", headers={**auth, "X-Mino-Client": "studio"})
        check("GET /me/bootstrap", me.status_code == 200, me.text[:200])
        boot = (me.json() or {}).get("data") or {}
        check("bootstrap logged_in", boot.get("logged_in") is True, str(boot)[:200])
        check("bootstrap role", boot.get("role") == "admin", str(boot.get("role")))
        studio_nav = boot.get("studio_nav") or {}
        check("bootstrap studio_nav", isinstance(studio_nav.get("entries"), list) and isinstance(studio_nav.get("allowed"), list), str(studio_nav)[:200])
        check("bootstrap default hides plugins", "plugins" not in (studio_nav.get("allowed") or []), str(studio_nav.get("allowed")))

        rt = client.get("/sys/runtime", headers=auth)
        rtd = (rt.json() or {}).get("data") or {}
        check("GET /sys/runtime", rt.status_code == 200 and "endpoints" in rtd, rt.text[:200])
        check("runtime lan_host", rtd.get("lan_host") == "mino.local", str(rtd.get("lan_host")))
        ep0 = (rtd.get("endpoints") or [{}])[0]
        check("runtime endpoint host", "mino.local" in str(ep0.get("url") or ""), str(ep0))

        nodes = client.get("/runtime/nodes", headers={**auth, "X-Mino-Client": "console"})
        check("GET /runtime/nodes", nodes.status_code == 200, nodes.text[:200])
        nd = (nodes.json() or {}).get("data") or {}
        check("nodes list", isinstance(nd.get("nodes"), list), str(nd)[:200])

        # MINO_SCOUT_MANIFEST_URL unset → 404. Packages are not stored on this server.
        rel = client.get("/releases/scout/latest", params={"os": "darwin", "arch": "arm64"}, headers=auth)
        check("GET /releases/scout/latest 404", rel.status_code == 404, f"status={rel.status_code} {rel.text[:160]}")

        from mino_nexus.routers.rReleases import pick_item
        multi = {
            "version": "0.1.0",
            "items": [
                {"os": "darwin", "arch": "arm64", "url": "https://example.com/a.zip", "sha256": "abc", "installer": "zip", "filename": "a.zip"},
                {"os": "win32", "arch": "x64", "url": "https://example.com/b.zip", "sha256": "def", "installer": "zip", "filename": "b.zip"},
            ],
        }
        picked = pick_item(multi, "darwin", "arm64")
        check("pick items[] darwin-arm64", bool(picked and picked.get("url", "").endswith("a.zip")), str(picked))
        single = {"version": "1.0.0", "url": "https://example.com/one.zip", "sha256": "x", "os": "win32", "arch": "x64", "installer": "zip"}
        check("pick single-entry", bool(pick_item(single, "win32", "x64")), str(pick_item(single, "win32", "x64")))
        check("pick single-entry wrong os", pick_item(single, "darwin", "arm64") is None)

        tok = client.post("/runtime/nodes/install-token", headers={**auth, "X-Mino-Client": "studio"})
        check("POST install-token studio", tok.status_code == 200, tok.text[:200])
        issued = ((tok.json() or {}).get("data") or {}).get("token") or ""
        check("install token value", bool(issued), tok.text[:200])

        denied = client.post("/runtime/nodes/install-token", headers={**auth, "X-Mino-Client": "console"})
        check("console cannot install-token", denied.status_code == 403, f"status={denied.status_code}")

        studio_users = client.post(
            "/auth/users",
            json={"username": "qa1", "password": "Password1", "name": "QA"},
            headers={**auth, "X-Mino-Client": "studio"},
        )
        check("studio cannot create users", studio_users.status_code == 403, f"status={studio_users.status_code}")

        created = client.post(
            "/auth/users",
            json={"username": "qa1", "password": "Password1", "name": "QA"},
            headers={**auth, "X-Mino-Client": "console"},
        )
        check("console can create users", created.status_code == 200, created.text[:200])

        mail = client.get("/settings/mail", headers={**auth, "X-Mino-Client": "console"})
        check("GET /settings/mail", mail.status_code == 200, mail.text[:200])

        studio_mail = client.put(
            "/settings/mail",
            json={"host": "smtp.example.com", "port": 587, "username": "a@b.c", "password": "x", "from_email": "a@b.c"},
            headers={**auth, "X-Mino-Client": "studio"},
        )
        check("studio cannot write mail", studio_mail.status_code == 403, f"status={studio_mail.status_code}")

        keys = client.get("/settings/ai/providers", headers={**auth, "X-Mino-Client": "studio"})
        check("GET /settings/ai/providers", keys.status_code == 200, keys.text[:200])
        providers = ((keys.json() or {}).get("data") or {}).get("providers") or []
        check("provider presets", len(providers) >= 3, str(len(providers)))

        console_key = client.put(
            "/settings/ai/providers/openai",
            json={"api_key": "sk-test", "enabled": True},
            headers={**auth, "X-Mino-Client": "console"},
        )
        check("console cannot write model key", console_key.status_code == 403, f"status={console_key.status_code}")

        kinds = client.get("/packs/kinds", headers={**auth, "X-Mino-Client": "console"})
        check("GET /packs/kinds", kinds.status_code == 200, kinds.text[:200])
        kind_rows = ((kinds.json() or {}).get("data") or {}).get("kinds") or []
        check("pack kinds nonempty", len(kind_rows) >= 4, str(kind_rows)[:200])

        plugins = client.get("/settings/plugins", headers={**auth, "X-Mino-Client": "studio"})
        check("GET /settings/plugins", plugins.status_code == 200, plugins.text[:200])
        plugin_rows = ((plugins.json() or {}).get("data") or {}).get("plugins") or []
        check("plugins catalog nonempty", len(plugin_rows) >= 7, str([p.get("id") for p in plugin_rows])[:200])
        cats = ((plugins.json() or {}).get("data") or {}).get("categories") or []
        check("plugin categories nonempty", len(cats) >= 4, str(cats)[:200])

        studio_plugin = client.put(
            "/settings/plugins/zentao",
            json={"enabled": True},
            headers={**auth, "X-Mino-Client": "studio"},
        )
        check("studio cannot write plugin config", studio_plugin.status_code == 403, f"status={studio_plugin.status_code}")

        console_plugin = client.put(
            "/settings/plugins/zentao",
            json={"enabled": True},
            headers={**auth, "X-Mino-Client": "console"},
        )
        check("console can write plugin config", console_plugin.status_code == 200, console_plugin.text[:200])

        studio_nav_put = client.put(
            "/me/studio-nav",
            json={"allowed": ["testing", "plugins"]},
            headers={**auth, "X-Mino-Client": "studio"},
        )
        check("studio cannot write studio-nav", studio_nav_put.status_code == 403, f"status={studio_nav_put.status_code}")

        console_nav = client.put(
            "/me/studio-nav",
            json={"allowed": ["testing", "runtime", "keys", "dispatch", "knowledge"]},
            headers={**auth, "X-Mino-Client": "console"},
        )
        check("console can write studio-nav", console_nav.status_code == 200, console_nav.text[:200])

        dispatch = client.get("/settings/dispatch", headers={**auth, "X-Mino-Client": "studio"})
        check("GET /settings/dispatch", dispatch.status_code == 200, dispatch.text[:200])
        calls = ((dispatch.json() or {}).get("data") or {}).get("calls")
        check("dispatch calls is list", isinstance(calls, list), dispatch.text[:200])

        devices = client.get("/device/list", headers={**auth, "X-Mino-Client": "studio"})
        check("GET /device/list", devices.status_code == 200 and isinstance(devices.json(), list), devices.text[:200])

        projects = client.get("/project/list", headers={**auth, "X-Mino-Client": "studio"})
        check("GET /project/list", projects.status_code == 200 and projects.json() == [], projects.text[:200])

        denied_proj = client.post(
            "/project/create",
            json={"name": "Demo", "description": "from console"},
            headers={**auth, "X-Mino-Client": "console"},
        )
        check("console cannot create project", denied_proj.status_code == 403, f"status={denied_proj.status_code}")

        created_p = client.post(
            "/project/create",
            json={"name": "Demo", "description": "studio"},
            headers={**auth, "X-Mino-Client": "studio"},
        )
        check("POST /project/create", created_p.status_code == 200 and created_p.json().get("id"), created_p.text[:200])
        check("created project has created_by", bool(created_p.json().get("created_by")), str(created_p.json())[:200])
        pid = (created_p.json() or {}).get("id") or ""

        created_a = client.post(
            "/project/app/create",
            json={"project_id": pid, "name": "AppA", "platforms": "android", "env": {}},
            headers={**auth, "X-Mino-Client": "studio"},
        )
        check("POST /project/app/create", created_a.status_code == 200 and created_a.json().get("id"), created_a.text[:200])
        aid = (created_a.json() or {}).get("id") or ""

        listed = client.get("/project/list", headers={**auth, "X-Mino-Client": "studio"})
        rows = listed.json() if listed.status_code == 200 else []
        check("list has project", isinstance(rows, list) and any(p.get("id") == pid for p in rows), str(rows)[:200])

        cfg = client.get(f"/app-automation/config/{aid}", headers={**auth, "X-Mino-Client": "studio"})
        check("GET /app-automation/config", cfg.status_code == 200 and cfg.json().get("code") == 200, cfg.text[:200])

        saved = client.put(
            f"/app-automation/config/{aid}",
            json={"qa_process": {"requirements": [{"id": "req-1", "title": "登录", "draft_cases": [{"case_id": "c1", "name": "打开"}]}]}},
            headers={**auth, "X-Mino-Client": "studio"},
        )
        check("PUT /app-automation/config", saved.status_code == 200, saved.text[:200])

        cases = client.get(f"/app-automation/cases/{aid}", headers={**auth, "X-Mino-Client": "studio"})
        case_rows = ((cases.json() or {}).get("data") or {}).get("cases") or []
        check("GET /app-automation/cases", cases.status_code == 200 and len(case_rows) == 1, cases.text[:200])

        summary = client.get("/app-automation/qa-process/summary", headers={**auth, "X-Mino-Client": "studio"})
        items = ((summary.json() or {}).get("data") or {}).get("items") or []
        check("GET qa-process/summary", summary.status_code == 200 and len(items) == 1, summary.text[:200])

        env_get = client.get(f"/project/{pid}/env", headers={**auth, "X-Mino-Client": "studio"})
        check("GET /project/env", env_get.status_code == 200 and env_get.json().get("code") == 200, env_get.text[:200])

        acc = client.put(
            f"/project/{pid}/accounts",
            json={"accounts": [{"phone": "13800000000", "env": "test", "password": "p"}]},
            headers={**auth, "X-Mino-Client": "studio"},
        )
        check("PUT /project/accounts", acc.status_code == 200, acc.text[:200])
        acc_rows = ((acc.json() or {}).get("data") or {}).get("accounts") or []
        acc_id = (acc_rows[0] or {}).get("id") if acc_rows else ""

        jobs_get = client.get("/settings/knowledge/jobs", headers={**auth, "X-Mino-Client": "studio"})
        check("GET /settings/knowledge/jobs", jobs_get.status_code == 200, jobs_get.text[:200])

        jobs_no_acc = client.put(
            "/settings/knowledge/jobs",
            json={"capture_enabled": False, "review_enabled": True},
            headers={**auth, "X-Mino-Client": "studio"},
        )
        check("PUT knowledge/jobs without account 400", jobs_no_acc.status_code == 400, f"status={jobs_no_acc.status_code}")

        jobs_put = client.put(
            "/settings/knowledge/jobs",
            json={"account_id": acc_id, "project_id": pid, "capture_enabled": False, "review_enabled": True},
            headers={**auth, "X-Mino-Client": "studio"},
        )
        check("PUT /settings/knowledge/jobs", jobs_put.status_code == 200, jobs_put.text[:200])
        jobs_data = (jobs_put.json() or {}).get("data") or {}
        check("knowledge jobs keyed by account", jobs_data.get("account_id") == acc_id, str(jobs_data)[:200])

        jobs_post = client.post(
            "/settings/knowledge/jobs",
            json={"account_id": acc_id, "project_id": pid, "capture_enabled": True, "review_enabled": False},
            headers={**auth, "X-Mino-Client": "studio"},
        )
        check("POST /settings/knowledge/jobs not 405", jobs_post.status_code != 405, f"status={jobs_post.status_code}")
        check("POST /settings/knowledge/jobs 200", jobs_post.status_code == 200, jobs_post.text[:200])

        auto = client.post(
            "/settings/knowledge/auto-review",
            json={"app_id": aid, "account_id": acc_id},
            headers={**auth, "X-Mino-Client": "studio"},
        )
        check("POST /settings/knowledge/auto-review not 405", auto.status_code != 405, f"status={auto.status_code}")
        check("POST /settings/knowledge/auto-review 200", auto.status_code == 200, auto.text[:200])

        tasks = client.get("/task/list", headers={**auth, "X-Mino-Client": "studio"})
        check("GET /task/list", tasks.status_code == 200 and tasks.json() == [], tasks.text[:200])

        runs = client.get("/case-runner/runs", headers={**auth, "X-Mino-Client": "studio"})
        check("GET /case-runner/runs", runs.status_code == 200, runs.text[:200])

        still_501 = client.post("/case-runner/run", json={}, headers={**auth, "X-Mino-Client": "studio"})
        check(
            "POST /case-runner/run not 501",
            still_501.status_code != 501,
            f"status={still_501.status_code} {still_501.text[:160]}",
        )
        check(
            "POST /case-runner/run empty body 400",
            still_501.status_code == 400,
            f"status={still_501.status_code} {still_501.text[:160]}",
        )

        started = client.post(
            "/case-runner/run",
            json={"app_id": aid, "case_ids": ["c1"], "async_exec": True},
            headers={**auth, "X-Mino-Client": "studio"},
        )
        check(
            "POST /case-runner/run starts",
            started.status_code == 200 and started.json().get("code") == 200,
            f"status={started.status_code} {started.text[:240]}",
        )
        run_data = ((started.json() or {}).get("data") or {})
        run_id = run_data.get("run_id") or run_data.get("task_id") or ""
        check("POST /case-runner/run returns run_id", bool(run_id), str(run_data)[:240])
        check(
            "run failed without device/key (not silent)",
            run_data.get("status") in ("failed", "running", "done") and (
                run_data.get("status") != "failed" or bool(run_data.get("error"))
            ),
            str(run_data)[:240],
        )
        if run_id:
            polled = client.get(f"/case-runner/run/{run_id}", headers={**auth, "X-Mino-Client": "studio"})
            check("GET /case-runner/run/{id}", polled.status_code == 200, polled.text[:200])
            cr_tasks = client.get("/case-runner/tasks", headers={**auth, "X-Mino-Client": "studio"})
            check("GET /case-runner/tasks", cr_tasks.status_code == 200, cr_tasks.text[:200])
            task_items = ((cr_tasks.json() or {}).get("data") or {}).get("items") or []
            check(
                "case-runner tasks include the run",
                any(t.get("run_id") == run_id or t.get("task_id") == run_id for t in task_items),
                str(task_items)[:240],
            )

        roles = client.get("/settings/ai/roles", headers={**auth, "X-Mino-Client": "console"})
        role_data = (roles.json() or {}).get("data") or {}
        check("GET /settings/ai/roles", roles.status_code == 200, roles.text[:200])
        check("roles catalog nonempty", len(role_data.get("product") or []) >= 3, str(role_data.get("counts"))[:200])

        prompt_put = client.put(
            "/settings/ai/roles/im-qa-assistant/prompt",
            json={"system_prompt": "你是 Mino 的 IM 总指挥。用中文简短回答。"},
            headers={**auth, "X-Mino-Client": "console"},
        )
        check(
            "PUT role prompt not 501",
            prompt_put.status_code != 501,
            f"status={prompt_put.status_code} {prompt_put.text[:200]}",
        )
        check("PUT role prompt 200", prompt_put.status_code == 200, prompt_put.text[:200])

        chat = client.post(
            "/settings/ai/roles/chat",
            json={"role_id": "req-analyst", "messages": [{"role": "user", "content": "你是谁"}]},
            headers={**auth, "X-Mino-Client": "console"},
        )
        check("POST role chat not 501", chat.status_code != 501, f"status={chat.status_code} {chat.text[:200]}")
        check(
            "POST role chat 400 without key",
            chat.status_code == 400,
            f"status={chat.status_code} {chat.text[:200]}",
        )

        tick = client.post(
            f"/app-automation/qa-process/tick/{aid}",
            json={"requirement_id": "req-1", "jobs": ["analyze_req"]},
            headers={**auth, "X-Mino-Client": "studio"},
        )
        check("POST qa-process tick not 501", tick.status_code != 501, f"status={tick.status_code} {tick.text[:200]}")
        check("POST qa-process tick 200", tick.status_code == 200, tick.text[:240])
        job_id = (((tick.json() or {}).get("data") or {}).get("job_id")
                  or (((tick.json() or {}).get("data") or {}).get("job") or {}).get("job_id")
                  or "")
        check("tick returns job_id", bool(job_id), tick.text[:240])
        if job_id:
            import time as _time
            last = None
            for _ in range(20):
                polled_job = client.get(
                    f"/app-automation/qa-process/job/{job_id}",
                    headers={**auth, "X-Mino-Client": "studio"},
                )
                last = polled_job
                if polled_job.status_code != 200:
                    break
                st = (((polled_job.json() or {}).get("data") or {}).get("job") or {}).get("status") or ""
                if st in ("done", "error", "cancelled"):
                    break
                _time.sleep(0.15)
            check("GET qa-process job not 501", last is not None and last.status_code != 501, (last.text if last else "")[:200])
            job_st = ((((last.json() if last and last.status_code == 200 else {}) or {}).get("data") or {}).get("job") or {}).get("status")
            check("qa job reaches terminal", job_st in ("done", "error", "cancelled"), f"status={job_st} {(last.text if last else '')[:200]}")

        figma = client.post(
            f"/app-automation/config/{aid}/figma/sync",
            json={"file_url": "https://www.figma.com/design/abc123xyz"},
            headers={**auth, "X-Mino-Client": "studio"},
        )
        check("POST figma sync not 501", figma.status_code != 501, f"status={figma.status_code} {figma.text[:200]}")
        check("POST figma sync 400 without token", figma.status_code == 400, figma.text[:200])

        with client.websocket_connect(f"/ws?token={token}") as ws:
            ws.send_json({"req_id": "1", "action": "get_server_info"})
            msg = ws.receive_json()
            check("WS get_server_info", msg.get("code") == 200 and msg.get("req_id") == "1", str(msg)[:200])
            ws.send_json({"req_id": "2", "action": "get_device_list"})
            msg2 = ws.receive_json()
            check("WS get_device_list", msg2.get("code") == 200 and isinstance(msg2.get("data"), list), str(msg2)[:200])
            ws.send_json({"req_id": "3", "action": "no_such_action"})
            msg3 = ws.receive_json()
            check("WS unknown action fast-fail", msg3.get("code") == 404, str(msg3)[:200])

        ping = client.get("/health")
        check("GET /health still works", ping.status_code == 200 and ping.json().get("ok") is True, ping.text[:200])

    if hits:
        print("FAIL check_http_contract")
        for h in hits:
            print(f"  {h}")
        print(f"\n  共 {len(hits)} 处失败。见 docs/HTTP.md。")
        return 1
    print("OK check_http_contract — Console/Studio 登录、节点、项目库与执行面契约")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
