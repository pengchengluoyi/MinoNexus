"""应用探索任务：伪用例 + 默认目标（LLM 自由探索）。"""
from __future__ import annotations

from typing import Any

EXPLORE_CASE_ID = "__explore__"

DEFAULT_EXPLORE_GOAL = (
    "在目标 App 内自由探索，尽量发现更多不同页面与入口。"
    "优先切换底栏 Tab、进入列表/卡片详情、使用返回后再尝试未去过的区域。"
    "不要停在登录界面反复点同一按钮；已无法发现新页面时调用 signal_done 结束。"
)

DEFAULT_SUCCESS = (
    "已尽量覆盖主要 Tab 与可见入口，或连续多步无法进入新屏面。"
)


def build_explore_case(
    *,
    instruction: str = "",
    max_steps: int = 80,
    max_idle_steps: int = 15,
) -> dict[str, Any]:
    extra = str(instruction or "").strip()
    goal = DEFAULT_EXPLORE_GOAL
    if extra:
        goal = f"{goal}\n\n补充约束：{extra}"
    return {
        "case_id": EXPLORE_CASE_ID,
        "name": "应用探索",
        "source": "explore",
        "precondition": "",
        "steps": [goal],
        "expected": [],
        "steps_raw": goal,
        "expected_raw": "",
        "success_criteria": DEFAULT_SUCCESS,
        "max_steps": max(10, min(200, int(max_steps or 80))),
        "max_idle_steps": max(3, min(60, int(max_idle_steps or 15))),
        "case_scene": {
            "required_session": "any",
            "session_prep": "skip",
            "device_need": "app",
            "reason": "应用探索：不测登录流程",
        },
    }
