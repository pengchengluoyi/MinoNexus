# MCP 与 LLM 响应查询

需求：让外部 Agent 通过 mino MCP 分析任务失败，不必读取数据库、索取登录凭据或依赖截断的任务摘要。

## 1. 接入与调用顺序

使用 Workbench 生成的个人 MCP token 接入 Nexus `/mcp`，保持现有认证和工具白名单。客户端配置文件由 MCP 客户端读取，不能把 token 打印到日志或发给模型。

连接后调用 `tools/list`，下列诊断工具仅开放给 MCP，均为只读，不会重跑用例或操作设备：

| 工具名 | 参数与用途 |
|---|---|
| `task_get` | `task_id`：任务状态和用例结果；MCP 返回完整结构，不再替换成 4000 字符 preview |
| `session_list` | `run_id`，可选 `case_id/status/limit/offset`：查完整会话 ID |
| `session_events` | `session_id/from_seq/limit`：读取原始事件及工具、守卫、回看事实 |
| `session_llm_list` | `session_id`，可选 `turn/from_seq/limit`：查模型调用及 `dispatch_id` |
| `llm_get` | `dispatch_id`，可选 `section/offset/limit`：读取响应、请求或元信息 |

定位 `cr-c8a13f5e7b41` 的调用顺序：

```json
{"name":"session_list","arguments":{"run_id":"cr-c8a13f5e7b41"}}
{"name":"session_llm_list","arguments":{"session_id":"<上一步返回的完整 session_id>"}}
{"name":"llm_get","arguments":{"dispatch_id":"<目标回合的 dispatch_id>","section":"response"}}
{"name":"llm_get","arguments":{"dispatch_id":"<同一 dispatch_id>","section":"request"}}
```

以上为 MCP `tools/call` 的 `params`，不是聊天正文。查看 `session_events` 中的 `review/hold`、`review/fail` 与 `exec/vision`，再用同回合的 LLM 响应核对模型是否返回 `prior_status`。

## 2. 分页与完整性

- `session_list` 按 `offset/limit` 翻页，结合 `total` 判断结束。
- `session_events`、`session_llm_list` 按 `next_from_seq` 翻页。后者在数据库内筛选 LLM 事件，不受旧投影只读取前 2000 个事件的限制。
- `llm_get` 的 `content` 是所选 section 的 JSON 文本片段。`has_more=true` 时使用 `next_offset` 继续；顺序拼接全部片段后再解析 JSON。`total_chars` 为总字符数，不是字节数。
- MCP 不经过 IM 的 4000 字符摘要裁剪；IM 原有行为保持不变。客户端自身的显示或上下文限制不属于服务端截断，应优先分页查询。

## 3. 原始响应与历史限制

新 LLM 记录增加 `raw_response`，保存 JSON 解析及工具决策归约前的助手文本与原始 `tool_calls`，包括 function arguments 字符串；不走旧输出的 8000 字符裁剪。图片仍通过现有媒体提取处理，不返回 base64 全图。这里的“原始响应”不是供应商完整 HTTP 报文。

`llm_get(section=response)` 同时提供：

- `raw_response`：解析前文本和工具调用。
- `parsed_output`：原有日志输出，供前后比较；历史记录可能已在落盘时裁剪。
- `parsed_output_truncated`：新记录明确标识解析输出是否在落盘时裁剪；旧记录为 `null`，代表没有该标记。不能把裁剪造成的字段缺失归因于解析器。
- `raw_response_available`：是否实际保留了解析前响应。
- `raw_response_redacted`：是否因原有助手隐私规则隐藏正文。

历史记录不能补回之前没有保存的原始参数。`raw_response_available=false` 时，只能把 `parsed_output` 称为已有的解析结果，不能断言模型当时确实漏填字段。`section=request` 读取原有保存的系统提示和用户输入，历史 8000 字符裁剪也无法还原。

不公开 `/settings` 配置、provider key、认证信息或任意 HTTP 代理。诊断详情复用既有调度记录，走 `/case-runner/llm/{dispatch_id}` 的独立只读接口。已超过调度日志保留范围的记录返回 404，不伪造空的成功结果。

## 4. 发布与验收

发布后重启 Nexus，并让 MCP 客户端重新连接、刷新工具列表。无需升级 Scout 或修改 Nexus/Scout 协议。

验收以真实 Session 为准：按 run_id 找会话，按回合找 dispatch_id，获取完整响应并对比原始 function arguments 与解析输出；验证分页续读、历史不可用标记、助手隐私脱敏以及不存在的 ID 返回错误。运行服务未加载新版本之前，不能将这些检查标为已通过。


## 2026-10-15：通过 MCP 更新 Scout

新增 `scout_list`、`scout_update(node_id, reason?)`、`scout_update_status(node_id)`。先列节点取得准确 node_id（不是设备 SN），明确更新目标后调用更新，再查询状态。服务需加载本次代码，客户端刷新 tools/list。

更新复用现有 node.update，使用 Scout 已配置的 manifest（默认最新发布）。不支持 version、降级或自定义 URL；额外参数明确拒绝，不会静默忽略用户指定的版本。通用 stop/restart/command 路由不向 MCP 放开。

节点归属、在线状态、已知活动任务和更新在途均检查；同一 Nexus 进程内并发更新拒绝。沿用账号与稳定 JSON-RPC id 的写收据，重取不重放。Scout 失败返回 MCP isError；超时或异常为结果不确定，先查状态，不能换请求 ID 盲重试。

成功返回 accepted 与 running_version_verified=false，仅代表 Scout 已应答。查询返回 running_version（在线节点上报）、last_reported_version、process_generation、connection_generation、update_job 和 command_inflight；离线 running_version=null。应在重连后核对实际运行版本，不能从“下载完成”或旧版本号判定更新成功。

当前入口不是 V6 D1 的持久更新队列：尚无跨进程/全入口原子 update_hold，活动任务检查后新任务仍可能竞入；应在维护空闲窗口调用。更新不会清除已有未知写隔离。固定版本、回退、排队和自动终态核验需 Scout/Nexus 后续共同实现。

本次用隔离 SQLite 和真实 ASGI/MCP 完成鉴权、工具发现、离线拒绝、重复请求和参数边界检查；未使用 pytest/mock，未修改业务库，未执行真实节点升级。线上下载安装/重连验收另记。

## 2026-10-27：节点配置能力（CLI / API / IM / Gmail）的 MCP 工具

除诊断工具外，MCP 现在可以直接使用 Workbench 节点配置里的四类能力。工具名与参数：

| 工具名 | 参数 | 用途与边界 |
|---|---|---|
| `cli_query` | `plugin`、`command`、`options?`、`node_id?` | 只读跑飞书/Meego CLI；节点默认来自当前 IM 会话，没有会话时自动选本人唯一已配置在线节点 |
| `cli_execute` | 同上，另可带 `attachment_ref` / `attachments` | 写操作；MCP 入口没有 IM 会话附件，带附件会 422；写回执按 JSON-RPC id 去重 |
| `case_api` | `project_id`、`operation`、`configuration`、`limit?` | 读 Workbench「API」集成；`operation=projects/sessions/cases`，`configuration.node_id` 必填；结果超过 `limit` 标 `truncated=true` |
| `im_channels` | 无 | 先看本人节点有哪些 IM 渠道、是否已绑定、是否可发；不返回密钥或聊天正文 |
| `im_send` | `channel`、`chat_id`、`text`、`chat_type?`、`reply_ctx?`、`client_request_id?` | 发文本；`chat_id` 必须由用户给出；飞书 CLI / LangBot 实例由节点状态补全；微信主动发送需要入站 `context_token` |
| `gmail_otp` | `node_id?`、`to_address?`、`since_ts?`、`lookback_seconds?`、`max_wait_ms?` | 从本人节点 Gmail 取码；只返回 4-8 位数字和诊断，最多等 45 秒 |

建议顺序：先 `im_channels` 确认渠道与节点，再 `im_send`；Gmail 直接调 `gmail_otp`，取不到时先确认邮件已到达，不要反复扩大等待窗口；CLI 查询用 `cli_query`，明确要创建/修改才用 `cli_execute`；用例服务先用 `operation=projects`/`sessions` 缩小范围，再读 `cases`。

排障要点：

- `im_send` 返回 422 且提示 `context_token`，是微信没有入站会话上下文，不是节点离线；不要声称已发送。
- `im_send` 返回 409，多为渠道未上报、未配置、被其他节点持有或机器人实例为空；按提示去 Workbench 检查，不换账号或换节点兜底。
- `gmail_otp` 返回 502/“没有取到验证码”，先核对 `to_address`、`since_ts` 和邮件是否到达；工具不会读取或返回邮件正文。
- `case_api` 返回 `source_api_*` 原因码时，按原因码检查项目权限、节点合同、远端项目/会话 id 和服务地址；不要把空结果当成“没有权限”。
- `cli_execute` 超时或返回 503 时按“结果未知”处理，先查第三方业务状态，不换 JSON-RPC id 重放。

## 2026-10-27：插件安装状态与安装/更新

新增两个 MCP 工具：

| 工具名 | 参数 | 用途与边界 |
|---|---|---|
| `plugin_status` | `node_id?` | 查看节点插件 `installed/configured`；指定 `node_id` 时附带下载中心 `installable/updatable/version/install_state` 和任务 |
| `plugin_install` | `node_id`、`plugin`、`action?`、`operation_id?`、`wait_ms?` | 安装或更新 `cli.feishu`、`cli.meego`、`langbot`，由 Scout 下载中心异步执行 |

建议顺序：先 `plugin_status` 找到节点和缺失插件；再 `plugin_install(action=install)`；long 任务用返回的 `task_id` 再查 `plugin_status`，或在提交时给 `wait_ms`。`langbot` 需要可用 Python 3.11+；`missing_python` 应先在节点准备 Python，不要反复提交安装。`case_api`、`gmail`、`bot.*` 属于内置能力，`installed=true` 不代表已配置，保存配置后看 `configured`。

排障：

- `plugin_status` 返回 `plugins_known=false`：节点还没上报插件快照，不是“未安装”，等待节点就绪或检查 Scout 版本。
- `plugin_install` 返回 409 `tool_busy`：同节点已有安装任务；先查 `tasks` 和任务 `state`，不要换 `operation_id` 重试。
- 任务 `state=unknown`：节点结果不确定；只查原 `task_id`，不要重新 `install`。
- 任务 `state=failed` 且 `error` 只给阶段：看节点安装日志中的任务 id；Nexus 不回传下载 URL、凭据或完整子进程输出。
- 管理员能看到他人节点的 `plugin_status` 快照，但 `plugin_install` 只允许 owner；这是账号隔离，不是节点离线。

## 2026-10-27：测试资源与应用逻辑工具

新增 20 个工具：`resource_*` 9 个、`logic_*` 11 个。它们指向新的安全读模型，不复用 Workbench 的旧账号/环境/设备路由；账号列表/详情/预约返回账号、密码、OTP 明文；日志/环境仍按原脱敏。

| 工具名 | 用途 |
|---|---|
| `resource_overview` | 项目资源全景：账号/设备/租约计数、最近日志 |
| `resource_account_list` | 账号列表；`state=free\|occupied\|locked`、`lifecycle`、`env`、`q` |
| `resource_account_get` | 单账号：账号/密码/OTP 明文、租约、机态、最近日志 |
| `resource_device_list` | 设备台账；`include_sessions` 带机态 App |
| `resource_log_list` | 资源分配日志 |
| `resource_env_get` | 环境/渠道/号池结构；只给 `configured` 和 `secret_slots` |
| `resource_account_reserve` | 预约账号；写 lease，立刻算 `occupied`；返回 `run_id` 和账号/密码/OTP 明文 |
| `resource_account_otp` | 统一取码；账号固定码 → 环境 fixed → Gmail → 人工；`requires_human=true` 时让用户读真实短信 |
| `resource_account_release` | 用 `account_id + run_id` 释放 |
| `logic_overview` | 页面/控件/入口/缺口覆盖度 |
| `logic_page_list` / `logic_page_get` | 页面列表/详情（控件、用例引用、入口、缺口；`include=reviews` 看评审摘要） |
| `logic_control_list` / `logic_control_get` | 控件列表/详情（完整 locator 只在详情） |
| `logic_entry_list` | `kind=entry\|gap\|summary` |
| `logic_flow_block_list` / `logic_flow_block_get` | 通用逻辑块目录/详情 |
| `logic_control_update` | 改控件定位；必须带 `expected_revision` |
| `logic_entry_extract` | 重抽入口/缺口；`project_id` 必须属于该应用；只回各来源计数，明细用 `logic_entry_list` |
| `logic_page_review_propose` | 提交页面评审候选；`expected_revision` 用页面 `knowledge_revision`；证据必须本人已结束 session；批准/驳回仍在 Workbench |

排障顺序：

1. 先 `resource_overview` / `logic_overview` 看是不是空项目、无设备、无页面。
2. 账号“明明空闲却约不到”：`resource_account_get` 看 `state=occupied` 与 `lease.run_id`；过期未释放仍算占用，先让持有者 `resource_account_release`。
3. 预约冲突：不要换 `run_id` 抢占；先读 `resource_account_list?state=occupied`，释放或换号。
4. 控件写冲突 `code=conflict`：重新 `logic_control_get` 取最新 `revision` 再写，不要盲目重试旧 revision。
5. `logic_entry_extract` 返回 422 `app_project_mismatch`：传的 `project_id` 不是应用所属项目，改用 `logic_overview` 的项目 id。
6. 环境里看不到密钥是设计行为；不要要求工具返回 `secrets`/`channel_secrets`，需要排障时去 Workbench 配置页核对。
