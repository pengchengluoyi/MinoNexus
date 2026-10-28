"""给 MCP 和写脚本的工具看的用例脚本合同。设备、模型和视觉判断都走 case.*。"""
from __future__ import annotations

from typing import Any


def script_contract() -> dict[str, Any]:
    return {
        "entry": "def run(case)",
        "optional_entries": ["def pretest(case)", "def posttest(case)"],
        "project_hooks": ["pretest.py", "posttest.py"],
        "case_hooks": "用例文件里也可以定义 pretest(case) 和 posttest(case)。后置在 finally 里执行。",
        "scopes": [
            "with case.prep(n): 用第 n 条前置原文",
            "with case.do(n): 用第 n 条操作原文",
            "with case.check(n): 用编号为 n 的预期，和操作步骤同一个号",
        ],
        "rules": [
            "步骤编号必须存在于用例原文，缺了就中止。check 的编号和操作步骤是同一个号，不是预期列表的下标。",
            "设备和模型不要在脚本里自己连。用 case 上的指令，由 Nexus 派给 Scout。",
            "返回值有 ok、summary、status、data。ok 为假时脚本自己决定是否 case.fail。",
            "case.fail(message) 直接判定这条用例失败。",
        ],
        "skeleton": (
            "def run(case):\n"
            "    case.launch()\n"
            "    with case.do(1):\n"
            "        case.tap(\"登录\")\n"
            "    with case.check(1):\n"
            "        case.vlm(\"是否出现首页\", verdict=\"pass\")\n"
        ),
        "commands": _COMMANDS,
    }


_COMMANDS: list[dict[str, str]] = [
    {"name": "launch", "call": "case.launch()", "effect": "启动当前应用"},
    {"name": "close", "call": "case.close()", "effect": "关闭当前应用"},
    {"name": "tap", "call": "case.tap(target, x=None, y=None)", "effect": "按可见文案在界面层级里点击。文案不在层级里时，可再带 x、y 作为坐标"},
    {"name": "vlm", "call": "case.vlm(prompt, verdict=\"\")", "effect": "看最近一张截图，返回看到的文字。verdict=\"pass\" 时未通过会中止"},
    {"name": "screenshot", "call": "case.screenshot()", "effect": "截图并写入会话。返回宽高，图像留给随后的 vlm"},
    {"name": "sample", "call": "case.sample(duration_ms, interval_ms=300, timeline=True)", "effect": "只按间隔截图，不调用模型。默认 timeline=True，每一帧各占时间线一张图。timeline=False 时时间线只留一张封面，整组帧仍随这条记录返回。同一次用例可以多次采集。最多 40 帧"},
    {"name": "vlm_frame", "call": "case.vlm_frame(index, prompt, verdict=\"\")", "effect": "采样结束后，对其中一帧做视觉判断，返回看到的文字"},
    {"name": "note", "call": "case.note(text, **fields)", "effect": "把自定义证据写进会话。不查能力目录，也不操作设备"},
    {"name": "input", "call": "case.input(target, text)", "effect": "向控件输入文本"},
    {"name": "swipe", "call": "case.swipe(direction)", "effect": "滑动，direction 为 up/down/left/right"},
    {"name": "back", "call": "case.back()", "effect": "返回"},
    {"name": "wait", "call": "case.wait(ms)", "effect": "等待毫秒"},
    {"name": "hierarchy", "call": "case.hierarchy()", "effect": "读取当前界面结构，文本在 data.text"},
    {"name": "account", "call": "case.account()", "effect": "当前租到的账号。没有账号会中止"},
    {"name": "otp_mail", "call": "case.otp_mail()", "effect": "按环境策略取验证码"},
    {"name": "llm", "call": "case.llm(prompt, text=\"\", schema=None)", "effect": "调用文本模型。无 schema 时返回字符串"},
    {"name": "event", "call": "case.event(name, **params)", "effect": "按能力名派发一条内部事件"},
    {"name": "fail", "call": "case.fail(message)", "effect": "判定失败并停止"},
]
