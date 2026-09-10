# DATA_MODEL — 数据模型与归属

**Nexus 是唯一的数据归属方。** Scout 不碰数据库（`../MinoScout/scripts/verify_no_orm.py` 守门）。

## 1. 归属总表

库文件是 `mino.db`（`data_dir()`，默认 `~/.mino-nexus`，可用 `MINO_NEXUS_DATA_DIR` 覆盖）。SQLAlchemy，`create_all` + 启动时 `run_auto_migration()`。

| 数据 | 表 | 权威来源 |
|---|---|---|
| `App` / 项目 / 环境 | `projects` / `apps` | UI / 飞书同步 |
| 用例、前置、预期 | `project_cases`（按 `project_id` + `requirement_id`；需求/脑图仍在 `apps.env.automation.qa_process`） | UI / QA 推进 |
| `AppRegressionRun`（批次结果） | `app_regression_runs` | Nexus 的循环 |
| 逐步 trace / baseline | `m_case_run_trace` / `m_case_baseline` | Nexus 的循环 |
| 设备身份 | `m_device` | **连通性来自 Scout 上报**，其余来自 UI |
| 节点 / 工作台归属 | `nodes` / `studios` | 活连接在内存 `NodeRegistry` |
| 账号 / 会话 | `users` / `sessions` / `auth_state` | UI 登录 |
| 设置 / 知识机审开关 | `settings` | UI |
| 知识条目 | `knowledge_entries` | UI / 学习沉淀 |
| 调度流水 | `dispatch_calls` | LLM / pipeline |
| Session 轨迹 | `session_events` / `session_meta` | Agent 循环双写 |
| 能力目录 | `catalog_entries`（`kind` 区分五类） | 空库不灌；只经 Console `/packs` 写入 |
| NavFSM（导航图配置） | `nav_fsm` / `nav_fsm_states` / `nav_fsm_edges` | Studio / `PUT /api/nav-fsm/{app_id}`；见 [NAVIGATION_ATLAS.md](NAVIGATION_ATLAS.md)。键：`app_id` + `project_id`（来自 `apps`） |
| NavFSM 校准证据（文件） | `{data_dir}/nav/calibration/{app_id}/{calibration_id}/` | 与 `mino.db` 同根；不进 git、不进 ORM 正文 |
| 技能 | `skills`（角色 + SOP + prompt + `view.id`） | builtin 从代码灌种一次，之后以库为准；坏行回退 `ai/skill_defs.py` |
| 图谱别名 | `m_atlas_alias` | UI |
| 集成插件策略 / 用户密钥 | `plugin_policies` / `user_plugin_secrets` | UI |
| Studio 侧栏 | `studio_nav` | Console |
| Scout 安装凭证 | `install_tokens` | Studio 领取 |
| 轻量任务 | `tasks` | `rTask` |
| 节点 manifest | Nexus 内存缓存 | **Scout 的 `REGISTER` / `HEARTBEAT`** |
| 原始截图 / 屏幕流 | **Scout 本地**，用后即删 | Scout |
| trace 缩略图 | Nexus 内存 `_RUNS` + `session_events` | Nexus |

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
| **`session_events` + `session_meta`** | append-only 轨迹（见 [9月8日_Session_Event_Log.md](9月8日_Session_Event_Log.md)） | DB，永久 | `GET /case-runner/sessions/{id}/events` · `/trajectory` · `/llm` |
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
