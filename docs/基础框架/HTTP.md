# HTTP — Console / Studio 怎么连本仓

## 会话执行版本证据（2026-10-18）

`GET /case-runner/sessions/{session_id}/execution-evidence` 只读取当前账号的历史会话。
可选查询参数 `expected_instance_id`、`expected_nexus_build_id`、`expected_node_id`、`expected_build_id` 是测试计划中的预期值，不自动采用当前实际值。
实例不符返回 409（`instance_mismatch`），会话不可读或跨账号返回 404，未认证返回 401。
返回实际 Nexus 实例/build、Scout 节点/进程/build、设备、结果事件序号、源码提交与快照摘要以及可复制的引用。
`state=verified` 只代表该会话的执行版本与预期一致且用例通过；缺预期、旧回执、源码变化、未结束或读取截断均不能通过。旧 build/节点/进程不符返回 `state=failed`。
固定 `scope=session_execution`、`web_chain_verified=false`、`mcp_regression_verified=false`，不把局部执行证据升级成正式 Web 闭环或 MCP 回归结论。不建设跨 Nexus 代理或归档库，删除测试库后原引用不可读取。

开发准备绑定可选 `expected_instance_id`；`probe` 同时核对目标实例与 build，401/403 明确失败。实例/版本检查不执行用例。源码 Scout 的 IDE 操作仅允许其独立配置中 `development_ide_projects` 登记的专用样例项目；通过 `mino-scout development-ide-projects --add <绝对路径>` 登记，不允许自身或 MINO 控制链源码目录。

UI **只**打 MinoNexus。没有公网域名时用内网名 `mino.local`：Nexus 进程启动后用 mDNS 注册，客户端不填 IP。

## 手动导入用例（2026-10-18）

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
| GET / PUT | `/settings/case-import` | 导入策略：`block_on_step_key_issues_default` |
| GET/PUT | `/nav-fsm/{app_id}/screen-key-refs` | Nav 屏幕 `meta.key_ref` |

## 设备输入法（Android）

Nexus 不直连 adb；经在线 Scout 下发 `set_input_method`。设备未安装时 Scout 会尝试从 GitHub 下载 [ADB Keyboard](https://github.com/senzhk/ADBKeyboard) 并 `adb install`，再切换。Scout 进程每次启动 REGISTER 前也会走同一套逻辑。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/device/{sn}/ime/adbkeyboard` | 将该 sn 对应 Android 设备默认输入法切到 ADB Keyboard。设备离线或 Scout 无应答时 409/502 |
| POST | `/device/{sn}/ime/system` | 关闭 ADB Keyboard，切回其它已启用系统输入法（经 Scout `set_input_method` + `ime=system`） |

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
| `POST` | `/nav-fsm/{app_id}/candidates/compile` | 从采集生成候选（不进正式库） |
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
| `POST .../review-requirements/{item_id}/review` | `{expected_revision,expected_job_id,reason,category?}`；category 允许 case_definition/semantic_parsing/capability_gap/configuration/execution_toolchain/external_evidence，null 表示暂不定责；只登记人工意见，不解除运行阻塞 |
| `POST .../review-requirements/recheck` | `{expected_revision,expected_job_id,reason,context?,evidence_session_id?}`；context 为当前项目应用及 owned 设备 `{app_id,sn,env_profile,env_surface,execution_mode?,action_scheme?,provider_id?}`。实际重算静态资格、目标绑定和资源登记检查，不租设备、不调用执行器。目标诊断按上下文保存，省略目标不能清除原目标问题；观察超过 60 秒或版本改变标过时 |

原有 `POST .../v5/compile`、`.../step-keys/compile`、`.../resource-key/compile` 统一进入队列，仍返回 `{case,job,preview}`。新客户端发送上述稳定请求身份；兼容无 body 的旧入口可复用在途工作，不能把 HTTP 返回当编译已完成。保存/导入不再被模型时延阻塞；预览的解析仍是可丢弃候选，确认导入保留原文并以持久任务报告编译缺口。

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

## V6 Nexus 安装维护（D2 首轮实现）

仅已登记且完成健康验证的独立安装 Nexus 接受更新。所有维护接口要求管理员；普通用户 403，源码部署 409。

| 方法 | 路径 | 合同 |
| --- | --- | --- |
| GET | `/sys/updates/status?operation_id=...` | 原操作持久状态；省略 ID 查看本部署当前操作 |
| POST | `/sys/updates/request` | `{operation_id,build_id,wait_seconds?,rollback?}`；稳定 ID 幂等，参数冲突拒绝；目标已暂存且具有确切版本兼容记录 |
| POST | `/sys/updates/{operation_id}/cancel` | 仅取消该操作尚未开始的等待；不强停安装 |

`/sys/server_info` 返回 instance_id、build_id、health_contract_version=1、deployment_managed。专用 probe token 仅可向 `/mcp` 调 initialize/ping/tools/list，不能调 tools/call 或普通 API。更新 hold 下新执行返回 blocked_by_update 与 operation_id；安装阶段保留健康和更新查询入口。详细部署步骤及未验收边界见 [D2 开发记录](../架构/V6/实施/mino-D2-开发记录-20261017.md)。

## Workbench 本机安装升级（D3）

`/sys/workbench/installation`、`/sys/workbench/updates/{check,status}` 为 GET；`/sys/workbench/updates/{request,cancel,recover}` 为 POST。均要求管理员；除安装身份查询外必须携带 installation_id，写操作绑定稳定 operation_id。只调用部署环境显式登记的同机原生助手，不接收命令/路径/URL 或 dirty 包绕过。完整参数、暂存目录及验收范围见 [Workbench MCP 更新入口](../架构/V6/实施/mino-D3-Workbench-MCP更新入口-20261017.md)。


### D2 本地人工恢复模式（2026-10-17）

`mino-maintain d2 recover --root <安装根> --mode <模式>` 只处理当前 needs_operator 操作，不提供远程强制解除门禁。

- `prestart-backup`：尚未重开写入，且有完整备份；确认进程退出后恢复备份，失败数据另存保留。
- `code-only`：曾开始正常启动；仅回退兼容旧代码，保留当前数据。
- `restart-previous`：仅 stopping/backing_up、writes_reopened=false、current 仍指向 previous_build；确认退出后重启旧版，保留原数据/部分备份。已进入迁移及后续阶段拒绝此模式。

显式恢复在启动前持久写入 state=restarting、phase=operator_recovery。恢复进程中断后，本地 helper 只探测原 previous_build 并完成同一 operation；无法核实则保留 needs_operator，不重放迁移或自动倒库。

Scout 已派发更新与待人工处理状态纳入 Nexus 忙闲检查；维护等待期间不新派发排队 Scout 更新，原操作查询/对账继续。MCP 线程按实际执行生命周期持有写者锁，连接/协程取消不等于写者已退出。
