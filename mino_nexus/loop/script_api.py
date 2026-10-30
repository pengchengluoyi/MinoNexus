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
            "一条用例一个文件，放在 tests/<需求ID>/<case_id>.py（目录名用 requirement_id，不用中文、不用 module 名）。不要写平台后缀，不要写 if android。",
            "公共业务放在 scripts/，页面和定位放在 pages/<page_key>.yaml。",
            "步骤编号必须存在于用例原文，缺了就中止。check 的编号和操作步骤是同一个号，不是预期列表的下标。",
            "开跑后整份脚本在 Scout 执行。账号、步骤和失败经 case.* 直接写入 Nexus。验证码和模型在 Scout 本机完成，密钥和视觉判断用的截图不回 Nexus。脚本里不要连接 adb、WDA、Playwright 或 Appium。",
            "不要调用 case.event。页面上没有的控件不要点。",
            "case.fail(message) 直接判定这条用例失败。",
        ],
        "skeleton": (
            "def run(case):\n"
            "    case.launch()\n"
            "    case.on_page(\"login\")\n"
            "    with case.do(1):\n"
            "        case.tap(\"login.submit\")\n"
            "    with case.check(1):\n"
            "        case.on_page(\"home\")\n"
        ),
        "commands": _COMMANDS,
    }


_COMMANDS: list[dict[str, str]] = [
    {"name": "launch", "call": "case.launch()", "effect": "启动当前应用"},
    {"name": "close", "call": "case.close()", "effect": "关闭当前应用"},
    {"name": "on_page", "call": "case.on_page(page_key)", "effect": "用 pages/<page_key>.yaml 里当前渠道的锚点确认到了这一页"},
    {"name": "tap", "call": "case.tap(control)", "effect": "按页面里的控件 id 或 label 点击，定位取本趟渠道。写成 page_key.control_id 时可以不先 on_page"},
    {"name": "input", "call": "case.input(control, text)", "effect": "向该控件输入。定位规则与 tap 相同"},
    {"name": "vlm", "call": "case.vlm(prompt, verdict=\"\")", "effect": "用本节点的模型看最近一张截图。verdict=\"pass\" 时未通过会中止。截图不发给 Nexus"},
    {"name": "screenshot", "call": "case.screenshot()", "effect": "截图并写入会话。返回宽高，图像留给随后的 vlm"},
    {"name": "sample", "call": "case.sample(duration_ms, interval_ms=300, timeline=True)", "effect": "只按间隔截图，不调用模型。默认 timeline=True，每一帧各占时间线一张图。timeline=False 时时间线只留一张封面，整组帧仍随这条记录返回。同一次用例可以多次采集。最多 40 帧"},
    {"name": "vlm_frame", "call": "case.vlm_frame(index, prompt, verdict=\"\")", "effect": "用本节点的模型看 sample 的其中一帧。截图不发给 Nexus"},
    {"name": "note", "call": "case.note(text, **fields)", "effect": "把自定义证据写进会话。不查能力目录，也不操作设备"},
    {"name": "swipe", "call": "case.swipe(direction)", "effect": "滑动，direction 为 up/down/left/right"},
    {"name": "back", "call": "case.back()", "effect": "返回"},
    {"name": "wait", "call": "case.wait(ms)", "effect": "等待毫秒"},
    {"name": "hierarchy", "call": "case.hierarchy()", "effect": "读取当前界面结构，文本在 data.text"},
    {"name": "account", "call": "case.account()", "effect": "当前租到的账号。没有账号会中止"},
    {"name": "otp_mail", "call": "case.otp_mail()", "effect": "用本节点 Gmail 收件箱取验证码。先调用过 case.account() 时按该邮箱过滤"},
    {"name": "llm", "call": "case.llm(prompt, text=\"\", schema=None)", "effect": "用本节点保存的模型地址、模型名和密钥调用文本模型。无 schema 时返回字符串"},
    {"name": "fail", "call": "case.fail(message)", "effect": "判定失败并停止"},
]
