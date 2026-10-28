"""外部 agent 使用 Mino 的入口说明。真源是 MCP 工具 agent.guide，不依赖某个仓库里的 skill 文件。"""
from __future__ import annotations

from typing import Any


def agent_guide() -> dict[str, Any]:
    return {
        "channel": "Mino MCP。工具名里的点写成下划线，例如 agent.guide 调用为 agent_guide。",
        "first_call": "agent_guide",
        "boundaries": [
            "脚本文本写在 script_guide 返回的 workspace.checkout 里，不要把脚本 POST/PUT 给 Nexus。",
            "git clone 和 git fetch 由 Nexus 在跑用例前执行。git commit 和 git push 由这台电脑上的 agent 用本机已有的 Git 身份完成。",
            "Nexus 不保存 GitHub 令牌。私有仓库靠运行 Nexus 的用户在这台电脑上已经能访问的 SSH 或 Git 凭据。保存仓库地址时就会检出；跑用例前如果工作区是干净的再 fetch。",
            "设备、验证码和模型由用例脚本里的 case.* 调用，经 Nexus 派给 Scout。脚本里不要自己连接 adb、WDA 或浏览器。",
        ],
        "flows": [
            {
                "name": "确认能操作哪些项目",
                "tools": ["project_list"],
            },
            {
                "name": "编写或修改用例脚本",
                "tools": ["script_guide"],
                "then": "在返回的 workspace.checkout 里直接改 Python 文件。指令表用同一次返回的 commands。",
            },
            {
                "name": "让 Nexus 跑用例",
                "tools": ["case_list", "device_list", "case_run", "task_get"],
            },
            {
                "name": "查看一次执行",
                "tools": ["task_list", "task_get", "session_list", "session_events", "session_screenshot"],
            },
            {
                "name": "自己操作设备并记到任务里",
                "tools": ["case_external_begin", "case_external_report", "case_external_screenshot", "case_external_finish"],
            },
            {
                "name": "看资源够不够",
                "tools": ["resource_overview", "resource_device_list", "resource_account_list"],
            },
        ],
    }
