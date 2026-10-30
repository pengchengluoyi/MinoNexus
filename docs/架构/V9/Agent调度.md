# V9 Agent 调度 — dshbot 与其他 agent

日期：2026-10-29。这是给 dshbot 和其他 agent 的使用说明，也是自定义脚本的写法规范。实现以 [README](README.md) 和 [页面与脚本](页面与脚本.md) 为准。产品里的当次返回以 MCP `agent_guide`、`script_guide` 为准；与本文不一致时以当次返回为准。

你负责拆任务、决定跑哪些用例、判断是不是缺陷、维护聊天和结论。你不操作设备，不新增全局能力，不把脚本文本提交给 Nexus 保存。Nexus 不再逐步决定下一次点击。

## 1. 每次先看入口

1. 调用 `agent_guide`。
2. 要改脚本或页面时调用 `script_guide`，传入项目。没有检出时这一步会克隆仓库。
3. 只在返回的 `workspace.checkout` 里改文件。提交和推送用本机 Git。Nexus 不保存 Git 令牌。

## 2. 三个目录各写什么

```
scripts/login.py                 # 多条用例共用的业务函数
tests/账号/case07fffa13.py        # 这一条用例的业务顺序，全渠道只此一份
pages/登录.yaml                  # 页面、控件、各渠道定位、入口
pretest.py                       # 整个项目的前置，已有才改
posttest.py
```

| 要改的内容 | 放哪 |
|---|---|
| 先做什么、后做什么、哪一步算通过 | `tests/<模块>/<case_id>.py` 的 `run` |
| 登录填表这类被多条用例调用的业务 | `scripts/` 里的函数 |
| 这个页面叫什么、上面有哪些控件、Android 和 iOS 分别怎么点到 | `pages/<page_key>.yaml` |

模块是稳定的业务块，例如账号、首页。不要用需求编号当目录。文件名用用例 id。一个 `case_id` 只有一个 `.py`，不要再写 `case07fffa13.android.py`。

渠道不同，只改 `pages/` 里该控件的定位单元格，不复制用例文件。

## 3. 用例怎么写

`run(case)` 只表达业务顺序。步骤编号用用例原文里的号：`with case.do(n)` 是操作，`with case.check(n)` 是同一个号的预期。

公共业务放函数，用例只编排：

```python
# scripts/login.py
def fill(case):
    case.on_page("login")
    account = case.account()
    case.input("login.email", account["email"])
    case.input("login.password", account["password"])
    case.tap("login.submit")
```

```python
# tests/账号/case07fffa13.py
from scripts.login import fill

def run(case):
    case.launch()
    with case.do(1):
        fill(case)
    with case.check(1):
        case.on_page("home")
```

规范化就这几条：

1. 一条用例一个文件，函数从 `scripts/` 来，定位从 `pages/` 来。
2. 脚本里不写平台判断，不写 `if android`。
3. 控件用 `page_key.control_id`，例如 `login.submit`。先 `case.on_page("login")` 之后可以只写控件 id `submit`。
4. 页面文件里没有的控件不要点。不要改用整屏搜索文案来绕过。
5. 坐标不写在用例里。某个渠道只能按坐标点时，写进该渠道的 `locator_spec`。
6. 重复三次以上的一段业务收成 `scripts/` 的函数。只在这一条用例里出现的步骤留在 `run` 里。
7. 断言用 `case.on_page` 或 `case.vlm`。`verdict="pass"` 未通过会中止。

## 4. 内部指令

设备、账号、模型都走 `case.*`。不要在脚本里连接 adb、WDA、Playwright 或 Appium。

| 调用 | 作用 |
|---|---|
| `case.launch()` / `case.close()` | 启动或关闭当前应用 |
| `case.on_page(page_key)` | 用 `pages/<page_key>.yaml` 里当前渠道的锚点确认到了这一页 |
| `case.tap(control)` | 按页面里的控件 id 或 label 点击，定位取本趟渠道 |
| `case.input(control, text)` | 向该控件输入 |
| `case.swipe("up")` | 方向为 up / down / left / right |
| `case.back()` | 返回 |
| `case.wait(毫秒)` | 等待 |
| `case.screenshot()` / `case.hierarchy()` | 截图或读取结构 |
| `case.sample(时长毫秒)` | 按间隔截图，不调用模型 |
| `case.vlm(问题, verdict="pass")` | 看最近截图 |
| `case.vlm_frame(序号, 问题)` | 看 `sample` 的其中一帧 |
| `case.account()` | 本次租到的账号。没有会中止 |
| `case.otp_mail()` | 按环境取验证码 |
| `case.llm(问题)` | 文本模型。不要用它决定下一步点哪个控件 |
| `case.note(文本)` | 只记证据，不操作设备 |
| `case.fail(原因)` | 判定失败并停止 |

不要调用 `case.event`。它已删除。业务差异写成 `scripts/` 里的函数。

页面文件从 `page_key` 写起，不写 schema 版本。控件的 `locator_spec` 按渠道分格：`uiautomator`、`xcuitest`、`playwright`、`ax`、`uia`。某一格空着，这个渠道跑到该控件就失败，理由是 `locator_missing`。补这一格，不要另写一份用例。

## 5. 怎样跑

1. `case_list`、`device_list` 看用例和设备。
2. `case_run` 开跑。这一步占用设备和账号，Scout 执行整份脚本。
3. `task_get`、`session_events` 看 `tool/result` 和截图。
4. 失败后若设备和账号被隔离，用原来的 baseline 释放。被节点断开取消的记作取消。
5. 是不是缺陷，由你根据日志判断，结论留在你自己的记忆里。

渠道程序没装好时，开跑会拒绝并写明缺什么。用 Nexus 提供的安装工具让该节点下载 Chromium、adb、ADB Keyboard 或 Appium。不要在自己的机器上装这些来代替节点。

## 6. 不要做

- 不要为 Android、iOS、Web 各写一份用例脚本。
- 不要把页面、控件、定位写回 Nexus 的应用逻辑库。写在 `pages/`。
- 不要把场景步骤加成全局能力后再发版。
- 不要用 `note` 充当执行步骤。
- 不要把密码、模型密钥写进仓库。账号内容只在运行时从 `case.account()` 拿。
- 不要在 IM、CLI 对话里直接要求节点执行脚本。先走 `case_run`。
