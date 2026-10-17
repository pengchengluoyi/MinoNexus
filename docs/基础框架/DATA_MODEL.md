# DATA_MODEL — 数据模型与归属

**Nexus 是业务状态的数据归属方。** Scout 不访问 Nexus 数据库；渠道消息真源仍在 Scout 本地 `scout.db`。Nexus 的 `assistant_turns` 保留限长、按敏感字段脱敏的 IM 轮次证据，用于请求复盘，不作为渠道消息同步库。

## 1. 归属总表

库文件是 `mino.db`（`data_dir()`，默认 `~/.mino-nexus`，可用 `MINO_NEXUS_DATA_DIR` 覆盖）。SQLAlchemy，`create_all` + 启动时 `run_auto_migration()`。

| 数据 | 表 | 权威来源 |
|---|---|---|
| `App` / 项目 / 环境 | `projects` / `apps` | UI / 飞书同步 |
| 项目号池账号 | `pool_accounts` + `pool_account_facets` | Studio 账号管理；`projects.env.test_accounts` 仅迁移来源 |
| 项目号池配置（扩展字段 / 启用模板 / 本地模板） | `project_pool_config` | Studio 号池与模板；`projects.env` 中对应键仅迁移来源 |
| 用例资源声明（Claim 词汇 / 示例） | 用例 `meta.resource_key` + `case_scene`（真源词汇见 `case_resource_key_catalog.py`） | Console「用例密钥」；见 [CASE_RESOURCE_KEY.md](CASE_RESOURCE_KEY.md) |
| 用例、前置、预期 | `project_cases`（按 `project_id` + `requirement_id`；需求/脑图仍在 `apps.env.automation.qa_process`） | UI / QA 推进 |
| 用例导入预览 | `case_import_previews`（账号与项目隔离，24 小时有效；解析结果逐行持久化，生命周期独立于提交回执） | 导入预览 API；工作租约避免多进程重复解析，进程退出后恢复未完成行 |
| 用例导入回执 | `case_import_receipts` / `case_import_receipt_lines`（账号、项目、提交身份唯一；不随预览过期删除） | 确认导入与回执查询/续交 API；保存原始行、决策及实际用例 revision 快照，每行用例与结果原子提交 |
| `AppRegressionRun`（批次结果） | `app_regression_runs` | Nexus 的循环 |
| 逐步 trace / baseline | `m_case_run_trace` / `m_case_baseline` | Nexus 的循环 |
| 设备身份 | `m_device` | **连通性来自 Scout 上报**，其余来自 UI |
| 设备 × App 会话 | `device_app_sessions` | Scout 执行 + `inspect-session` / 清缓存转移；见 [CASE_RESOURCE_KEY.md](CASE_RESOURCE_KEY.md) |
| 节点 / 工作台归属 | `nodes` / `studios` | 活连接在内存 `NodeRegistry` |
| 账号 / 会话 | `users` / `sessions` / `auth_state` | UI 登录 |
| 设置 / 知识机审开关 | `settings` | UI |
| 文档库元数据 / 分片 | `doc_sources`（含 `source_kind`/`source_url`）/ `doc_chunks` + FTS | 上传 / 飞书同步；见 [DOC_LIBRARY.md](DOC_LIBRARY.md) |
| 原始文档文件 | `{data_dir}/docs/{app_id}/{source_id}/` | 与 `mino.db` 同根 |
| 执行知识条目 | `knowledge_entries` | UI / 跑批沉淀 / DOC_LEARN（二期） |
| 调度流水 | `dispatch_calls` | LLM / pipeline |
| Session 轨迹 | `session_events` / `session_meta` | Agent 循环双写 |
| IM 轮次证据 | `assistant_turns` | `detail_json.user_text/assistant_text/outbound_text/sent_text/request_id` 与 `card_json`；正文最多保留 8000 字符及截断标记，敏感值隐藏；记录发送状态和片数，仅当前账号可查 |
| 能力目录 | `catalog_entries`（`kind` 区分五类） | 空库不灌；只经 Console `/packs` 写入 |
| NavFSM（导航图配置） | `nav_fsm` / `nav_fsm_states` / `nav_fsm_edges` | Studio / `PUT /nav-fsm/{app_id}`；见 [NAVIGATION_ATLAS.md](NAVIGATION_ATLAS.md)。键：`app_id` + `project_id`（来自 `apps`） |
| NavFSM 校准证据（文件） | `{data_dir}/nav/calibration/{app_id}/{calibration_id}/` | 与 `mino.db` 同根；不进 git、不进 ORM 正文 |
| AppIntel 关联（规划） | `app_intel_links`（P1 可选） | 导航 / 知识 / 文档交叉引用；投影层见 [9月12日_AppIntel信息基座.md](9月12日_AppIntel信息基座.md) |
| 技能 | `skills`（角色 + SOP + prompt + `view.id`） | builtin 从代码灌种一次，之后以库为准；坏行回退 `ai/skill_defs.py` |
| 图谱别名 | `m_atlas_alias` | UI |
| 集成插件策略 / 用户密钥 | `plugin_policies` / `user_plugin_secrets` | UI |
| Studio 侧栏 | `studio_nav` | Console |
| Scout 安装凭证 | `install_tokens` | Studio 领取（短 TTL） |
| Scout 节点长期 token | `node_credentials` | 首次 REGISTER 换发，按 `node_id` 唯一 |
| 轻量任务 | `tasks` | `rTask` |
| 节点 manifest | Nexus 内存缓存 | **Scout 的 `REGISTER` / `HEARTBEAT`** |
| 原始截图 / 屏幕流 | **Scout 本地**，用后即删 | Scout |
| trace 缩略图 | Nexus 内存 `_RUNS` + `session_events` | Nexus |

Workbench V4：`users.email` 为规范化后的可空邮箱，数据库唯一约束兜底；无邮箱的旧本地账号保留 NULL。启动时仅新增列、规范化回填和创建唯一索引，不删除旧账号。历史重复邮箱会使唯一索引迁移明确失败，需要先由账号管理处理，不静默合并账号。新普通注册写 `email_verified=false` 并固定 `role=user`。注册与登录会话采用独立事务，不通过全量账号快照写回。

列表分页索引为 `project_cases(project_id,sort_index,pk)` 与 `app_regression_runs(app_id,started_at,run_id)`，根据 V4 实测查询计划消除分页排序临时表。助手旧 `default_app_id/default_sn` 列保留，业务停止读写。

设备表里**连通性字段是缓存，不是权威** —— 权威在 Scout。Nexus 不要自己去探。

## 2. `MDevice.channels` 的新字段

拆分新增 `node_id`（见 [NODE_REGISTRY.md](NODE_REGISTRY.md)）：

```json
{
  "node_id": "mac-studio-01",
  "adb":    {"state": "connected", "transport": "usb", "serial": "R5CT30xxxx", "last_probe_at": "..."},
  "remote": {"state": "connected", "auth_state": "Authenticated", "last_heartbeat_at": "..."},
  "ios":    {"state": "not_applicable"}
}
```

`state` 取值：`connected` / `disconnected` / `unauthorized` / `auth_failed` / `unpaired` / `not_applicable`。

## 3. trace 的三处存储

| 存储 | 内容 | 生命周期 | 谁读 |
|---|---|---|---|
| `agent_stream._RUNS` | 每步 thumb + decision + status | 内存，**上限 40 个 run** | `GET /case-runner/agent/steps`（热路径） |
| **`session_events` + `session_meta`** | append-only 轨迹（见 [9月8日_Session_Event_Log.md](9月8日_Session_Event_Log.md)；**排查**见 [排查手册](排查手册/README.md)） | DB，永久 | `GET /case-runner/sessions/{id}/events` · `/trajectory` · `/llm` |
| `AppRegressionRun` | 批次级结论 + 用例级结果 | DB，永久 | `GET /case-runner/tasks/{id}` |

Studio 读取顺序：**先内存 `_RUNS`，兜底 session log 投影**。

**说明**：session log 双写于 `agent_loop`（结构化 event + `stream/emit` 镜像）；服务重启后轨迹仍可从 DB 恢复。

## 4. 从 sqlite 到 PG

本仓已经是 sqlite：`data_dir() / mino.db`，`core/database.py` + `core/migration.py`。上游文件名是 `autobots.db`，这里不用。

换 PG 单列一个阶段。到时候要处理：

- 15 个 ORM 模型的方言差异（JSON 字段、自增主键、大小写）
- `migration.py` 的自动迁移逻辑重做（建议换 alembic）
- sqlite PRAGMA 相关的连接配置（`_set_sqlite_pragma`）作废
- 并发写：sqlite 靠 `_with_db_retry` 硬扛，PG 下应改成正常事务

## 5. 未决项

| # | 事项 | 影响 |
|---|---|---|
| 1 | `_RUNS` 内存上限 vs 多节点 | session log 已落库；内存仅作热缓存 |
| 2 | sqlite → PG 的时机 | 上云的前置条件 |
| 3 | 循环状态（跑到第几步、history）在内存 | Nexus 多实例需要 sticky routing 或状态外置 |
| 4 | 截图归档策略 | 当前 Scout 用后即删，只有 thumb 留在 Nexus。若要复盘原图需要对象存储 |

## Workbench V4 账号命名空间与迁移

- `nodes.owner_user_id` 来自认证安装凭证；设备归属必须与所在节点一致。不同账号上报同 SN 被忽略；同账号节点间可移动，设备 owner 保持不变。无归属节点/设备保留原数据并停用，普通页面及管理员页面均不隐式认领。
- `settings.id=ai-user:<uid>` 存该账号 provider 配置、Key、base URL、模型、默认项与用途开关。`settings.main` 中旧全局 Key 保留为待确认数据，在线执行不回退到该配置或环境 Key。
- `app_regression_runs`、`session_meta`、`dispatch_calls`、`doc_sources` 增加 `owner_user_id`。后台任务创建时固化 owner；读列表、结果、轨迹、模型诊断与文档检索均限定该 owner。文档 FTS、字面检索及分片读取/变更采用同一过滤。无 owner 的文档不自动同步。
- 任务分页索引 `app_regression_runs(owner_user_id,app_id,started_at,run_id)`；最终实测 `EXPLAIN QUERY PLAN` 命中 `ix_run_owner_app_started` 覆盖索引。
- `im_route_identities` 精确唯一键 `(channel,instance_id,tenant,sender_id)`，记录本人 `user_id`、`node_id`、`bound_at` 与随机 `binding_key`。解绑再绑定生成新代次，旧请求不能恢复权限。旧 `im_identities` 保留但不参与在线路由。
- `im_inbound_claims(key,owner_user_id,created_at)` 对同来源消息原子去重；绑定码在事务中条件删除并校验 TTL/owner，其他账号尝试不消费码。
- `assistant_subscriptions` 保存真实 sender 与确认后的 reply context，通知前检查任务 owner、节点 owner、实例与绑定代次。会话路由元数据存入 `assistant_turns.detail_json._im_route`，不向公共详情暴露原始 sender 或路由凭据；正文证据按上述规则保留，历史被丢弃的正文无法回填。

首次启动在建表/迁移前使用 SQLite backup API 生成独占创建、0600 权限的一致性备份，不覆盖旧备份。迁移标记 `migration:account-isolation-v1` 写回 SQLite。仅可信且仍对应有效用户的旧任务 `plugin_user_id` 回填 owner，关联 session 继承可信任务归属；无可信归属数据不推测 owner。已部署中间版本缺 binding_key 时，添加列前也执行备份。历史全局 Key、旧社交映射和无归属本地插件配置均需维护流程明确确认，当前未实现认领/转移 UI。

Scout 密钥命名空间包含已确认 owner；本地 IM channel 以服务 origin + owner + channel + 实例 + 租户 + 发送者形成作用域，查询不读取旧未分域行。原历史文件和行保留；更换账号或服务拒绝继承本地插件。绑定时间按秒记录，同秒旧上下文也会被保守排除。


## V4 Desktop 设备与资源准入

- `m_device` 保留 `platform=desktop`，OS/会话代际/桌面状态/就绪原因/能力/权限元信息存现有 `extra` JSON 列。`sn=desktop:<node_id>:console`，重启不创建新设备。
- `nodes.extra.host` 保存宿主信息与桌面摘要；设备连通性仍只来自 Scout 注册/心跳。
- 新表 `device_reservations`：`resource_key` 主键，`run_id` 非空且有索引。独占资源键为 `desktop:<node_id>:console` 或 `device:<sn>`；headless Web 为 `web:<sn>:lane:<0..3>`。资源准入与 `app_regression_runs` 新任务一起原子提交，失败整笔回滚；任务终态释放自己的记录。
- `app_regression_runs.payload.device_targets` 按 sn 保存选中设备的真实平台、OS、会话代际和应用渠道；`platforms_by_sn` 由这些服务端事实生成。客户端平台参数只兼容旧调用，不作为权限或路由依据。
- 历史无 owner 节点认领在 SQLite 事务内同时更新 `nodes`、未归属的 `m_device` 与 `node_credentials`。旧 studio/plugin 字段不授予账号权限。

以上使用现有数据库建表流程，不增加 JSON 真源。验证必须使用新的 `MINO_NEXUS_DATA_DIR`，禁止修改已有运行库。

## 项目维护者（2026-10-15）

- 新表 `project_members`：主键 `(project_id, user_id)`，`role` 固定 `maintainer`，`added_by` / `added_at` 记录管理员操作。项目创建者（`projects.created_by`）隐式为维护者，不写入该表；管理员不需要成员行。删除项目同时删除其成员行。由 `create_all` 建表，无 JSON 真源。


### 显式平台迁移回执（N4 开发中）

`platform_migration_receipts(plan_id,state,reviewer,backup_path)` 仅由显式审阅后的 `services.platform_migration_apply` 在成功的业务数据迁移事务内创建/写入，不由启动自动执行。state 为 applied/reverted，回执与字段更新同事务；重复调用仍验证对应字段摘要，漂移则拒绝。业务库启用仍依赖三仓消费者和完整引用清单验收，不能根据该工具存在推定已迁移。session、历史 run、原文、冻结 publication 和锁键不在该工具可写范围。

## N2 普通绑定与原子终结（2026-10-15）

复用 session_events/session_meta；不新增 JSON 真源。`execution/bound` 与 `runtime_context.execution_binding` 同事务写入，并保存事件引用；绑定内容不可变，同 ID 同内容幂等，不同内容拒绝。runtime_context 深合并，禁止覆盖绑定/终结身份；最终幂等摘要覆盖 context。SQLite 并发更新仍在事务内串行分配事件序号，超时完整回滚。

绑定正文超过 256 KiB 写 `binding_size_warning`（字节数、操作数和唯一实现数），不截断、不因超线直接拒绝。实际存储失败才阻断派发。普通 SessionWriter.close/force_close 走原子 finalizer，将 session/end、普通 unverified verdict、finalization_id、finished_at 和证据范围一起提交；取消与未知写隔离优先，不将普通 pass 提升为严格 verified_pass。终结后拒绝新增事件或覆盖终态，重复相同终结幂等。

读取返回 `binding_record.status=legacy|supported|unsupported|invalid`。没有 binding 的旧会话保留原证据；未知版本不按当前目录补造详情。Workbench 显示版本提示、当时的操作/实现装配和普通终结保证范围。


## V5 持久化编译与逐项复核（2026-10-15）

- `case_revisions`：`revision_id`、owner/project/case、单案递增 revision、不可变 source/source_digest、冻结 compile_input/input_digest、来源 receipt/line 或补充理由、时间。原文和资源配置形成候选版本；旧版本没有更新或删除入口，删除用例也保留历史。首次接管旧用例只登记当前可见快照，`legacy_snapshot` 不伪造旧解析来源。
- `case_compile_jobs`：owner/project/case/request_id 唯一；revision 引用、构建身份、状态、claim token/worker/租约、取消标记、恢复父任务、产物/诊断/实际 dispatch/model/catalog provenance。状态为 queued/running/succeeded/failed/interrupted/cancelled。领取原子；租约失效标 interrupted，不自动重复模型调用。显式恢复创建新关联 job，同一请求重试返回同一 job。
- `case_review_records`：轻量追加记录，不是新工单系统。保存 revision/item/context、诊断快照、确认分类、理由、实际复检结果、reviewer/time 和可选 session 引用；不覆盖自动 verdict。静态复检消失记 fixed_pending_run；只有 owner/项目/用例/原文/编译 job/冻结版本匹配且在问题记录之后的 verified_pass session 才能标 verified_resolved。
- `project_cases.extra` 的 `source_revision_id/source_revision/source_digest/latest_compile_job_id/compile_job/effective_artifact` 是服务端投影。客户端不能写入编译成功或有效产物。保存原文与 revision+queued job 在同一事务；导入行状态/快照/结果也在该事务。receipt result 保存 `revision_id/compile_job_id/source_digest`，不再把预览缓存当权威产物。
- worker 只在事务外复用既有 resource/step 编译器；恢复固化 owner，不复制项目环境、服务商密钥或账号整包。结果落盘时校验 claim、租约、取消、owner、最新 revision/job 和冻结输入；旧结果只入历史。新任务使旧有效产物失效；旧 publication 仍可读，必须重审发布才可执行新原文。


### N4 SN 引用迁移补充（2026-10-16）

显式映射支持 `operation=rename_key`，仅限 `device_targets` 和 `platforms_by_sn` 的字典键。path 指向旧键，before 为旧 desktop:{node}:console，after 为同一 node 的 mac/win:{node}:console；引用值原样移动，不导出到计划。新键已存在、旧键不存在、跨宿主、未知映射及受保护历史路径均拒绝。需要同时改嵌套平台时，先列 rename_key，再列新键下的 set；同一字段 CAS 与同一事务提交，回退按逆序执行。共享 resource_key、历史 session/run/frozen publication 不参与改名。

预演、prepare 与 apply/rollback 同时检查 tasks 的 pending/queued/running、case_compile_jobs 的 queued/running；有未结束工作时阻止切换。编译任务的输入/产物/历史不直接迁移。任意未分类字典中的 desktop SN 键会输出脱敏阻断项，不能因尚未支持改写便从盘点中消失。该扩展不代替完整引用审阅、业务写入暂停及三仓消费者验收。


N4 预演/静默窗口按持久 write_safety 动作账本核对停止状态：完整 settled 回执，或 recovered 且包含同 action_ref/SN 的 write_stop.v1、审核者与有效进程/执行 lane 停止证明，才能忽略历史 cases 中保留的隔离摘要。缺失/空账本沿用旧数据保守检查，畸形账本、独立顶层隔离、未知写及证明不完整仍阻断；不修改历史 verdict 或解除实际租约。apps.platforms 继续按现有逗号字符串存储合同盘点，不误报必须改成 JSON 数组；嵌套 platforms 仍须数组。


V6 D1：`scout_updates` 保存节点更新操作、唯一活动 slot、参数、等待截止和最后执行端回执。门禁与新任务资源准入共用 SQLite 写事务；执行端操作账本是安装状态权威。
