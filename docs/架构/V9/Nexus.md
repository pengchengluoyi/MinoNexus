# V9 Nexus — 在线服务与下发

日期：2026-10-29。读完 [README](README.md) 再改。只改本仓。

Nexus 继续保存设备态、账号租约、执行日志和隔离。脚本开跑不再进入逐步决策。一次运行收成一包脚本运行载荷交给 Scout。

## 1. 要做

### 1.1 脚本开跑绕过逐步决策

`case_run` 在用例走脚本仓库时：

1. 沿用现有准入：设备占用、账号租约、写安全。失败则不要下发。
2. 打开现有会话，`session_id` 规则不变。
3. 按 [页面与脚本](页面与脚本.md) 组装脚本运行载荷：用例文件、根上的 `pretest.py` / `posttest.py`、`scripts/` 下的 `.py`、`pages/` 下的 `.yaml`。超限拒绝开跑，原因写进任务，不静默截断。
4. 把这一包发给持有该 `sn` 的 Scout。`platform` 只用于 Scout 选定位单元格。
5. 收回的步骤结果仍写入原来的 `session_events`。隔离和 baseline 仍由现有写安全处理。

删除 `mino_nexus/loop/agent_loop.py`，以及仍调用 `run_case` 的入口（当前有 `loop/case_exec.py`、`api/replay.py`）。回放只读已有 `session_events`，不再重新决策。没有单独的脚本文件（一个 `case_id` 对应一个不含平台后缀的 `.py`）时开跑失败，原因写明，不要改跑现在仓库里 `cases/<case_id>.android.py` 这种渠道文件。新脚本只认 `tests/`。

### 1.2 回调

Scout 执行 `account` / `otp_mail` / `llm` / `vlm` / `vlm_frame` / `note` 时回调本仓。校验 `run_id` 仍是这次准入、`session_id` 匹配、调用方是该设备所在节点。模型密钥只走 `platform/settings.py` 的执行账号，不把密钥放进下发载荷。

`note` 仍是证据，限制与现在的 `case.note` 相同。

### 1.3 `script_guide` 布局

`GET` 脚本引导的 `layout` 改成 [页面与脚本](页面与脚本.md)：

- `scripts_package`：`scripts/`
- `tests_root`：`tests/<模块>/<case_id>.py`，一个用例一个文件
- `pages_dir`：`pages/<page_key>.yaml`
- `path_field` 仍是 `script_path`
- 去掉 `{app_id}/` 建议目录，去掉按渠道文件名回退

### 1.4 应用逻辑改读仓库

按 [页面与脚本 §4](页面与脚本.md) 做一次导出：现有页面、控件定位、入口写成 `pages/*.yaml`。导出后 `logic.page.*`、`logic.control.*`、`logic.entry.*` 只读检出里的 YAML。停写 `nav_fsm*`、控件表和逻辑块表。没有 `pages/` 时返回空列表，不读库。库表本批不删。

`agent_guide` 里「由 Nexus 派给 Scout」改成：开跑后整份脚本在 Scout 执行；账号、验证码和模型经 `case.*` 回调 Nexus。脚本文本仍然只写在 `script_guide` 返回的检出目录，不 POST 给 Nexus。Git 提交仍在本机，Nexus 不保存 Git 令牌。

### 1.5 DSH 插件适配

插件安装之后打开的是现有 Workbench，origin 仍编译进前端。本仓提供两件事：

- 一个入口说明：插件应打开的 Workbench 地址，以及登录失败只提示「无法连接服务器」。
- 一个 MCP 工具，把「安装 / 更新渠道程序」转成该节点下载中心已有的安装请求。允许的项与 Scout 下载中心合同一致，并加上 Scout 本批新增的 Appium 项（名称以 [Scout](Scout.md) 为准）。Nexus 进程不下载、不保存这些安装包。

工具只接受已认证用户名下、当前在线的节点。不接受临时 URL。

### 1.6 协议

脚本运行载荷的字段只使用 README 那张表，不另加版本号。写入本仓 `docs/基础框架/PROTOCOL.md` 时整段复制，不要增删字段。Scout 仓的协议文档贴同一段。`protocol.py` 能往返这一包；不提交 fixture。

## 2. 不要做

- 不留下 `agent_loop` 或任何逐步决策入口。
- 不在执行循环里为脚本步骤计算 `executor_order` 或选择能力目录行。
- 不把业务仓库里的函数登记进 `catalog_entries`。
- 不实现 `case.tap` 等设备动词。那是 Scout 的脚本执行模块。
- 不改 Workbench 页面。
- 不改 `../MinoScout` 以外的协议文本时顺手改实现；Scout 实现由 Scout 那份文档负责。本仓若改 `docs/基础框架/PROTOCOL.md`，只加脚本运行载荷这一段。

## 3. 做完的样子

- 仓库里不再有 `agent_loop.py`。`case_run` 不会产生 `agent-decide`。
- Scout 收到的载荷含 `version=1`、用例 `.py` 和 `pages/*.yaml`，不含本机绝对路径，不含模型密钥。
- 只有渠道后缀脚本、缺页面文件、超限、节点不在线时任务失败原因可读。
- 应用逻辑接口在有 `pages/` 时返回其中的页面和控件；库里的旧页面不再被读出来。
