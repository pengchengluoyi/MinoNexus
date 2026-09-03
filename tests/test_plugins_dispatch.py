"""调用记录列表 + 集成插件目录，不能再返回空数组。

    python tests/test_plugins_dispatch.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="mino-plugins-test-")
os.environ["MINO_NEXUS_DATA_DIR"] = _TMP

from mino_nexus.ai import dispatch_log  # noqa: E402
from mino_nexus import plugins_store as ps  # noqa: E402
from mino_nexus import settings_store as ss  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}{' — ' + detail if detail else ''}")
        failures.append(name)


print("== 插件目录不再是空列表 ==")
empty = ps.list_integration_plugins()
check("有分类", len(empty.get("categories") or []) >= 4, str(empty.get("categories")))
check("有 7 个集成插件", len(empty.get("plugins") or []) == 7, str([p["id"] for p in empty.get("plugins") or []]))
check("未配置时不是 ready", all(p["status"] != "ready" for p in empty["plugins"]), str([p["status"] for p in empty["plugins"]]))

print("== 写入真实禅道 / Figma / 飞书机器人后状态跟着变 ==")
ss.save_figma_settings(access_token="fig-test-token")
ps.save_integration_plugin("zentao", {"enabled": True, "url": "https://zt.example.com", "account": "qa", "token": "tok-1"})
bot = ps.create_robot_integration(platform="lark", name="用例机器人", credentials={"app_id": "cli_1", "app_secret": "sec-1"})
listed = ps.list_integration_plugins()
by_id = {p["id"]: p for p in listed["plugins"]}
check("figma ready", by_id["figma"]["status"] == "ready", by_id["figma"]["status"])
check("zentao ready", by_id["zentao"]["status"] == "ready", by_id["zentao"]["status"])
check("feishu ready", by_id["feishu"]["status"] == "ready", by_id["feishu"]["status"])
check("wechat 仍待连接（扫码未搬）", by_id["wechat"]["status"] == "need_connect", by_id["wechat"]["status"])
detail = ps.get_integration_plugin("zentao")
check("禅道 token 不明文回给前端", "tok-1" not in json.dumps(detail, ensure_ascii=False))
check("禅道 has_token", detail["config"]["has_token"] is True)
feishu = ps.get_integration_plugin("feishu")
check("飞书带 robots", any(b.get("id") == bot["id"] for b in feishu.get("robots") or []), str(feishu.get("robots")))

print("== 调用记录读写同一份 jsonl ==")
dispatch_log._write({
    "id": "ds-test-1",
    "kind": "llm",
    "status": "done",
    "trigger": "case_run",
    "job": "agent-decide",
    "role": "test-engineer",
    "input": "hello",
    "output": "ok",
})
rows = dispatch_log.list_calls(limit=10)
check("list_calls 非空", len(rows) >= 1, str(len(rows)))
check("能按 id 取回", dispatch_log.get_call("ds-test-1") is not None)
check("落盘在测试目录", Path(_TMP, "data", "dispatch", "calls.jsonl").is_file())

print()
if failures:
    print(f"FAILED {len(failures)}: {', '.join(failures)}")
    sys.exit(1)
print("ALL OK — plugins + dispatch")
