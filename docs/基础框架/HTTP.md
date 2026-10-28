# HTTP — Console / Studio 怎么连本仓

## 合并部署接口（2026-10-19）

正式 Workbench 与 Nexus 同源，HTTP 使用 `location.origin`，WebSocket 使用同源 `/ws`。`/node` 仍供 Scout 使用，协议不变。浏览器同源来源允许进入 WS 握手，未登录仍以 1008 拒绝；反向代理来源可用 `MINO_WORKBENCH_ORIGINS` 显式允许，CORS 默认只允许开发来源。

`/health`、`/packs`、`/flow-blocks` 保留为 API。对应 UI 页面为 `/admin/health`、`/admin/packs`、`/admin/flow-blocks`，防止浏览器刷新误取 API。`/static` 为上传资源，不被 SPA 覆盖；静态资源缺失返回 404。

正式地址 `http://服务器:50104/`；开发 Nexus 50101、Workbench Vite 60101。Nexus/Workbench 在线更新 HTTP/MCP 工具已移除，改为安装 wheel 或更新容器 tag；`/releases/scout/*` 与 Scout 安装能力保留。

## 会话执行版本证据（2026-10-18）

`GET /case-runner/sessions/{session_id}/execution-evidence` 只读取当前账号的历史会话。

`GET /case-runner/sessions/{session_id}/locator-chain` 只读定位链轨迹。按会话汇总 `hit/miss/ambiguous/skip/config`，附带 `runtime_context.locator_chain`（`enabled/platform/structural`）。命中回写草稿不经过此接口。android / web / mac / win 都先走结构链；树空记 `miss`（`structure_empty`），再看图兜底。Workbench 尚未做展示页，本仓只提供只读 API。
可选查询参数 `expected_instance_id`、`expected_nexus_build_id`、`expected_node_id`、`expected_build_id` 是测试计划中的预期值，不自动采用当前实际值。
实例不符返回 409（`instance_mismatch`），会话不可读或跨账号返回 404，未认证返回 401。
返回实际 Nexus 实例/build、Scout 节点/进程/build、设备、结果事件序号、源码提交与快照摘要以及可复制的引用。
`state=verified` 只代表该会话的执行版本与预期一致且用例通过；缺预期、旧回执、源码变化、未结束或读取截断均不能通过。旧 build/节点/进程不符返回 `state=failed`。
固定 `scope=session_execution`、`web_chain_verified=false`、`mcp_regression_verified=false`，不把局部执行证据升级成正式 Web 闭环或 MCP 回归结论。不建设跨 Nexus 代理或归档库，删除测试库后原引用不可读取。

开发准备绑定可选 `expected_instance_id`；`probe` 同时核对目标实例与 build，401/403 明确失败。实例/版本检查不执行用例。IDE 项目按应用保存的开发电脑、连接、项目路径明确路由，不再要求 `development_ide_projects` 样例登记，也不按 MINO 源码目录名拦截。项目与源码目录须在所选开发电脑上存在，源码须位于项目目录内。失败回执可携带 `reason_code`、`remediation`、`error_field`，供 Workbench 显示具体原因、处理办法并定位配置字段；保存、连通与项目核对是不同状态。

`check` 可带项目级 `runtime` 及 `support.inspect_running/restart/restart_reason`。启动前检测当前 PyCharm 管理的已有项目实例；返回 `reused_existing=true` 时仅复用并验证，不再次执行启动配置。`probe` 检测到运行实例时也返回 `reused_existing=true`，无需额外激活；未配置健康地址仍可使用该实例并返回进程状态，不据此声称 HTTP 或业务功能正常。Workbench 默认显示“已使用现有实例”，刷新操作展示排队/加载、更新时间与完成反馈。开发联动按真实工具选择：`support.restart_mode=run_control_mcp` 时用社区 MCP Run Control 与 PyCharm 原生 MCP 按项目/配置/sessionId 控制普通 Run 和 Debug；`debugger_mcp` 仅用于已确认的调试会话。已有 Debugger 连接可自动复用已保存且属于同一 PyCharm 的原生连接，不重复导入、不猜测端口。`action=stop` 与启动/重启使用同一权限、revision 和互斥。缺少普通 Run 控制工具时返回 `restart=false/run_control_required`；连接成功不能冒充可重启，普通 Run 不再要求手工先切换为 Debug。停止后确认进程实际退出，再启动同一配置；不可调试配置不再被 canDebug 阻断普通 Run 控制。新运行状态可含 `scope=configuration`、`session_id/debug_state`，超时只读恢复原会话，不通过终端停止或自动重发。Workbench 按已保存的当前 revision 检查、配置名与权限恢复配置阶段；全部完成显示详情及三个已完成步骤，运行状态独立显示。

UI **只**打 MinoNexus。没有公网域名时用内网名 `mino.local`：Nexus 进程启动后用 mDNS 注册，客户端不填 IP。

## 手动导入用例（2026-10-18）

### 工作区聊天附件（2026-10-21）

`POST /project/{project_id}/collab/workspaces/{topic_id}/attachments` 接收 multipart `file`。
PNG、JPEG、WebP 截图最大 8MB，校验解码后转存 PNG；普通文件最大 20MB，空文件拒绝。
上传只创建当前工作区的附件消息，不解析或导入用例。归档与历史工作区不可上传。
`GET .../{topic_id}/attachments/{ref}` 沿用项目与工作区归属校验；截图返回 `image/png`，
普通文件返回 `application/octet-stream` 和带原文件名的下载响应。旧截图保持可读。
Workbench 输入框附件按钮用于聊天文件；用例导入通过工作区顶部菜单「导入用例…」进入，
仍需明确核对并确认才写入用例。

Workbench 的项目用例页只保留「导入」，弹窗选择本地文件、飞书表格或用例接口。
独立来源配置页、定时读取循环、失败退避调度、自动解析和每日预算调度已删除。
所有远端读取均由用户点击触发，不创建来源配置或绑定，不自动改写用例。

- `GET /project/{project_id}/cases/import/sources`：返回本人在线且支持读取的节点
  `{items:[{id,label,kinds}]}`，单节点自动选择。要求项目维护权限。
- `POST /project/{project_id}/cases/import/read-source`：
  `{kind:"sheets"|"case_api",operation:"read"|"projects"|"sessions",configuration:{...}}`。
  认证会话决定 owner，Scout 必须属于该账号。复用现有 `node.source_read` 协议。
- 飞书配置 `{node_id,spreadsheet_url,range}`，链接需带工作表 `sheet` 参数，单元格范围
  默认 `A1:Z1000`，可修改。读取返回 `{table,filename}`。
- 用例接口项目发现只需 `{node_id}`，会话发现还需 `service_url/remote_project_id`。
  返回 `{items:[{id,label}],service_url}`；读取配置包含 `node_id/service_url/remote_project_id`、
  `scope=project|session`，会话范围还需 `session_id`，返回 `{table,filename}`。
- 读取后沿用字段核对、解析预览、冲突审阅和确认导入接口；未确认时不写用例。
  远端 `id` 以 `api-{remote_project_id}-{id}` 作为导入编号，避免跨项目冲突。
  `test_result` 等远端元数据只展示在原始表格中，不充当本地执行结论。

账号在「设置 → 节点 → 接入配置 → 工作项 → 用例服务 API」保存，密码交由 Scout
现有凭据管理保存。导入弹窗不接收密码、token 或任意 API 路径，Nexus 不直连外部服务。
服务地址在「管理 → 集成配置 → 用例服务 API」统一保存，默认 `http://192.168.22.9:3001/`。
`GET /settings/integrations/case-api` 需要登录，返回 `{base_url}`；`PUT` 接收同形 body，
仅管理员可写。地址校验为 http/https 根地址并去掉末尾斜杠，独立存入 SQLite
`settings.id=case-api-integration`，不包含邮箱、密码或 token，不改动其他系统设置。
Nexus 在每次发现项目、发现会话和读取用例时注入该地址；请求携带的旧地址与当前配置不一致
时拒绝读取，重新发现项目后可继续。Scout 仅在自己的已认证绑定中读取本节点账号，
使用 Nexus 下发的地址。节点不再配置服务地址；旧版本 Scout 需升级以使用统一管理的地址。
历史 `/project/{project_id}/case-sources` 接口保留手动兼容和历史查询，不再接受
`enabled/auto_compile/interval_seconds/daily_build_limit` 等调度参数。

## 导入时添加需求分组（2026-10-18）

`POST /project/{project_id}/requirements` 接收 `{app_id,title}`，要求当前项目维护权限并验证
应用属于该项目。仅向该应用现有的 SQLite `env.automation.qa_process.requirements` 追加
一个有 ID 和名称的分组，保留其他配置；不启动分析或模型调用。名称去首尾空白，长度
1–200，空名称拒绝；同一应用同名请求返回已有 ID，避免重试重复创建。

导入弹窗在没有需求分组时显示名称输入和「添加分组」，默认名称为「导入用例」。成功后
立即更新列表并选中新分组，继续选择文件或核对已选文件。失败时保留输入和文件状态。

开发 / 内网默认：`http://mino.local:10104`。可用 `VITE_NEXUS_URL` 覆盖。

## 登录

1. `GET /sys/server_info` —— 登录页 ping。连不上提示「无法连接服务器」。
2. `GET /auth/status` —— `{ logged_in, needs_setup, mail_configured, ws_token, ... }`
3. `POST /auth/login` body `{ username, email, password }`
4. 成功 `{ code: 200, data: { token, ws_token, user_id, role, ... } }`
5. 之后 HTTP `Authorization: Bearer <token>`，WS `/ws?token=<ws_token>`（与 token 相同）

首次启动若还没有账号，会写入本地初始账号：

| | |
|---|---|
| 用户名 | `admin` |
| 密码 | 环境变量 `MINO_BOOTSTRAP_PASSWORD`，缺省 `Mino@local` |
| 角色 | `admin` |

账号角色只有两种：`admin`（管理员）与 `user`（用户）。旧角色名会映射：`platform_admin`/`org_admin` → `admin`，其余 → `user`。

- **Console**（`X-Mino-Client: console`）：仅 `admin` 可登录；登录用账号密码。非管理员返回 403「仅管理员可登录控制台」。
- **Studio**（`X-Mino-Client: studio`）：邮箱登录 / 邮箱注册。注册**不**校验验证码，只查邮箱格式与是否已注册。

数据目录：`MINO_NEXUS_DATA_DIR`，否则 macOS `~/Library/Application Support/MinoNexus`。

## 客户端门禁

请求头 `X-Mino-Client: console|studio`。

| | Console | Studio |
|---|---|---|
| `/auth/users` 写 | 允许 | 允许 |
| `/settings/mail` 写 | 允许 | 允许 |
| `/packs` 写（builtin） | 允许 | 允许 |
| `/settings/plugins` 写 | 允许 | 允许 |
| `/settings/ai/providers` 写 | 403 | 允许 |
| `/runtime/nodes/install-token` | 403 | 允许 |
| `/runtime/nodes/{id}/command` | 403 | 允许 |
| `/runtime/nodes/{id}/plugins/*` | 403 | 允许 |
| `/project` `/app-automation` `/task` 写 | 403 | 允许 |
| `/nav-fsm` 写 | 403 | 允许 |
| `/case-runner` 写 | 403 | 允许 |

## Scout 安装

安装包在 **Scout GitHub Release**，不进本仓数据目录，也不要在服务器上维护 zip / dmg / exe。

发布清单由 Nexus 的 Scout 接入服务读取并用于确定实际可安装目标；Workbench 通过 Nexus 获取安装命令：

```
https://github.com/<owner>/MinoScout/releases/latest/download/manifest.json
```

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/runtime/nodes` | 当前账号归属的全部节点；管理员的已归属节点按本人 owner 过滤，另外可见未归属节点以便认领。`studio_id` 仅兼容、不授权。离线节点仍列出 |
| POST | `/runtime/nodes/install-token` | 签发登录账号的 15 分钟单机安装凭证；返回 `install_id`、`expires_at`、`nexus_url`、实际发布 `targets` 和各系统 `commands`；`Cache-Control: no-store` |
| GET | `/runtime/nodes/installations/{install_id}` | 仅签发账号可查询；不返回凭证。状态 `pending` / `expired` / `registered` / `revoked`，以及本次注册节点的名称和在线状态 |
| POST | `/runtime/nodes/{node_id}/revoke-token` | 当前 owner 吊销本人单节点长期凭证并断开该连接；其他节点不受影响 |
| PATCH | `/runtime/nodes/{node_id}` | 仅本人节点；拒绝转移 owner、共享节点或重新关联 studio，本轮不提供节点转移 |
| POST | `/runtime/nodes/{node_id}/command` | Studio 对已连接节点下发 `stop` / `restart` / `update` / `sleep` / `wake`。离线 409。`start` 400（进程已退出时不能远程拉起） |
| POST | `/runtime/nodes/{node_id}/plugins/config` | 把该节点的插件参数转到 Scout。body：`kind`、`plugin_id`、`values`、`clear`。Nexus 不落库。离线 409 |
| POST | `/runtime/nodes/{node_id}/plugins/install` | 让 Scout 按发布清单安装 CLI / MCP。body：`kind`、`plugin_id` |
| POST | `/runtime/nodes/{node_id}/plugins/remove` | 卸掉该节点上的插件并删除本机密钥 |
| GET | `/runtime/nodes/{node_id}/workload` | 合并 Scout `device_workload` 与 Nexus 在途 run（按 sn） |
| GET | `/runtime/nodes/{node_id}/logs?lines=` | `EXECUTE node.log_tail`（Scout ≥ 下一版含该指令） |
| POST | `/device/{sn}/ime/system` | Android 设备恢复系统输入法（关闭 ADB Keyboard） |
| GET | `/releases/scout/latest` | **客户端不用。** 兼容旧调用的可选代理；未设 `MINO_SCOUT_MANIFEST_URL` 时 404。不存文件 |

Console 不提供 Scout 安装或包配置。

## 还没搬的

HITL 问人界面、排期 cron、基线库、从设计稿/定位抽登录图标（CLIP/视觉）、完整 3500 行 Agent 恢复/拟人化路径。

已经可联：登录、概览、账号、发信、模型 Key、**技能**（`/settings/ai/skills`）与 **Jobs**（`/settings/ai/jobs`）prompt 编辑、角色 prompt / 对话、扩展包只读、节点列表、项目/应用/环境/号池、自动化配置与用例草稿、**Case Runner 下发与轮询**（无 Scout / 无 Key 时任务失败并带中文原因，不再 501）、qa-process tick/assist/脑图、**项目用例库导入 preview/commit**、Figma 同步（无 Token 时 400）。

## 项目用例库与导入

用例真源表 `project_cases`，唯一键 `(project_id, case_id)`。导入需先选 `requirement_id`，经 preview → commit；冲突逐条或批量选「覆盖 / 保留」。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/project/{project_id}/cases` | 项目用例列表。每条含 `step_keys_summary`（含 `ui_automation.coverable` / `blocked_by` / 逐步 `lights`） |
| GET | `/project/{project_id}/cases/{case_id}` | 单条用例 + `step_keys_summary`。`coverable=false` 时 Studio 选用例应禁用开跑 |
| GET | `/project/{project_id}/requirements` | 导入可选需求（项目内各 App 聚合） |
| POST | `/project/{project_id}/cases/preflight` | `{app_id,case_ids,recompile_stale?:false}`；默认只读，返回 `runnable` / `blocked`（不含 advisory）；启动入口传 true，以当前用例排队修复失效产物并返回 `compilation_jobs`，需项目维护权限 |
| POST | `/project/{project_id}/cases/import/preview` | 解析表格。每行：`steps_parsed` / `expected_parsed`（编译出的事件）、`remark`（不可覆盖的人话）、`ui_automation`。`flags` 仍是机器码。**不拦入库** |
| POST | `/project/{project_id}/cases/import/commit` | 写入；body 含稳定 `client_request_id`、`preview_token`、各行 `on_conflict`；同身份同输入复用回执，输入变化 409。`block_on_step_key_issues` 省略时用设置默认；旧预览调用可回退到 token 身份，直接 rows 提交必须提供身份 |
| GET | `/project/{project_id}/cases/import/receipts/by-request/{client_request_id}` | 按本账号/项目的提交身份只读查询；预览删除或过期不影响回执 |
| GET | `/project/{project_id}/cases/import/receipts/{receipt_id}` | 按回执 ID 查询原始行、逐行结果、汇总、`pending`、`receipt_state`；他人/不存在 404 |
| POST | `/project/{project_id}/cases/import/receipts/{receipt_id}/resume` | 明确续交待处理行；有效准备租约不重领，已写行不重放。失败/受阻行需新建预览，原始输入准备中断不自动重复解析 |
| DELETE | `/project/{project_id}/cases/{case_id}` | 删除单条用例 |
| POST | `/project/{project_id}/cases/delete` | 批量删除；body `{ case_ids: [] }` |
| POST | `/project/{project_id}/cases/{case_id}/resource-key/compile` | 从前置编译 `resource_key` |
| POST | `/project/{project_id}/cases/{case_id}/step-keys/compile` | 编译操作/预期 → `meta.step_program_keys` + `key_compile_warnings` |
| POST | `/project/{project_id}/cases/step-keys/validate` | 校验用例文案密钥（未知块/屏、fallback） |
| GET | `/settings/case-resource-key` | 词汇表；含 `console.entries_by_layer`（operation/expected 分组） |
| GET | `/settings/case-resource-key/registry` | 聚合 catalog + 逻辑块 + Nav 屏 `key_ref`（`?app_id=`） |
| GET / PUT | `/settings/case-import` | 导入策略：`block_on_step_key_issues_default`、`preview_batch_size`、`compile_regex_fallback`（默认 false，关闭后未命中 LLM 的语句不再走目录正则兜底） |
| GET/PUT | `/nav-fsm/{app_id}/screen-key-refs` | Nav 屏幕 `meta.key_ref` |

## 设备输入法（Android）

Nexus 不直连 adb；经在线 Scout 下发 `set_input_method`。设备未安装时 Scout 会尝试从 GitHub 下载 [ADB Keyboard](https://github.com/senzhk/ADBKeyboard) 并 `adb install`，再切换。Scout 进程每次启动 REGISTER 前也会走同一套逻辑。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/device/{sn}/ime/adbkeyboard` | 将该 sn 对应 Android 设备默认输入法切到 ADB Keyboard。设备离线或 Scout 无应答时 409/502 |
| POST | `/device/{sn}/ime/system` | 关闭 ADB Keyboard，切回其它已启用系统输入法（经 Scout `set_input_method` + `ime=system`） |

两条输入法接口共用 `device_ime._dispatch_set_input_method`，固定下发 `executor_order=["adb"]` 和 `selected_impl={"executor":"adb","runs_on":"scout"}`。管理操作即使 `run_id` 为空，也必须满足普通设备动作的唯一执行器合同；不得省略 `selected_impl.executor` 或让 Scout 自动补选。无可用系统输入法、设备离线和 Scout 执行失败仍返回实际错误，不视为切换成功。

旧 `POST /app-automation/qa-process/import/{app_id}` 的 `kind=cases` 已停用，请走上述 preview/commit。

## Jobs（LLM prompt 真源）

Console **Jobs** 页读写下列 API。Studio 写入口经 `client_gate` 拦截。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/settings/ai/jobs` | 列表（含 `system_blocks` / `user_blocks`） |
| GET | `/settings/ai/jobs/health` | 启动烟测：每条 enabled job 能否 render |
| GET | `/settings/ai/jobs/{id}` | 单条 |
| PUT | `/settings/ai/jobs/{id}` | 保存块或 `{ reset: true }` 恢复上一版；保留最近 5 次修订 |
| POST | `/settings/ai/jobs/{id}/preview` | dry-render（假槽 + 可选 flags） |

## 技能

技能是可编辑产品对象（角色 + SOP + prompt + 结果视图），与 `/packs` 分开。引擎 / 指针 / 视图只能选自已注册枚举。

扩展包 `GET/PUT/PATCH /packs`：列表项分开展示 `lifecycle`（`status` 仍只投影 lifecycle+enabled）、`enabled`、`ui_coverable`（可做/不可做）、`observe`（`exec` / `visual_each_run` / `program`）。不要合成一个下拉。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/settings/ai/skills` | 列表 + `enums`（engine / pointer / view） |
| GET | `/settings/ai/skills/{id}` | 单条。空 prompt / 坏 SOP 回退 builtin |
| POST | `/settings/ai/skills` | 新建。`engine` 必须是 `agent_loop` / `qa_job` / `chat` |
| PUT | `/settings/ai/skills/{id}` | 改 prompt / SOP / view。`{ reset: true }` 恢复 builtin prompt |
| GET | `/settings/ai/roles` | 仍返回产品角色；`skills` 字段已是技能表 |
| PUT | `/settings/ai/roles/{id}/prompt` | 兼容旧入口；技能 id 会写进 `skills` 表 |

## 文档库（PDF / Markdown 全文检索）

真源见 [DOC_LIBRARY.md](DOC_LIBRARY.md)。原始文件上传、分片、FTS 检索；**不是**下面的执行知识条。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/settings/docs` | 列表。`app_id` 必填 |
| POST | `/settings/docs/upload` | `multipart`：`file` + `app_id` + 可选 `project_id` / `title` |
| GET | `/settings/docs/search` | 全文检索。`q` + `app_id`；可选 `vector=1` 启用 query embedding |
| GET | `/settings/docs/{source_id}` | 元数据 |
| GET | `/settings/docs/{source_id}/chunks` | 分片列表 |
| DELETE | `/settings/docs/{source_id}` | 删除文档及索引 |
| POST | `/settings/docs/{source_id}/extract` | DOC_LEARN：抽取执行知识条（`review_status=pending`）。query：`app_id` / `project_id` |
| PATCH | `/settings/docs/{source_id}/sync` | 飞书文档定时同步开关。body：`auto_sync` / `sync_interval_sec`（≥300） |
| POST | `/settings/docs/{source_id}/sync-now` | 立即重拉飞书正文 |
| POST | `/settings/docs/sync-feishu` | 从飞书 wiki/docx 链接同步。body：`url` + `app_id` + 可选 `bot_id` |

## 执行知识库

短条口径落 `knowledge_entries`（手填 / 跑批沉淀 / 守卫 `wiki_ref`）。Console「能力 → 知识库」与 Studio 测试页「知识」共用 `/settings/knowledge*`。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/settings/knowledge` | 列表。可按 `app_id` / `account_id` / `account_ident` 过滤 |
| PUT | `/settings/knowledge` | 整表替换 `items` |
| PUT | `/settings/knowledge/{id}` | 新建或更新一条 |
| DELETE | `/settings/knowledge/{id}` | 删除 |
| POST | `/settings/knowledge/{id}/review` | `approve` / `reject` |
| GET | `/settings/knowledge/app/{app_id}` | 某应用的条目 |
| POST | `/settings/knowledge/append` | 给某应用追加一条 |

机审开关仍在 `settings.payload.knowledge_jobs`（按被测账号），**不要**写进知识表：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/settings/knowledge/jobs?account_id=&project_id=` | 读该应用登录账号的开关；不传账号返回旧缺省 + `accounts[]` |
| PUT / POST / PATCH | `/settings/knowledge/jobs` | body 必须带 `account_id` 或 `account_ident`，且能在号池里解析到 |
| POST | `/settings/knowledge/auto-review` | 机审待审。关开关时 `skipped`，否则先留人工 |
| POST | `/settings/knowledge/analyze-failure` | 失败分析入口，避免再 405 |

## 自检

```bash
pip install -e .    # 或 uv sync
```

## AppIntel（三渠道统一信息基座）

前缀 `/apps/{app_id}/intel`。设计稿见 [9月12日_AppIntel信息基座.md](9月12日_AppIntel信息基座.md)。

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/search` | 统一检索。`q` + `kinds=nav,knowledge,doc`；可选 `state_id` / `project_id` |
| POST | `/context-pack` | 跑批/调试用 context 编排。body：`intent` / `state_id` / `policy`（默认 `wiki_first`）/ `budgets` |
| GET | `/route` | Nav 路径规划。`from_state` + `to_state`；返回沿途 `wiki_along_route` |
| POST | `/ask` | 检索 + LLM 问答（须带 `citations`）。body：`question` |
| GET | `/graph` | 以 `center=nav:page.x` 为中心的子图（邻接边 + 挂接知识） |
| GET | `/gaps` | 缺口扫描：无 `wiki_ref` 的 state、空文档库等 |
| GET/POST | `/proposals` | 待审队列（`nav_patch` / `knowledge_draft` / `doc_learn_batch` 等） |
| POST | `/proposals/{id}/review` | 审核提案。body：`status`（`approved` / `rejected`） |
| GET/POST | `/links` | 跨渠道关联边（补充 `wiki_ref`，不替代 guards 内权威挂接） |

跑批每步由 `agent_loop` 调 `context_pack_for_step`（`wiki_first`），session log 事件 `intel/context_pack`。

## 通用逻辑块（FlowBlocks）

表 `nav_flow_block_catalog`（`app_id=__global__` 为通用库）；应用覆盖写在 NavFSM **draft** 的 `meta.flow_block_overrides`。**Console**「能力 → FSM 逻辑块」编辑全局库与应用覆盖。

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/flow-blocks/catalog` | 通用逻辑块列表（`?channel=web` 可筛） |
| `GET` | `/flow-blocks/catalog/{block_id}` | 单块（含 `steps_json`） |
| `PUT` | `/flow-blocks/catalog/{block_id}` | 创建/更新全局块（需 packs 写权限） |
| `GET` | `/flow-blocks/apps/{app_id}/overrides` | 读应用覆盖 |
| `PUT` | `/flow-blocks/apps/{app_id}/overrides` | 写应用覆盖（落 draft） |

`GET /nav-fsm/{app_id}/screen-atlas` 与 `live-graph` 响应中的 `doc.meta.flow_blocks` 已剔除 `fb.global.*`，避免架构图展示通用块。

## NavFSM（导航图配置与校准证据）

配置真源是 `mino.db` 的 `nav_fsm` / `nav_fsm_states` / `nav_fsm_edges`；校准证据是
`{data_dir}/nav/calibration/{app_id}/{calibration_id}/` 下的文件。方案见
[NAVIGATION_ATLAS.md](NAVIGATION_ATLAS.md)。

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/nav-fsm` | 列出已配导航图的应用（摘要） |
| `GET` | `/nav-fsm/{app_id}` | 取整份配置。额外回 `runtime_ready` / `runtime_reason` —— **草稿里带 `__CALIBRATE__` 不是错误**，那是等着校准后补录的部分，编辑口照常返回 |
| `PUT` | `/nav-fsm/{app_id}` | 整份替换。校验不过回 **422**（残留 `__CALIBRATE__`、边引用了不存在的 state、`project_id` 与 `apps` 不一致） |
| `DELETE` | `/nav-fsm/{app_id}` | 删一份（可带 `?version=`） |
| `GET` | `/nav-fsm/{app_id}/captures` | v2.5：被动采集 session 列表 |
| `GET` | `/nav-fsm/{app_id}/captures/{session_id}` | 该 session 的 turn 索引 |
| `GET` | `/nav-fsm/{app_id}/captures/{session_id}/turn/{turn_id}` | 单 turn hierarchy 样本 |
| `GET` | `/nav-fsm/{app_id}/calibration-report` | 采集报告 + 待审候选数 |
| `GET` | `/nav-fsm/{app_id}/candidates` | 当前候选包 |
| `POST` | `/nav-fsm/{app_id}/candidates/compile` | 从采集合成导航页并写入 `nav_fsm`（与应用逻辑页同一份页面） |
| `POST` | `/nav-fsm/{app_id}/candidates/{id}/review` | 接受/拒绝候选 |
| `POST` | `/nav-fsm/{app_id}/candidates/apply-to-draft` | 已接受候选合并进 draft |
| `POST` | `/nav-fsm/{app_id}/feedback` | 失败点反馈 → 定点候选 |
| `GET` | `/nav-fsm/{app_id}/calibration` | 【遗留】walkthrough 批次列表 |
| `GET` | `/nav-fsm/{app_id}/calibration/{calibration_id}` | manifest + walkthrough |
| `GET` | `/nav-fsm/{app_id}/calibration/{calibration_id}/step/{index}` | 单步 hierarchy 片段（正文可能很大，按需取） |
| `GET` | `/nav-fsm/{app_id}/template` | 产待校准骨架 JSON（**不落库**，可含 `__CALIBRATE__`） |
| `GET/PUT/DELETE` | `/nav-fsm/{app_id}/draft` | 半成品草稿（落在 `data_dir`，允许占位符） |
| `POST` | `/nav-fsm/{app_id}/draft/promote` | 草稿校验通过后写进 `nav_fsm*` 表 |
| `POST` | `/nav-fsm/{app_id}/calibration` | 【遗留】手动 walkthrough 批次 |
| `POST` | `/nav-fsm/{app_id}/calibration/{id}/step` | 【遗留】记一步 |
| `POST` | `/nav-fsm/{app_id}/calibration/{id}/finish` | 【遗留】收工 |
| `GET` | `/nav-fsm/{app_id}/metrics` | §9 指标（`guard_fp`/`guard_fn` 为 null 表示没样本） |
| `GET` | `/nav-fsm/{app_id}/reviews` | 待人工标注的 guard 事件 |
| `GET` | `/project/{project_id}/apps/{app_id}/understanding/pages/{state_id}/case-refs` | 页面详情用例引用：`enters` / `operates` / `asserts`，按同页面 `page_label` 聚合多个业务状态 |
| `POST` | `/nav-fsm/{app_id}/annotate` | QA 标注 fp/fn（body：`session_id`, `target_seq`, `verdict`） |

**Playbook 开关**（`apps` automation config）：

| 键 | 说明 |
|---|---|
| `observe_hierarchy` | 每 turn 拉 hierarchy **并被动采集**（env `MINO_OBSERVE_HIERARCHY=1`） |
| `nav_calibration` | 【遗留】walkthrough 高密度模式，新流程不必开 |

CLI 等价：`mino-nexus nav start|step|finish|draft-save|draft-promote|metrics …`

**runtime 与编辑口的门禁不同**：`GET` 给的是可编辑的原样内容，跑批走的是
`nav_fsm_store.load()` —— 后者还会校验校准账号与本次租号一致（否则 hierarchy 是别的
账号采的，`resource-id` 与控件态都可能不同）。所以「Studio 里能打开」不等于「跑批能用」，
看 `runtime_ready`。

## Workbench V4 增量接口

- `POST /auth/register`：邮箱、密码即可；固定普通用户且邮箱未验证，不影响登录。`/auth/send-code` 的注册用途返回 410，现有 Studio / Console / Workbench 已核对无发码调用。
- `GET /project/{project_id}/cases?limit=50&offset=0&app_id=...&search=...&module=...&result=all|failed|never`：分页摘要，不触发程序图编译。未指定 limit 时保留旧完整响应。app_id 必须属于当前项目，结果筛选必须提供 app_id。
- `GET /project/{project_id}/case-modules`：独立模块计数。用例详情仍由 `GET /project/{project_id}/cases/{case_id}` 读取。
- `GET /case-runner/tasks?app_id=...&limit=50&offset=0&summary=true`：仅任务摘要；不取完整轨迹。默认 summary=false 保留旧客户端响应。
- `POST /case-runner/case-results`：`{app_id,case_ids}`，最多 200 个当前项目用例，返回 `items` 按用例 id 映射的最新状态、原因与时间，覆盖全部保留历史，不限最近 20 个任务。
- `GET /runtime/nodes` 的 `im_channels` 与 `GET /assistant/status` 使用同一规范化渠道状态。助手状态只包含当前账号可见节点，逐渠道增加 `bound`，IM 和 `mcp_available` 分开聚合。
- `GET/PUT /assistant/profile` 只读写 `persona/enabled`。旧默认字段通过 `extra=ignore` 明确忽略；数据库旧列保留。

## Workbench V4 账号隔离

上述客户端门禁之外仍须校验会话归属。Scout 节点、我的助手、模型密钥三页及其关联操作对管理员采用同一 owner 边界；请求体中伪造 `user_id` 不改变服务端执行账号。其他账号及无归属资源统一返回 404，未登录返回 401。节点插件、日志、设备设置、安装记录与远程操作在访问前验证 owner；不同账号上报相同设备 SN 不转移原归属或继承配置。

| 方法 | 路径 | 账号边界 |
| --- | --- | --- |
| GET | `/settings/ai/providers` | 本人服务商配置、默认项和用途开关；Key 仅脱敏，新账号为未配置 |
| PUT / DELETE | `/settings/ai/providers/{provider_id}` | 仅保存或删除会话账号的配置 |
| POST | `/settings/ai/providers/{provider_id}/validate` | 使用本人已保存配置验证模型连接，实际模型调用也校验 owner |
| PUT | `/settings/ai/usage` | 本人模型用途开关 |
| GET | `/nodes`、`/devices`、`/capabilities`、`/runtime/*` | 必须登录，节点与设备按 owner 过滤 |
| WS | `/ws?token=<session_token>` | 必须认证；快照和推送按用户过滤，每次请求/推送复核 token，吊销后关闭旧连接 |

IM 回调由已认证 Scout 连接提供节点身份，校验实际机器人实例与持有权，再按 channel + instance + tenant + sender 解析本人绑定。绑定码仅本人节点可消费，限时且一次性；回调在执行前原子去重。历史消息必须同实例、租户、实际发送者且晚于当前绑定确认；每次模型调用、工具调用、通知与回复前复核绑定代次。解绑/重新绑定后旧请求、旧正文读取及旧通知失效。详情正文由 Scout 本地按 scope 现取，不在 Nexus 缓存。

IM 工具 `device.list`、`case.run`、`app.explore` 支持 `project_id/app_id/env_surface/env_profile`。渠道别名按项目解析，多个渠道时先明确渠道，再询问该渠道兼容且可执行的本人设备。消息中明确指名项目和渠道时，本轮先查询实时候选，不复用历史设备清单；下发保留 `env_surface` 并重新校验设备准入。设备摘要将 `status=online/offline` 与 `desktop_state=ready` 分开显示，后者只表示“桌面就绪”。

Scout `REGISTER.account_isolation_version=1` 才可接入；旧客户端在安装码消费前拒绝并提示升级。`REGISTERED.owner_user_id` 确认本地插件命名空间，不能用新账号或新服务继承旧凭据。


## V5 执行绑定预检

`GET /project/{project_id}/cases/{case_id}/v5/execution-preview` 要求认证，查询参数为 `app_id`、`sn`，可选 `env_profile`、`env_surface`。用例必须存在已发布计划，应用必须属于同一项目，设备必须属于当前账号且与应用渠道兼容。

成功信封中的 `data` 包含 `status=ready|blocked|needs_input|not_applicable`、`binding` 和逐操作 `diagnostics`；固定返回 `scope=execution_binding_only`、`runnable_now=false`。`ready` 只表示当前快照可解析执行绑定，不表示资源已租用、页面基线或业务用例已经可运行。接口不创建 session、预约或绑定事件；真实 attempt 重新解析并原子写入 `execution/bound` 和绑定上下文，不能直接复用预检结果。未发布计划返回 409，目标配置输入错误返回 400；跨账号项目/设备沿用 404。

绑定使用验证过的节点连接代际、Scout 进程代际和能力修订，不按 Scout 版本字符串推断支持。旧节点未声明 `execution_binding_version=1` 时返回阻断诊断。严格 Web 的 `web.strict.v1` 为显式版本适配；业务原子仍按目录的 enabled、lifecycle、caller、phases 和实现合同约束，接口不会自动改业务目录。控件 `/probe` 使用独立的项目配置绑定，仍不产生用例通过结论。旧普通合同保持原行为；普通 runner 全面迁移仍待后续。

## V4 Desktop 选择与管理员入口

| 方法 | 路径 | 行为 |
|---|---|---|
| GET | `/case-runner/devices` | 默认 `only_online=false`、`compatible_only=false`，保留账号内离线/未就绪设备。可传 `app_id/env_surface/env_profile/action_scheme/playwright_headless`；逐行返回 `executable/blocking_code/blocked_reason`。`compatible_only=true` 且指定应用时只保留兼容该渠道的设备，未就绪原因仍可展示。原生 macOS/Windows/Linux 渠道要求同 OS Desktop；网站匹配 Web 槽。 |
| POST | `/case-runner/run`、`/case-runner/explore` | 服务端再次核对 owner、Scout 事实与应用兼容性，在 SQLite 原子获取资源锁后建任务。忽略客户端平台对执行路由的影响；阻塞返回 409（错误详情含 sn、blocking_code、message），他人资源返回 404。 |
| POST | `/runtime/nodes/{node_id}/claim` | 管理员将无归属历史节点认领到当前账号并关闭旧连接；普通用户 403，已有归属 409。长期凭证同步归属，下次重连仍可用。 |
| GET | `/packs/desktop-paths` | 旧客户端兼容入口：返回空 changes 和 configuration_required=false；Desktop 工具从当前 Scout 设备清单生成，不写库。 |
| POST | `/packs/desktop-paths` | 旧客户端兼容入口：返回空 configured 和 configuration_required=false；不修改目录，不要求管理员角色。 |

`GET /runtime/nodes` 的每个节点新增宿主 `host` 摘要；设备保持 `platform/os/session_key/session_generation/desktop_state/capabilities/readiness`。管理员可查看未归属节点的宿主状态以便认领；已归属节点与任务设备仍按账号隔离。离线行不丢失这些字段。

模型供应商的 `GET /settings/ai/providers` 与 `PUT /settings/ai/providers/{id}` 支持 `pc_compress_ratio`：默认 1，取值 1–10，禁止非有限数。字段仍按当前执行账号隔离存储，不新增共享密钥或配置表。旧客户端省略该字段时保留现有 PC 设置。Desktop 采集使用该比例，模型输入不再叠加 Plan 压缩；返回的捕获原始尺寸与 capture 引用仍用于坐标映射。

## 项目维护者权限（2026-10-15）

项目写接口要求 **管理员、项目创建者或 `project_members` 中的维护者**；读接口不变。非维护者返回 403，`detail = {reason_code: "project_maintainer_required", message}`。存量项目由 `projects.created_by` 隐式成为维护者，无需数据迁移。路径段填 app id 的兼容入口按所属项目判定；`/nav-fsm`、`/app-automation`、`/flow-blocks/apps`、`/apps/{app_id}/intel` 的持久写按 app 所属项目判定，app 与项目都查不到时交给原处理函数。

受保护：项目环境、应用创建/删除/环境、账号池与模板、登录态改写、分配日志恢复、删除项目；用例保存/删除、导入提交与续交、步骤与资源键编译、待补充、控件、v5 基线/编译/发布；导航图草稿/发布/编辑、应用自动化配置与脑图发布、逻辑块应用覆盖、AppIntel 审核与关联。不受保护：创建项目、导入预览/取消/重试、校验、控件探测、账号分配/挑选、资源试跑、导航路由计算与校准。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/project/{project_id}/maintainers` | 任何登录账号可读；返回 `items[{user_id,name,email,source: creator\|member,added_by,added_at}]`、`can_edit`、`can_manage` |
| POST | `/project/{project_id}/maintainers` | 仅管理员；body `{user_id}`，账号不存在 400 `user_not_found` |
| DELETE | `/project/{project_id}/maintainers/{user_id}` | 仅管理员；创建者不可移除（400 `creator_is_implicit_maintainer`） |

Workbench 入口：项目配置 → 「项目维护者」。

## MCP 安全读模型与预约（2026-10-27）

前缀 `/project/{project_id}/resources`；应用逻辑读模型在 `/project/{project_id}/apps/{app_id}/logic` 与 `/project/{project_id}/logic`。这些路由是为 MCP 新加的返回字段受控模型；账号接口返回明文账号密码，日志/环境/设备仍脱敏。不登记 Workbench 现有的账号/环境/设备/控件路由（MCP token 的 HTTP 白名单是路由级，登记旧路由等于放行明文）。读接口要求登录；写接口沿用 `project_maintainer`。详细设计与工具表见 [测试资源与应用逻辑的 MCP 暴露方案](../架构/V8/测试资源与应用逻辑的MCP暴露方案-20261027.md)。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/resources/overview` | 账号/设备/租约计数、最近资源日志、告警；不含 ident |
| GET | `/resources/accounts` | 账号列表；`env`、`state`（`free`/`occupied`/`locked`）、`lifecycle`、`q`、`limit≤200`、`offset`；返回账号/密码/OTP/手机号/邮箱明文、租约、`has_password/has_otp` |
| GET | `/resources/accounts/{account_id}` | 账号详情 + 租约 + 机态 + 最近日志；账号/密码/OTP 明文，日志 `detail` 递归脱敏 |
| POST | `/resources/accounts/{account_id}/otp` | 统一取码；body `{env_profile?, env_surface?, node_id?, sn?, since_ts?, max_wait_ms?, ...}`；返回 `code/source/requires_human/hint`，固定码优先，Gmail 其次，真实短信人工兜底 |
| GET | `/resources/devices` | 设备台账；`include_sessions=true` 时带最多 5 条机态（掩码 `identity_hint`） |
| GET | `/resources/logs` | 资源分配日志；`account_ident` 掩码、`detail` 递归脱敏、`page_size≤100` |
| GET | `/resources/env` | 环境/渠道/号池结构；只返回 `configured` 与 `secret_slots` 名称，不返回密钥 |
| POST | `/resources/accounts/reserve` | MCP 预约；body `{account_id?, env?, lifecycle?, run_id?, ttl_sec?}`；写号池 lease，账号立刻算 `occupied`；返回账号/密码/OTP 明文 |
| POST | `/resources/accounts/{account_id}/release` | body `{run_id}`；必须与当前 lease 一致，否则 409 |
| GET | `/apps/{app_id}/logic/overview` | 页面/控件/入口/缺口覆盖度 |
| GET | `/apps/{app_id}/logic/pages` | 页面列表；`q`、`status`、`limit≤200`、`offset` |
| GET | `/apps/{app_id}/logic/pages/{state_id}` | 页面详情；`include=controls,case_refs,entry,gaps,reviews`，控件≤100、引用≤50、评审≤20 |
| GET | `/logic/controls` | 控件列表；`app_id`、`page_state_id`、`semantic_role`、`sensitive`、`q`、`limit≤200` |
| GET | `/logic/controls/{control_id}` | 控件全量 locator + 版本历史（≤20） |
| GET | `/apps/{app_id}/logic/entries` | 入口/缺口/摘要；`kind=entry\|gap\|summary`、`target`、`status`、`platform`、`limit≤200` |
| POST | `/apps/{app_id}/logic/entries/extract` | 重抽入口/缺口；body `{platform?, case_ids?, include_cases?, include_capture?}`；只回各来源计数，明细用 GET entries |
| POST | `/apps/{app_id}/logic/pages/{state_id}/review-proposals` | 提交页面评审候选；body 同 `Proposal`；只回候选摘要，批准/驳回仍在 Workbench |
| POST | `/apps/{app_id}/logic/pages/{state_id}/review-proposals` | 提交页面评审候选；body 同 `Proposal`；只回候选摘要，批准/驳回仍在 Workbench `understanding/.../reviews/{review_id}` |
| GET | `/logic/flow-blocks` | 流程块目录；只返回块定义、步骤数、用例引用，不返回步骤正文 |
| GET | `/logic/flow-blocks/{block_id}` | 流程块详情；最多 100 步，凭证键丢弃、自由文本掩码 |

注意：`/resources/accounts` 与 `/resources/env` **不是** Workbench 旧接口的别名；旧 `/project/{id}/accounts`、`/project/{id}/env` 仍可能返回明文/密钥，禁止登记到 MCP 工具表。`logic.control.update` 复用 `PUT /project/{id}/controls/{control_id}`（新增 `platform`）；`logic.entry.extract` 走新的 `POST /project/{id}/apps/{app_id}/logic/entries/extract`，先校验 app 归属、只回计数。旧 `POST /nav-fsm/{app_id}/entries/extract` 仍服务 Workbench，但不登记 MCP。页面评审的 `decide` 也未登记 MCP，避免模型自批。

## Mino Bots 协作（2026-10-19）

前缀 `/project/{project_id}/collab`。写接口需项目维护者（与上节一致）。执行类工作项在创建时固化 `holder_user_id`，仅持有人可触发。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/templates` | Bot 模板（登录可读） |
| GET | `/marketplace` | 市场条目（本地 YAML + 内置；可选 env `MINO_COLLAB_MARKETPLACE_URL` 合并远程） |
| GET/POST | `/bots`、`/bots/ensure-defaults`、`/bots/install` | 项目 Bot |
| GET/PUT | `/policy` | 项目自动化策略（自动写草稿/编译/执行等；可选 `app_id`） |
| GET/POST | `/topics`、`PATCH /topics/{id}` | 议题 |
| POST | `/topics/{id}/pause` / `/resume` / `/close` | 暂停 / 继续 / 结束议题 |
| GET | `/topics/{id}/digest` | 议题摘要 JSON |
| POST | `/topics/from-case` | body `{app_id, case_id}`，锚定用例议题；worker 自动挂 `case_run` |
| GET/POST | `/topics/{id}/messages` | 讨论（含 IM 镜像 `author_kind=im`） |
| GET | `/topics/{id}/work-items` | 工作项 |
| POST | `/topics/{id}/decompose` | 拆解；`bump_version`、`include_case_run` |
| POST | `/topics/{id}/advance` | **推荐** 一键推进：可选 message/goal、同步记忆、跑流水线（默认 `engine=langgraph`，与 qa_cover 同链；`agentscope` 为 Team 实验路径且失败自动回退 LangGraph），返回 `hint` 与 `next_steps` |
| GET | `/topics/{id}/agent-canvas` | 多 Agent 画布（nodes/edges/position、mermaid、transcript），议题 `agent_canvas` 同步返回；Workbench 默认用 @xyflow/react 渲染 |
| GET | `/openhands/status` | OpenHands 是否配置、是否可达 |
| PUT | `/openhands/config` | body `{api_url}` 写入 `settings.integrations.openhands`（环境变量已设时 409） |
| POST | `/topics/{id}/pipeline/run` | 交付流水线 → 工作项；body `engine`: `agentscope`（默认，TeamPipeline+qa_cover）或 `langgraph` |
| POST | `/topics/{id}/knowledge/refresh` | `propose_atlas` + 可选 AppIntel `atlas_patch` 提案，写入 `knowledge_graph` |
| POST | `/topics/{id}/memory/refresh` | LLM 摘要写入 `memory` |
| POST | `/topics/{id}/im/bind` | body `{channel, chat_id, node_id?, label?}` |
| POST | `/work-items/{id}/transfer` | 交接执行账号 |
| GET | `/work-items/{id}/approvals` | 工作项审批单 |
| POST | `/work-items/{id}/approvals/{approval_id}/decide` | body `{decision: approve\|reject, comment?}`；持有人不可自批 |
| POST | `/work-items/{id}/trigger` | 立即排队；body 仅 `force_retry` |
| POST | `/work-items/{id}/redo` | body `{feedback?}`，重做并带人工反馈 |
| POST | `/work-items/{id}/answer` | body `{fact}`，解除 blocked 项缺事实 |

后台 `collab_worker` 消化 `pending` 工作项；`case_run` / `compile` 提交后进入 `waiting`，由 `run_store` 终态写 `evidence`。不再使用 `confirm` / `execute_cases` 门禁。

讨论 `POST .../messages` 的 `orchestration` 字段：`{action, reply, affected_items}`（快路径 + `collab_intent` 模型 job）。`GET .../digest` 与 WebSocket `testing_task` 事件 `collab_notify` 在失败/阻塞/全部完成时推送摘要。

`action_kind`：`llm_job`、`apply_drafts`、`compile`、`publish`、`case_run`、`diagnose`、`defect`（及遗留 `v5_publish` / `qa_tick`）。完成只认证据字段 `evidence` / `run_id`。

**P3 策略持有者**：`PUT /policy` 的 `auto_publish=true` 时，流水线在 `compile` 后插入 `publish`，`case_run` 依赖发布结果；`publish` 成功证据含 `v5_plan_state`。`POST/PATCH /bots` 可设 `platforms`（如 `["android"]`）与 `policy_override`；worker 按 `topic.app_id` 匹配 Bot，**不在 platforms 内的用例不会被编译/执行/发布**。`PATCH /bots/{bot_id}` 可更新作用域。

协作 OpenHands：`PUT .../openhands/config` 或环境变量 `OPENHANDS_API_URL`（优先于库内配置）；`MINO_COLLAB_MARKETPLACE_URL`（远程 Bot 市场 JSON）；`MINO_COLLAB_AGENTS_SCOPE_LANGGRAPH_ONLY=1`（强制 LangGraph，不走 TeamPipeline）；`MINO_COLLAB_AGENTS_SCOPE_TEAM=1`（仅在 LangGraph 回退时额外跑一轮协调者摘要，一般不必开）。

### V5 应用平台新写入边界

`POST /project/app/create` 的 `platforms` 仍为逗号分隔字符串，成员严格限制 `mac / win / android / web`；重复值、旧别名（Mobile/pc/desktop/iOS 等）返回 422。空字符串表示未选择；不会自动解释成全平台或补出 Android。服务层更新应用 platforms 使用同一校验。历史读取暂保留原标签，数据转换必须走审阅迁移，不通过读接口隐式重写。本节仅说明应用写入口，不代表环境、导入、恢复的 N4 全量切换已完成。

## 普通/严格执行绑定预检（2026-10-15）

`GET /project/{project_id}/cases/{case_id}/execution-preview`，保留 `/v5/execution-preview` 别名。必需 `app_id`、`sn`；可选 `env_profile`、`env_surface`、`action_scheme`、`provider_id`、`execution_mode=legacy|v5`（默认 v5 兼容旧入口）。Workbench 新建任务显式传当前模式。

只读返回 `status`、`binding`（仅 ready）、`diagnostics`、`scope=execution_binding_only`、`runnable_now=false`。不写 session、不触发旧环境懒迁移、不租号/预约、不操作设备、不调用模型。项目读权限保持现有共享规则，设备与模型配置仍限定认证账号。编译缺失/原文变化返回 needs_input；离线、隔离、策略拒绝等返回 blocked。诊断包含来源和恢复入口；可选分支有 `blocking:false`。ready 仅表示当前绑定资格，启动时重新解析，不承诺队列许可或业务通过。

Workbench 的“预检所选用例”按当前完整选择逐条调用；选择设备/环境/用例/方案/模式改变后原结果标为过期，不替用户创建任务。


### V5 导入平台与历史任务重跑边界

用例导入直接平台值限 mac/win/android/web；环境通道 ID、label 或 alias 仅允许精确唯一匹配并解析到通道的四值 platform。server、旧别名、歧义与未知值在预览行返回 platform_error，parse_status=invalid_platform，selected_by_default=false；不进入模型编译。提交和导入回执续交再次校验，失败以逐行 blocked + reason 返回，不跳过或默认 Android。

失败任务重跑要求当前认证 owner 与任务 owner 一致，所选 SN 在当前登记中属于当前账号且具有四值平台。无法确认的旧身份返回 409 并要求重选；不会按 SN 前缀猜平台或迁移历史任务。该检查不代替实际开跑的资源隔离、占用、节点在线和应用兼容性准入。


### V5 项目环境新写入合同

`PUT /project/{project_id}/env` 的渠道在规范化之前校验：目标 platform 为 mac/win/android/web，kind 分别为 app/app/app/web；kind=server 时省略 platform 字段。无效值、kind 冲突、重复/非法 ID 返回 422，配置不写入。渠道 ID 可含字母、数字、下划线、点号、连字符，长度 1–64；不要求等于 platform，不自动改名。旧配置仍可读，保存前需先按审阅方案迁移。

部分请求未提供 profiles/channels/pipeline 时保留原值，不能因只修改默认环境清空其他环境。旧 `PUT /project/app/{app_id}/env` 只接受已登记渠道的配置块，合并到 test，拒绝未知渠道与 channels/profiles/environments 结构改写（422）；渠道平台改动使用项目环境入口。

## V5 持久化编译与待审闭环（2026-10-15）

以下接口均要求认证；项目原文和任务摘要沿用共享读取，新增写接口复用现有管理员/项目创建者/显式维护者权限。模型调用始终使用持久 job 的发起账号；完整单 job 产物仅其 owner 可读取，目标设备与 session 证据仍按当前账号隔离。GET 不领取、不恢复、不编译。

| 方法/项目用例路径 | 输入与结果 |
| --- | --- |
| `GET /project/{pid}/cases/{cid}/version-chain` | 当前原文、最新 job、有效 artifact、publication 与实际 revision/job 历史 |
| `GET .../compile-jobs/{job_id}` | 单个任务状态、产物、诊断、真实 provenance；仅本账号/项目/用例 |
| `POST .../v5/publish` | `{expected_revision,expected_compile_job_id,reviewed_expected_strength,retry_policy?,reversible?}`；已登记版本链必须携带审阅时 preview 的 `compile_job_id`。同一原文重编后旧 job 或缺 job 均返回 409 `compile_revision_conflict`，须刷新并重新审阅；没有版本链的历史用例保持原接口兼容 |
| `POST .../compile-jobs` | `{expected_revision,client_request_id}`；排队同版本重编，有在途任务时复用；返回 job，不等待模型 |
| `POST .../compile-jobs/{job_id}/cancel` | `{expected_revision}`；取消激活资格；不能保证远程模型调用已停止 |
| `POST .../compile-jobs/{job_id}/recover` | `{expected_revision,client_request_id}`；仅最新 failed/interrupted/cancelled 任务可显式创建关联新 job；可能再次调用模型 |
| `GET .../review-requirements` | 当前 source/revision/job，编译、发布、断言/资源及已检查目标的诊断；来源、六类建议根因或根因待确认、缺项、point/operation、动作、逐项历史 |
| `PUT .../review-requirements` | 原有 `{expected_revision,source:{precondition,steps_raw,expected_raw},reason}`；原文与新 revision+queued job 原子落盘，不同步编译 |
| `POST .../review-requirements/{item_id}/review` | `{expected_revision,expected_job_id,reason,category?}`；category 允许 case_definition/semantic_parsing/capability_gap/configuration/execution_toolchain/external_evidence，null 表示暂不定责；只登记人工意见，不改变用例执行资格 |
| `POST .../review-requirements/recheck` | `{expected_revision,expected_job_id,reason,context?,evidence_session_id?}`；context 为当前项目应用及 owned 设备 `{app_id,sn,env_profile,env_surface,execution_mode?,action_scheme?,provider_id?}`。实际重算静态资格、目标绑定和资源登记检查，不租设备、不调用执行器。目标诊断按上下文保存，省略目标不能清除原目标问题；观察超过 60 秒或版本改变标过时 |

原有 `POST .../v5/compile`、`.../step-keys/compile`、`.../resource-key/compile` 统一进入队列，仍返回 `{case,job,preview}`。新客户端发送上述稳定请求身份；兼容无 body 的旧入口可复用在途工作，不能把 HTTP 返回当编译已完成。保存/导入不再被模型时延阻塞；预览的解析仍是可丢弃候选，确认导入保留原文并以持久任务报告编译缺口。

执行入口（助手、Workbench 启动及 Case Runner）重新读取用例，再校验原文摘要、编译/资源目录合同及执行绑定合同。产物缺失或失效时复用 queued/running 或按原文版本与当前构建依赖生成幂等重编任务；不把旧预检结果作为执行许可。编译未就绪、能力缺口或页面入口缺口不再作为拒绝整案的理由：重编回执进入 `compilation_jobs`，未就绪的步骤/断言在执行时交给意图识别与入口调度，风险随任务记录。只有用例本身不可执行（空选择、已删除、不属于当前项目）才在创建任务前拒绝。真实语义阻塞不触发重编，failed/interrupted/cancelled 不自动重放；仅模型调用前检测到 `compile_input_or_build_changed` 的失败可按新的构建身份排队，重复请求不重复调用。列表、详情及默认 preflight 不触发编译；无关 Nexus 发布版本变化不会单独使成功产物失效。

来源/任务变化或恢复父任务不一致返回 409；无项目维护权限返回 403，不存在或非本账号的完整 job/设备/session 资源返回 404 或证据不匹配；复核理由/分类/证据不合约返回 400。queued/running/failed/interrupted/cancelled 最新版本均不能悄悄使用旧 artifact 或旧 publication 执行。发布快照保存 source_revision_id/source_revision/compile_job_id/effective_artifact，并在提交时再次比较引用。


### 2026-10-16：MCP 节点更新、写安全复核与多语言证据

- MCP `scout_list` / `scout_update` / `scout_update_status` 复用当前 owner 的节点接口。`scout_update` 只请求节点已配置发布清单的最新版本，不接受任意本地包路径、任意 URL 或指定未发布版本；离线不能宣称升级成功，须通过状态和重新连接核对运行版本。本地未发布修复包的备份安装另记交付记录。
- `POST /case-runner/tasks/{task_id}/write-safety/recheck`（MCP `task_write_safety_recheck`）：仅当前 owner 的已结束任务可显式复核；其他账号任务 404、在途任务 409。只重新获取 Scout 停止证明，不重放原设备写动作。证明缺失仍 `resource_isolated=true`；成功返回逐动作 `recovered`、`mode`、`reason_code`、`baseline_required`，并明确 `business_result_unchanged=true`。跨进程必须有旧代际退役证明，新进程在线本身不是停止证明。停止证明不等于资源复位：当前只更新停止状态并保留隔离，历史失败或阻断不改成通过。迟到的有效停止回执也会更新已结束任务的当前停止状态；普通迟到写回执仅追加对账。
- 文案检查点冻结 `rules.text_match=exact|intent`。作者指定固定文案仍精确检查；明确跨语言意图按原用例含义核验，保留 `observed_text`、`observed_language`、`evidence` 与逐项 `semantic_checks`。新策略通过新编译/草稿生效，不改写历史发布。缺原文、缺逐项证据、输出语言标签非法等仍拒绝，不能退化成普通布尔断言。
- 单次观察回看可证明某个语言检查点符合，不自动回写整条业务用例结果。身份、协议、交互能力、完整 check 和严格 `verified_pass` 仍各自按合同核验。


## 应用登录校验配置（2026-10-16）

`GET /app-automation/login-evidence-policy/{app_id}` 读取当前应用的非凭据配置。
`PUT /app-automation/login-evidence-policy/{app_id}` 需要应用维护权限，body 为 `policy` 与读取到的 `expected_policy`；当前值改变时返回 409，非法配置返回 422。该接口只合并 `app.env.automation.playbook.login_evidence_policy`，不返回或修改其他登录凭据、OTP 或 playbook 字段。

- `mode=ui_identity`：默认要求页面可见的账号身份与租号一致。
- `mode=submitted_account`：用于应用没有身份展示入口的业务设计。必须显式指定非空 `env_profiles` 与 `env_surfaces`，仅匹配范围启用；其他应用/环境/渠道保持默认。
- `submitted_account` 仍要求本作用域实际填写的账号与租约一致且输入结果已核验、真实提交动作成功回执，以及提交后当前截图的已登录正向证据。配置值、租约、输入完成、表单消失均不能单独证明已登录。页面 `identity` 保持 `unknown`，账号绑定来源另记 `submitted_account`；明确 mismatch 必须阻断。
- 配置从数据库应用配置读取，随新任务快照，不向业务代码写入应用 ID、包名、特定账号或该应用的身份展示规则。Workbench：应用 → 配置 → 登录校验。
- MCP 对应 `app_login_evidence_policy_get` / `app_login_evidence_policy_set`。保存需要稳定请求标识并使用正式维护权限；该接口不启动任务、不改变原判定。


### 登录专属页面说明与多语言完整意图

登录校验配置另支持 `authenticated_content_hint`（最多1200字，默认空串），用于描述应用维护者确认的登录后专属页面特征。该说明仅进入配置范围 `env_profiles` / `env_surfaces` 匹配的新任务；实际当前截图必须命中完整特征，配置存在本身不等于登录成功。它与账号来源独立，不能替代实际账号填写、提交回执或可见身份核验。Web 首次引导遮挡身份时，可显式配置 submitted_account，而不是修改代码或伪造 UI identity=match。说明通过 Workbench「登录专属页面说明」或 MCP policy 字段维护。

多语言 intent 检查要求完整含义一致：泛化欢迎/入门标题不能替代具体问题，单个示例尝试不能替代兴趣收集与多选推荐；主题接近不能当成等义。agent-vision-assert/assert-vision v9、agent-text-assert v4；inspect-session v6。实际不符保留 fail，不把不同语言本身当成失败原因。

## 旧安装更新接口已移除

原 `/sys/updates/*`、`/sys/workbench/*` 及对应 MCP 更新工具随 B5 移除。Nexus/Workbench 改由 wheel 或 Docker 统一升级，不在服务进程内下载、停写、切换和回滚。历史 D2/D3 资料仅供旧安装迁移参考，不能用来调用新版本接口。

`/sys/server_info` 保留实例与版本信息，`deployment_managed=false` 表示没有内置维护助手；专用 probe token 仍仅允许 MCP 的 initialize/ping/tools/list。Scout 下载和安装入口保持原合同。

## 协作消息快速接收（2026-10-19）

POST /project/{project_id}/collab/topics/{topic_id}/messages 保持 200 信封，消息和回复任务在同一事务提交后即返回消息及 reply_status=queued；不等待模型回复或命令编排完成。GET messages 返回 reply_status/reply_error（历史消息为空）。后台按序处理并重新核对账号及维护权限；完成后经当前账号 WebSocket collab_message_updated 通知，前端有待回复项时每秒兜底查询。queued 重启后继续；已开始但因重启中断的处理标 interrupted，不自动重放可能产生副作用的命令。正在处理回复时拒绝删除议题，防止删除与回复写入竞态。

## IM 调用 Scout CLI（2026-10-21）

助手新增 `cli.query`（read）和 `cli.execute`（write）两个通用入口；不逐个登记 CLI 子命令，不新增账号/动作授权设置。复用现有 IM 绑定、节点归属、群聊启用/执行权限；CLI 使用节点的外部服务，不额外要求选择 Mino 项目，原有 Mino 项目工具仍检查项目范围。写操作沿用请求回执去重。默认使用当前 IM 会话的 Scout，没有会话节点时选本人唯一已配置的在线节点；多个候选必须传 `node_id`。

- `POST /runtime/cli/query`：固定以只读模式调用现有 `plugin.cli.feishu` / `plugin.cli.meego` 能力，不允许创建、修改。
- `POST /runtime/cli/execute`：固定以写模式执行。body 同查询：`{plugin: "feishu"|"meego", node_id?: string, command: string[], options?: object}`；`command` 不含程序名，长参数在 `options` 中不带 `--`。模型无法用请求参数改写读写模式。
- HTTP 继续要求现有账号认证；IM 的绑定和群策略在助手入口复核。执行只到该账号可用的 Scout，通过插件固定入口和参数数组运行，不启动任意 Shell，不开放删除、CLI 登录/配置、任意本地文件参数。飞书使用该插件的扫码授权，不借用操作系统默认账号；Meego 原生命令使用独立 HOME/profile。
- 成功回执保留业务 ID，IM 可展示查询内容与创建结果；`dry-run=true` 只预览，不表示已创建。写调用失败/超时保守记为结果未知，不自动重放，先查询第三方业务状态。

飞书原生命令要求该 Scout 已完成飞书 CLI 扫码授权。Meego 现有 Plugin ID / Plugin Secret / User Key 为插件 OpenAPI 认证，不等于官方 CLI OAuth 用户登录：已配置插件凭据时兼容 `workitem meta-types`、`meta-fields`、`meta-templates`、`get`、`create`，其中查询工作项需明确实际类型；创建先查询目标空间的工作项类型、字段和模板，不猜测 `bug` 类型或字段/模板 ID。`workitem create` 使用 `POST /open_api/{project_key}/work_item/create`，仅收到工作项 ID 才报告创建成功。其他 Meego 原生命令仍要求该节点独立 CLI 用户授权，授权缺失时如实返回失败，不使用其他账号兜底。

### Meego 空间发现、追问与错误（2026-10-21）

`cli.query` 的插件凭据模式增加 `project list`（`POST /open_api/projects`）和 `project get`（`POST /open_api/projects/detail`）。用户身份 `user_key` 从当前 Scout 的 Meego 配置读取并放在请求体中；详情支持 `options.project-keys` / `options.simple-names` 字符串数组。空间链接中的别名不直接作为已验证的 `project_key`：先查详情，使用返回的真实 key。缺少 User Key 时提示在 Scout 配置补齐，不在聊天中收集密钥。

缺少必要业务信息返回 `needs_input=true` 与 `field=options.<参数>`，助手以“请补充信息”保存待补充请求；补充时合并 options，保留标题及已选参数。后续模型收到待补充 CLI 请求的规范参数；查询补充成功后可继续原提交意图，不必重新描述 Bug。未确定空间时优先查询可访问空间，不能唯一判断才让用户选择。

Meego 明确返回 `Check Token Perm Failed` 时，保留错误码、鉴权/业务阶段和当前操作，HTTP 返回 403；助手说明需核对对应 OpenAPI 权限、插件发布版本在空间内的启用状态和 User Key 的空间权限。缺少配置或本地参数校验失败为 422。创建请求已发送但超时或缺少工作项 ID 仍为 503/uncertain，不能重放或宣称已创建。Meego 的“测试配置”仅能证明换到插件凭证，不能据此声称具备空间读取或创建权限。

### 飞书 CLI 请求续接与权限诊断（2026-10-21）

助手 origin 与轮次路由保留 `transport=feishu_cli`，供工具执行、待补充请求续接和绑定复核使用。兼容旧版已保存请求缺少的固定路由标记：仅在账号、节点、实例、发送者和 binding_key 均匹配后恢复，再校验当前节点归属与插件状态。真实入站仍须携带正确 transport；解绑重绑、跨账号、实例变化、离线与渠道停用仍拒绝。

网关失败日志与轮次 detail 增加 `error_reason`、`error_stage`、`error_site`，分别标明固定原因码、失败阶段和函数位置。区分绑定变化、执行账号缺失、会话被新消息打断和处理租约失效；不记录任意异常正文、聊天正文或凭据。保留既有写回执和未知结果不重放规则。

Meego 空间发现返回空列表时，助手不重复相同查询，而是询问目标空间链接或别名，将待补充查询保存为 `project get`；补充后继续解析真实空间 key。空的空间详情提示核对目标和访问范围，空的工作项类型或创建模板明确阻断创建，不猜测业务 ID。空结果本身不能证明权限被拒绝。CLI 无进展提示不再要求选择 Mino 应用或测试模块。

追问无候选项时使用直接输入提示，不生成空 choices，也不要求回复序号；确认或继续消息沿用同一输入提示。CLI HTTP 响应排除 Scout 附带的顶层 `plugins` 状态快照，避免无关状态造成长度截断并丢失业务权限错误码及具体处理指引；业务结果和权限拒绝状态仍按原合同返回。

## 节点配置能力对 MCP 开放（2026-10-27）

Workbench 节点配置里的 CLI、用例服务 API、IM 和 Gmail 不再只服务 IM 助手。新增 MCP 工具如下；工具仍经 `run_tool`、`route_allowed`、执行账号和写回执，不新增通用 HTTP 代理，不把节点密钥带回 Nexus：

| MCP 工具 | 路由 | 风险 | 说明 |
|---|---|---|---|
| `cli_query` | `POST /runtime/cli/query` | read | `cli.query` 的 `surfaces` 增加 `mcp`；节点未指定时自动选本人唯一已配置在线节点 |
| `cli_execute` | `POST /runtime/cli/execute` | write | `cli.execute` 的 `surfaces` 增加 `mcp`；MCP 入口没有 IM 会话附件，传 `attachments` / `attachment_ref` 直接 422 |
| `case_api` | `POST /runtime/case-api/read` | read | 读 Workbench「API」集成；`operation=projects/sessions/cases`，`configuration.node_id` 必填；最多返回 `limit` 行并标 `truncated` |
| `im_channels` | `GET /assistant/capabilities` | read | 列本人节点的飞书 CLI、微信、LangBot 渠道、连接状态、绑定和消息能力；不含密钥和聊天正文 |
| `im_send` | `POST /runtime/im/send` | write | 向已上报渠道发文本；`chat_id` 必须显式给；飞书 CLI / LangBot 的实例由节点状态补全，微信仍要求入站 `context_token` |
| `gmail_otp` | `POST /runtime/gmail/otp` | read | 调当前账号节点上的 `plugin.gmail.fetch_otp`；只返回 4-8 位验证码和有限诊断，不回传邮件正文或密码 |

约束：

- `case_api` 走现有 `case_import_sources.read` 和 `case_source_transport`，只读、受项目权限约束；`configuration` 的密钥仍在 Scout，Nexus 只接收 `node_id`、远端项目/会话和范围。
- `im_send` 的 `channel` 只接受 `feishu_cli`、`wechat`、`feishu`、`wecom`、`dingtalk`；LangBot 不接受调用方伪造 `bot_uuid`，服务端按节点上报的 `instance_id` 覆盖。`delivery_id` 取 `client_request_id`，未给时按内容与目标哈希，避免同一请求重发。
- `gmail_otp` 的 `max_wait_ms` 上限 45 秒，`since_ts` 只能取最近 7 天；`to_address` 省略时用该节点 Gmail 配置的收件地址。超时或未配置返回明确失败，不猜验证码。
- 以上工具均为 `surfaces: [mcp]`；`cli.query` / `cli.execute` 同时保留 `im`。MCP 工具仍受 `assert_no_delete` 和写回执约束，不新增 DELETE、任意 Shell、节点本地文件读取或凭据写接口。

### 插件安装状态与安装/更新（2026-10-27）

在同一个 MCP 入口增加两个插件生命周期工具。它们复用 Workbench 已使用的节点归属、`node_tools` 下载中心合同和 Scout 持久任务，不新增下载源、不接收任意 URL 或密钥：

| MCP 工具 | 路由 | 风险 | 说明 |
|---|---|---|---|
| `plugin_status` | `GET /runtime/plugins/status?node_id=` | read | 列当前账号可见节点的插件 `installed/configured` 状态；指定 `node_id` 时同时读取下载中心，返回 `installable/updatable/version/install_state` 和最近任务 |
| `plugin_install` | `POST /runtime/plugins/install` | write | 提交 `cli.feishu`、`cli.meego` 或 `langbot` 的 `install/update` 任务；返回 Scout 任务 `task_id/state`，`wait_ms>0` 可在 60 秒内轮询 |

约束：

- `plugin_status` 不返回插件密钥、webhook、字段值或聊天正文；只保留安装、配置、连接和版本状态。`plugins_known=false` 表示节点尚未上报插件快照，不等于未安装。
- `plugin_install` 只接受三个需要单独安装的插件；`case_api`、`gmail`、`bot.*` 等内置能力返回参数错误，应去 Workbench 保存配置。该工具只允许 MCP 入口，IM 助手调用会明确拒绝并提示去 Workbench 下载中心。
- 安装由 Scout 下载中心异步执行，任务绑定节点当前进程；同一 `operation_id` 幂等。MCP 按 JSON-RPC 请求派生稳定 `operation_id`，超时后先用 `task_id` 查询，不能换请求 ID 重装。
- 只允许节点 owner 提交；管理员查看他人节点时可以读插件快照，但不能通过本工具安装。离线、插件归属未确认、旧版 Scout 或已有活动任务时明确拒绝。
- 状态和安装任务仍只走 SQLite 的 `NodeToolTask` 与节点归属，不在 Nexus 落插件配置；不开放删除、卸载或任意工具安装。

## 多 Agent 工作层（2026-10-22，v1）

工作层 HTTP 只做鉴权、参数校验和状态读写；执行由进程内 `work_runner` 领取步骤，模型/工具调用不在请求线程里跑。所有接口在 `/project/{project_id}/work` 下，要求当前账号有项目访问权限，并且只返回 `owner_user_id` 为当前账号的工作项。

- `POST /`：body `{goal, title?, topic_id?, source_message_id?, create_key?, acceptance?, budget?, instructions?, policy?}`；`policy.write_approval=plan|always` 控制写工具审批级别；`(owner_user_id, create_key)` 幂等，返回 `{code:200, data:work, created}`。
- `GET /?topic_id=&status=&limit=&offset=`：工作项摘要列表，含 `progress`。
- `GET /{work_id}`：详情，含 `steps`、`approvals`、`artifacts`、`links`、`events`；只读 `run_store`，不复制 run 状态。
- `POST /{work_id}/pause|resume|cancel`：暂停/恢复/取消；取消会把待审批置 rejected，运行中的步骤由 finish guard 收口。
- `POST /{work_id}/budget`：仅用于 `blocked_reason` 为 `budget_exhausted:*` 的工作；body `{extra_tokens?, extra_steps?, extra_turns?, extra_tool_calls?}`，默认追加 60000 token、8 步、4 回合、4 次工具调用。显式追加后恢复被取消的步骤；不在后台静默加预算。
- `POST /{work_id}/instruct`：`{text}` 追加用户约束，进入后续计划和角色回合。
- `PATCH /{work_id}/plan`：`{plan, acceptance, expected_revision}`；有版本冲突返回 409，重新审批后旧计划不继续。
- `POST /{work_id}/approvals/{approval_id}`：`{decision:"approve"|"reject", comment?, option?}`；计划批准会把 work 拉回 running；工具审批执行审批时冻结的 tool_id/args，不让模型换参数重来。
- `POST /{work_id}/steps/{step_id}/retry|skip|takeover|resolve`：重试失败步骤、跳过、暂停并接管、裁决 `unknown` 写结果。`unknown` 不自动重放；必须先确认已执行/未执行。

写操作成功仍返回 `{code:200, data}`；业务错误使用 `{detail:{reason_code,message}}`，前端 `errText` 可直接显示。工作事件只在 SQLite commit 后通过 `/ws` 以 `work_event` 推送，归属按 `work_items.owner_user_id` 解析。
