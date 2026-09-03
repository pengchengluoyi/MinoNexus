# DATA_MODEL — 数据模型与归属

**Nexus 是唯一的数据归属方。** Scout 不碰数据库（`../MinoScout/scripts/verify_no_orm.py` 守门）。

## 1. 归属总表

| 数据 | 存在哪 | 权威来源 |
|---|---|---|
| `App` / 项目 / 环境 | Nexus DB | UI / 飞书同步 |
| 用例、前置、预期 | Nexus DB | 飞书同步 |
| `AppRegressionRun`（批次结果） | Nexus DB | Nexus 的循环 |
| `TaskTimeline` / `WorkflowLog` | Nexus DB | Nexus 的循环 |
| `MDevice`（设备） | Nexus DB | **连通性来自 Scout 上报**，其余来自 UI |
| 能力目录 | `plugins/**.yaml` | 本仓，唯一真源 |
| 知识 / 覆盖度 / 号池 | Nexus DB | 知识捕获 + 人工审核 |
| 节点 manifest | Nexus 内存缓存 | **Scout 的 `REGISTER` / `HEARTBEAT`** |
| 原始截图 / 屏幕流 | **Scout 本地**，用后即删 | Scout |
| trace 缩略图 | Nexus 内存 `_RUNS` + 落库 | Nexus |

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

## 3. trace 的两处存储

| 存储 | 内容 | 生命周期 | 谁读 |
|---|---|---|---|
| `agent_stream._RUNS` | 每步的 thumb + decision + status | 内存，**上限 20 个 run** | `GET /case-runner/agent/{runs,steps}` |
| `AppRegressionRun` | 批次级结论 + 用例级结果 | DB，永久 | `GET /case-runner/tasks/{id}` |

`task_store` 读取顺序：先内存，兜底 DB。

**已知隐患**：`_RUNS` 上限 20 是单机假设。多节点并发时会被挤爆，早期 run 的逐步 trace 会消失（批次结论仍在 DB 里）。处理方向：按 `node_id` 分桶，或改成落库 + 短 TTL 内存缓存。**未决，见 §5。**

## 4. 从 sqlite 到 PG

上游是 `APP_DATA_DIR` 下的 sqlite（`server/core/database.py`），配 `migration.py` 自动迁移。

搬迁建议：**先沿用 sqlite 把双进程链路跑通，PG 单列一个阶段。** 换 PG 时要处理：

- 15 个 ORM 模型的方言差异（JSON 字段、自增主键、大小写）
- `migration.py` 的自动迁移逻辑重做（建议换 alembic）
- sqlite PRAGMA 相关的连接配置（`_set_sqlite_pragma`）作废
- 并发写：sqlite 靠 `_with_db_retry` 硬扛，PG 下应改成正常事务

## 5. 未决项

| # | 事项 | 影响 |
|---|---|---|
| 1 | `_RUNS` 上限 20 在多节点下的处理 | 多节点并发时早期 run 的逐步 trace 丢失 |
| 2 | sqlite → PG 的时机 | 上云的前置条件 |
| 3 | 循环状态（跑到第几步、history）在内存 | Nexus 多实例需要 sticky routing 或状态外置 |
| 4 | 截图归档策略 | 当前 Scout 用后即删，只有 thumb 留在 Nexus。若要复盘原图需要对象存储 |
