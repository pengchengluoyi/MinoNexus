# DATA_MODEL — 数据模型与归属

**Nexus 是业务状态的数据归属方。** Scout 不访问 Nexus 数据库；IM 正文仅保存在 Scout 本地 `scout.db`，不形成第二份业务真源。

## 1. 归属总表

库文件是 `mino.db`（`data_dir()`，默认 `~/.mino-nexus`，可用 `MINO_NEXUS_DATA_DIR` 覆盖）。SQLAlchemy，`create_all` + 启动时 `run_auto_migration()`。

| 数据 | 表 | 权威来源 |
|---|---|---|
| `App` / 项目 / 环境 | `projects` / `apps` | UI / 飞书同步 |
| 项目号池账号 | `pool_accounts` + `pool_account_facets` | Studio 账号管理；`projects.env.test_accounts` 仅迁移来源 |
| 项目号池配置（扩展字段 / 启用模板 / 本地模板） | `project_pool_config` | Studio 号池与模板；`projects.env` 中对应键仅迁移来源 |
| 用例资源声明（Claim 词汇 / 示例） | 用例 `meta.resource_key` + `case_scene`（真源词汇见 `case_resource_key_catalog.py`） | Console「用例密钥」；见 [CASE_RESOURCE_KEY.md](CASE_RESOURCE_KEY.md) |
| 用例、前置、预期 | `project_cases`（按 `project_id` + `requirement_id`；需求/脑图仍在 `apps.env.automation.qa_process`） | UI / QA 推进 |
| 用例导入预览 | `case_import_previews`（账号与项目隔离，24 小时有效；解析结果逐行持久化，提交后移除） | 导入预览 / 确认导入 API；工作租约避免多进程重复解析，进程退出后恢复未完成行 |
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
- `assistant_subscriptions` 保存真实 sender 与确认后的 reply context，通知前检查任务 owner、节点 owner、实例与绑定代次。旧会话路由元数据存入 `assistant_turns.detail_json._im_route`，不存正文、不向公共详情暴露原始 sender 或路由凭据。

首次启动在建表/迁移前使用 SQLite backup API 生成独占创建、0600 权限的一致性备份，不覆盖旧备份。迁移标记 `migration:account-isolation-v1` 写回 SQLite。仅可信且仍对应有效用户的旧任务 `plugin_user_id` 回填 owner，关联 session 继承可信任务归属；无可信归属数据不推测 owner。已部署中间版本缺 binding_key 时，添加列前也执行备份。历史全局 Key、旧社交映射和无归属本地插件配置均需维护流程明确确认，当前未实现认领/转移 UI。

Scout 密钥命名空间包含已确认 owner；本地 IM channel 以服务 origin + owner + channel + 实例 + 租户 + 发送者形成作用域，查询不读取旧未分域行。原历史文件和行保留；更换账号或服务拒绝继承本地插件。绑定时间按秒记录，同秒旧上下文也会被保守排除。


## V4 Desktop 设备与资源准入

- `m_device` 保留 `platform=desktop`，OS/会话代际/桌面状态/就绪原因/能力/权限元信息存现有 `extra` JSON 列。`sn=desktop:<node_id>:console`，重启不创建新设备。
- `nodes.extra.host` 保存宿主信息与桌面摘要；设备连通性仍只来自 Scout 注册/心跳。
- 新表 `device_reservations`：`resource_key` 主键，`run_id` 非空且有索引。独占资源键为 `desktop:<node_id>:console` 或 `device:<sn>`；headless Web 为 `web:<sn>:lane:<0..3>`。资源准入与 `app_regression_runs` 新任务一起原子提交，失败整笔回滚；任务终态释放自己的记录。
- `app_regression_runs.payload.device_targets` 按 sn 保存选中设备的真实平台、OS、会话代际和应用渠道；`platforms_by_sn` 由这些服务端事实生成。客户端平台参数只兼容旧调用，不作为权限或路由依据。
- 历史无 owner 节点认领在 SQLite 事务内同时更新 `nodes`、未归属的 `m_device` 与 `node_credentials`。旧 studio/plugin 字段不授予账号权限。

以上使用现有数据库建表流程，不增加 JSON 真源。验证必须使用新的 `MINO_NEXUS_DATA_DIR`，禁止修改已有运行库。
