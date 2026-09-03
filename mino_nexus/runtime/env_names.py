"""环境名归一化。

port(nexus): 从上游 `server/services/runtime/env_gate.py`（400 行）里**只摘出这两个纯函数**。

那个文件其余部分是"环境闸门"，会 lazy import `project_env` / `account_issue_service` /
`knowledge_briefing` / `playbook_service` / `agent_executor` / `screen` —— 全都还没搬。
整体搬过来会拖进半个服务层，所以先只取 planner 真正用到的 `canon_run_env`。
env_gate 本体等服务层搬完再说。
"""
from __future__ import annotations


_CANON = {
    "test": "test",
    "testing": "test",
    "qa": "test",
    "dev": "dev",
    "pre": "pre",
    "staging": "pre",
    "stg": "pre",
    "prod": "prod",
    "production": "prod",
    "live": "prod",
    "测试": "test",
    "开发": "dev",
    "预发": "pre",
    "正式": "prod",
    "生产": "prod",
}


def canon_run_env(raw: str) -> str:
    s = str(raw or "").strip()
    if not s or s.lower() == "unknown":
        return ""
    return _CANON.get(s.lower(), _CANON.get(s, s.lower() if s.lower() in _CANON.values() else ""))


def env_matches(observed: str, wanted: str) -> bool:
    a, b = canon_run_env(observed), canon_run_env(wanted)
    return bool(a and b and a == b)

