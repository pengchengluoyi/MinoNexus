# 06 — 执行能力与 Web 槽位

## 1. Executor 模型（Scout 内）

Scout 启动时装配 executor 字典，REGISTER 上报每个 executor 的：

- `id`（adb / playwright / …）
- `available` + `reason`
- `provides`（抽象能力名，供 Nexus catalog 过滤）

Nexus `router_proxy` 按 **设备 platform / sn** 合成 `executor_order`：

- Android / iOS：**不含** playwright
- Web 槽：**仅** playwright

循环侧只发 `EXECUTE`；Scout `core.execute` → 具体 executor。

## 2. 设备发现

- **adb / iOS / remote**：周期性 manifest 探测，变化时发 `node.device_*` 框架事件。
- **Web 槽**：不插 USB；`sn = web{scout_id}`，platform 为 web/playwright。

旧名 `web-local`：无活节点上报时 Nexus 清理残留登记。

## 3. Playwright Hub（并发预期）

实现要点（MinoScout `playwright_hub.py` + `core.py`）：

- Playwright **sync API 绑定线程**：Hub 在 **4 条 Playwright 专用单线程 shard**（`PLAYWRIGHT_PARALLEL_LANES = 4`，不可通过环境变量改）里启动实例。
- 每节点通常 **一个 Web sn**（`web{node_id}`）；同一 sn 上 **每个 run_id 独立 Context/Page**（会话键 `sn::run_id`），共享一条 Chromium Browser 实例（按 sn、按 PW worker 线程）。
- `core` 按 **run_id 哈希** 固定到 4 条 shard，保证同 run 的步骤总在同一 Playwright 线程上执行。
- `cancel_run` / 任务结束会 `release_run(run_id)`，只关该 run 的 Context，不误关其它并行 run。

因此：

| 问题 | 结论 |
|------|------|
| 同一节点多 Web 任务并行？ | **支持**（多 run 多 Context；受 PW 线程池与内存限制） |
| Nexus 设备占用？ | Web 槽 **最多 4 路** 并行：`WEB_PLAYWRIGHT_PARALLEL_LANES`；满员 409 `web parallel full`；真机仍独占 |
| Studio 新建执行？ | Web 槽不因 `busy_task_id` 挡第二单；列表展示 `active_run_count` |
| 资源上限？ | headed 1280×800 每 Browser/Context 常见 **数百 MB～1GB+**；并行 run 数宜保守 |
| browser 层未装完？ | `heavy_deps` 后台拉取；probe 可能 unavailable，Web 用例会 decline/fail |

## 4. 与跑批策略的建议

- **Web 回归**：同一 Scout 节点可 **多任务并行**（不同 run_id）；账号/数据隔离仍按号池租约与用例设计。
- **真机**：按 serial 并行，受节点数与 adb 带宽限制；**一机一单**。
- **节点就绪**：除 `alive` 外，对 Web 任务应看 REGISTER 里 **playwright available** 与 `layers.txt` 是否含 browser 层。

## 5. 能力目录（Nexus）

Scout **不读** `catalog_entries`。Nexus 算完实现路径后塞进 `EXECUTE`（`selected_impl`、`device_hint` 等）。  
加能力见 [CAPABILITY_CATALOG.md](../基础框架/CAPABILITY_CATALOG.md)——多数仅改 YAML，Scout 侧走已有 executor 路径。

## 6. 非设备 executor（仍在 Nexus）

以下 **不会** 发到 Scout：

| executor | 用途 |
|----------|------|
| hitl | 人工 |
| vlm | 视觉 LLM 断言 |
| ai_persona | 子事件编排 |
| internal | wait_ms / noop |

误发到 Scout 会导致 supports=false 与白跑 fallback。
