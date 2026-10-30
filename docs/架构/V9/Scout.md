# V9 Scout — 三个模块

日期：2026-10-29。读完 [README](README.md) 再改。只改 `../MinoScout`。协议段落与 Nexus 写入的脚本运行载荷逐字相同。

Scout 从三个入口展开，互不借用。

| 模块 | 职责 |
|---|---|
| 安装包 | 本机渠道程序的文件、版本、是否可用 |
| 调度接口 | 接受脚本运行载荷，核对 run 之后交给脚本执行 |
| 脚本执行 | 跑 `def run(case)`，设备动词走本机渠道 |

IM、CLI、API 仍走现有插件入口。这三个入口不能提交 `script_run`，也不能调用脚本执行。

## 1. 安装包

沿用下载中心已有项：Chromium（对应当前 Playwright）、adb、ADB Keyboard，以及已有的 CLI / 插件安装。本批增加 **Appium** 为一项可查询、可安装的渠道程序，状态与其它项一样上报，不在业务请求里隐式安装。

Nexus 转发的安装请求走已有下载中心合同：固定来源、摘要校验、不接受临时 URL。安装结果用现有进度事件回报。

本模块不读用例仓库，不执行 `run(case)`。

## 2. 调度接口

只接受 README 的脚本运行载荷。

1. `run_id` 必须能向 Nexus 核对本节点、本 `sn` 仍被这次运行占用。核失败：`declined`，不创建脚本进程。
2. 渠道程序未就绪（例如 Android 要 adb 而未安装）：`declined`，原因写明缺哪一项。不在这次运行里自动下载。
3. 同一 `run_id` + `case_id` 重复投递：已在跑则返回进行中，不并行第二份。
4. 通过后把 `files` 落到该次运行的临时目录，再调用脚本执行。运行结束删除临时目录。

调度接口不解析用例业务，不选择下一步点哪里。

## 3. 脚本执行

临时目录的仓库根加入 `sys.path`。入口是载荷里 `script_path` 的 `run(case)`。存在根上的 `pretest` / `posttest` 时，顺序与现在 Nexus 的脚本运行时一致：项目前置、用例前置、`run`、用例后置、项目后置；后置在 `finally`。

`case.*` 的业务含义以 [Agent调度](Agent调度.md) 为准。定位以 [页面与脚本](页面与脚本.md) 为准。

| 调用 | 实现 |
|---|---|
| `launch` `close` `swipe` `back` `wait` `screenshot` `sample` `hierarchy` | 本机已有渠道。缺渠道则这条失败并结束用例 |
| `on_page` `tap` `input` | 在随包的 `pages/*.yaml` 里按 `page_key` 和控件 id 找到 `locator_spec`，只用载荷 `platform` 对应的单元格（`uiautomator` / `xcuitest` / `playwright` / `ax` / `uia`）。没有单元格则本步失败，原因 `locator_missing` |
| `account` `note` | 回调 Nexus |
| `prep` `do` `check` | 直接把步骤进入和离开写入 Nexus 会话，不经过设备动作 |
| `fail` | 直接把失败原因写入 Nexus 会话，然后结束脚本 |
| `otp_mail` | 本节点 Gmail 收件箱取码。应用专用密码在节点保险库 |
| `llm` `vlm` `vlm_frame` | 本节点按已保存的地址、模型名和密钥请求模型。密钥和截图不回 Nexus |
| `fail` | 结束为 `fail` |
| `event` | 直接失败，原因 `event_disabled`。不查能力目录 |

每步设备动作把现有形态的 `tool/result` 交给 Nexus 写入 `session_id`。截图走现有会话截图，不在 Scout 另建一套给 Workbench 的图床。

脚本可以 `from scripts.xxx import ...`。禁止脚本自行连接 adb、WDA、Playwright、Appium，也禁止按 `platform` 分支。禁止 `subprocess` 调这些程序——设备动作只经过上面的 `case.*`。YAML 只解析，不执行。

异常不抛给 Nexus 连接层。与现在的 RESULT 一样，失败也要有结束态。

## 4. 不要做

- 不读 `catalog_entries`，不在节点保存第二份能力目录。
- 不保存账号池，不决定跑哪条用例，不判断缺陷。
- 不 `git clone` 业务仓库，不保存 Git 令牌。文件只来自 `files`。
- 不执行 `pip install`，不跑载荷以外的路径。
- 不改 Nexus、不改 Workbench。
- 协议文档只粘贴脚本运行载荷那一段，不加版本号，不加字段。

## 5. 做完的样子

- 一份只含 `case.launch` 和 `case.tap` 的包在对应渠道上真正点到设备，并在 Nexus 会话里出现 `tool/result`。
- 包里出现 `case.event` 时整次失败且未碰设备。
- IM 消息不能触发 `script_run`。
- Appium 与 Chromium、ADB Keyboard 一样能在下载中心看到状态；业务开跑不会顺带安装它。
