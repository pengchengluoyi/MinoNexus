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

实现要点（MinoScout `playwright_hub.py`）：

- Playwright **sync API 绑定线程**：Case  worker 线程内一个 Playwright 实例。
- **每个 sn** 一个 Chromium Browser；`open_case` 对该 sn **单 Context / Page**（新开 case 会先 `close_case`）。
- 每节点通常 **只有一个 Web sn**（`web{node_id}`）。

因此：

| 问题 | 结论 |
|------|------|
| 同一节点多 Web 用例并行？ | **共享同一浏览器/页面模型**，不是多 Chromium 并行；多 run 会争用 |
| 要提高 Web 并行度？ | 多 Scout 节点、或未来 Hub 多 context/多 browser + Nexus 派单改造 |
| 资源上限？ | 代码无硬 cap；headed 1280×800 单实例常见 **数百 MB～1GB+**，依页面而定 |
| browser 层未装完？ | `heavy_deps` 后台拉取；probe 可能 unavailable，Web 用例会 decline/fail |

## 4. 与跑批策略的建议

- **Web 回归**：默认按 **单节点单 Web 槽串行** 规划用例与设备占用。
- **真机**：按 serial 并行，受节点数与 adb 带宽限制。
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
