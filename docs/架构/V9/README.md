# V9 脚本执行下沉 — 止血

日期：2026-10-29。状态：**已修订。** 一份用例一个脚本；页面逻辑在仓库 `pages/`；删除 `agent_loop`。布局以 [页面与脚本](页面与脚本.md) 为准。

本文冻结 DSH Bot 与其他 agent 的脚本执行怎么分工。三端同时开工时，只改自己仓库里本文点名的范围。字段表以本文为准，禁止一边开发一边加字段。

| 文档 | 谁读 | 仓库 |
|---|---|---|
| [Nexus](Nexus.md) | Nexus agent | 本仓 `MinoNexus` |
| [Scout](Scout.md) | Scout agent | `../MinoScout` |
| [页面与脚本](页面与脚本.md) | 三端都读。仓库三目录和页面真源 | — |
| [Agent调度](Agent调度.md) | dshbot 与其他写脚本、跑用例的 agent | 不改产品代码；Nexus 把同一份规则放进 `script_guide` |
| [Workbench](Workbench.md) | Workbench agent | `../MinoWorkbench` |

## 1. 冻结结论

执行过程中的逐步调度不在 Nexus。Nexus 签发一次运行并保存共享状态。Scout 跑完整份脚本，本机调用渠道。业务差异写在该项目的脚本仓库里，不新增全局能力。

```
dshbot / 其他 agent
  拆任务、选用例、判断缺陷、写仓库
  MCP：开跑、读日志、释放隔离、让节点安装渠道包
        │ run_id
Nexus
  设备态、账号租约、session_events、隔离与 baseline
  项目仓库地址；把本次脚本打成文件包交给 Scout
        │ 脚本运行载荷
Scout
  安装包：渠道程序的下载与状态
  调度接口：只接受带 run_id 的脚本运行
  脚本执行：run(case)，设备动词在本机，账号 / 模型 / 证据回调 Nexus
        │
Workbench
  读仓库地址、script_path、模块目录、日志
```

## 2. 三端交接点

只允许下面这一个新载荷。不新开 WebSocket，不第二套结果格式。

### 2.1 脚本运行载荷（Nexus → Scout）

| 字段 | 含义 |
|---|---|
| `run_id` | Nexus 已准入的任务号 |
| `session_id` | 写入 `session_events` 的会话 |
| `case_id` | 用例 id |
| `sn` | 本趟设备 |
| `platform` | `android` / `ios` / `web` / `mac` / `win` |
| `script_path` | 仓库内相对路径，必须是 `.py` |
| `files` | `{相对路径: 文本}`。用例 `.py`、根上的 `pretest.py` / `posttest.py`（有才带）、`scripts/**/*.py`、`pages/**/*.yaml` |

约束：

- 路径必须落在仓库内，禁止 `..` 和绝对路径。单文件不超过 256KB，整包不超过 4MB，文件数不超过 256。
- 可执行的只有用例和 `scripts/` 的 `.py`。`pages/` 只放 YAML。不含 `requirements.txt` 的安装动作，Scout 不因这次运行执行 pip。
- Scout 看不到 Nexus 的 `case_workspaces` 绝对路径。禁止把本机路径当作节点上的路径。
- 令牌无效、设备与 `sn` 不符、节点未登记该渠道：整次 `declined`，不执行脚本。

### 2.2 运行中的回调（Scout → Nexus）

脚本里的 `case.*` 分成两处。名字保持现有合同，见 `mino_nexus/loop/script_api.py`。

| 调用 | 谁执行 |
|---|---|
| `launch` `close` `tap` `input` `swipe` `back` `wait` `screenshot` `sample` `hierarchy` `on_page` | Scout 本机渠道。`tap` / `input` / `on_page` 按 [页面与脚本](页面与脚本.md) 用 `pages/` 和本趟 `platform` 取定位 |
| `account` `otp_mail` `llm` `vlm` `vlm_frame` `note` | Nexus。Scout 带 `run_id` + `session_id` 调用，不保存模型密钥和账号密码 |
| `fail` | Scout 结束脚本，结果 `fail` |
| `event` | 停用。业务步骤写在 `scripts/`，定位写在 `pages/` |

每步设备动作仍以现有 `tool/result` 写入该 `session_id`。截图引用沿用现有会话截图。Workbench 和 `session_events` 不改事件名。

### 2.3 开跑与读回（agent → Nexus，已有 MCP）

| 步骤 | 工具 | 本批 |
|---|---|---|
| 看清入口 | `agent_guide` | 改文案：脚本运行不再逐跳由 Nexus 决策 |
| 改仓库 | `script_guide` | 布局改为本文 §3 |
| 开跑 | `case_run` | 只做租约、会话、下发脚本运行载荷。`agent_loop` 删除，没有逐步决策入口 |
| 读结果 | `task_get` `session_events` | 不改字段 |
| 释放隔离 | 现有 baseline | 不改 |
| 装渠道程序 | 新工具只转发 Scout 下载中心已有安装项 | Nexus 自己不下载安装包 |

缺陷是否成立、跑哪些用例、聊天和记忆留在 dshbot。不进 Nexus，不进 Scout。

## 3. 仓库布局

见 [页面与脚本](页面与脚本.md)。摘要：`scripts/` 放公共业务函数，`tests/<模块>/<case_id>.py` 一个用例一个文件，`pages/<page_key>.yaml` 放现在库里的页面逻辑。渠道只影响 YAML 里的定位单元格。

## 4. 本批不做什么

- 不保留 `agent_loop`。调用它的开跑和回放入口一起删。
- 不把场景步骤登记成新的 `catalog_entries`。
- 不按渠道再拆一份用例脚本。
- 不让 IM / CLI / API 启动脚本或碰设备。
- 不在 Scout 上保存账号池、模型密钥、Git 令牌。
- 不把 `note` 当成可执行的自定义能力。
- 不把页面 YAML 写回 `nav_fsm*` 或控件表。库表停写，本批不删表。
- 不为这条链路新做前端。应用逻辑改为读仓库里的 `pages/`。
