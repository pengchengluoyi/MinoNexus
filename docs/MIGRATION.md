# MIGRATION — 从 MiniOrangeServer 搬到这里

> 上游 `../MiniOrangeServer` **已停止维护，只读，禁止改动**。本仓是它的一半。
> 本清单由 `../MiniOrangeServer` 于 2026-09-02 的全量文件（392 个 `.py`）分类而来，**无待定项**。

## 0. 总账

| 去向 | 文件 | 行数 |
|---|---|---|
| → MinoNexus | 186 | 60,165 |
| → MinoScout | 67 | 12,424 |
| 不搬（随上游归档） | 138 | 30,358 |
| 合计 | 392 | 102,947 |

不搬的 30,358 行是旧路径：`services/executor/`（旧 Plan 循环，`plan_execute_service` 零调用方）、`copilot_service.py`、`services/local/{locate,navigation,overlay,plan}`、`core/vision/`、`core/local_brain.py`、`services/shared/page_context/`、`feishu_regression_service.py`、`driver/` 的非 engine 部分。

**这批代码里含 `torch` / `open-clip-torch` / `paddleocr` / `paddlepaddle` / `ultralytics` / `opencv`。不搬 = 两个新仓库都不用养这些依赖。**

依据（实测于上游）：
- 批次执行路径运行时对 `local/locate`、`page_navigation`、`core/vision` **零调用**（`grep -rn` 在 `server/services/regression/` 下 0 命中）
- 定位走 `ai/regression/planner.py:398 locate_element()`，**纯 VLM**，不经 CLIP / OCR
- `services/executor/plan_execute_service` 无任何调用方

## 1. 搬迁时必须断开的四条边

上游的 live path 通过几处 lazy import 把上万行旧代码拖进依赖闭包。**搬迁不是复制粘贴，这四处要改写 import 指向。**

| # | 上游位置 | 实际用到的东西 | 不处理会拖进来 | 怎么改 |
|---|---|---|---|---|
| **E1** | `case_runner.py:66,419,719` → `app_automation_service` | 3 处 lazy import，含 `persist_run_finish`（落库） | `copilot_service` 4,411 行 → `clip_locate_service` → `clip_service`（**torch**） | Nexus 里独立成 `services/persistence/run_finish.py`，不带 copilot |
| **E2** | `case_precondition_service.py:355` → `page_navigation_service._screen_is_login_home` | **一个私有函数** | `page_navigation_service` 3,159 + `services/executor/execute_steps` 751 | 抽成独立小工具函数 |
| **E3** | `screen.py:381` + `remote_executor.py:138` → `driver/agent/Crawl/device_bootstrap.bootstrap_mobile_engine` | 2 个调用点，要一个 engine 对象 | 整个 `driver/tentacle` 引擎层 | Scout 里提炼成 `engines/factory.py::EngineFactory`。`driver/agent/Crawl` 这个位置在新仓库不存在 |
| **E4** | `case_precondition_service.py:295,356` + `app_automation_service.py:595` → `page_context_service` 的 `_collect_full_screen_text` / `_identify_page_by_screen_keywords`；`screen_frame_service.py:56` → `_shot_to_bgr` | 3 个私有函数 | `page_context_service` 804 行（连带 `core/vision`、`local/*`、`figma_*`） | 三个函数各自抽成独立小工具；`_shot_to_bgr` 直接内联 |

> E1 / E2 / E4 是**意外耦合**：几个私有工具函数和一个落库函数，合计拖进约 9,000 行和整个 torch 栈。E3 是真实功能依赖，需要提炼而非切断。

## 2. 搬到本仓（MinoNexus）的文件

| 上游目录 | 文件 | 行数 | 文件名 |
|---|---|---|---|
| `server/services/` | 38 | 19,338 | `__init__.py`, `account_issue_service.py`, `account_tag_service.py`, `agent_session_store.py`, `app_automation_service.py`, `auth_service.py`, `case_precondition_service.py`, `cover_import.py`, `crawl_job_manager.py`, `crawl_persistence.py`, `device_service.py`, `execution_clarification_service.py`, `failure_knowledge_service.py`, `feishu_service.py`, `feishu_wiki_service.py`, `feishu_ws_listener.py`, `feishu_ws_worker.py`, `figma_icon_service.py`, `figma_logic_service.py`, `figma_service.py`, `im_bot_service.py`, `im_command.py`, `im_prompts.py`, `knowledge_briefing.py`, `knowledge_capture_service.py`, `knowledge_facts.py`, `knowledge_review_service.py`, `knowledge_situation.py`, `mail_service.py`, `memory_context.py`, `project_env.py`, `qa_process_assist.py`, `qa_process_jobs.py`, `qa_role_jobs.py`, `run_service.py`, `skills_registry.py`, `system_settings_service.py`, `wechat_ilink_service.py` |
| `server/services/regression/` | 13 | 8,930 | `__init__.py`, `agent_executor.py`, `agent_memory.py`, `agent_stream.py`, `case_runner.py`, `coverage_codes.py`, `expect_catalog.py`, `orchestrator.py`, `persona_remote_lifecycle.py`, `pick_device.py`, `recovery.py`, `router.py`, `task_store.py` |
| `server/services/ai/` | 14 | 5,956 | `__init__.py`, `app_atlas.py`, `app_profile.py`, `atlas_alias_repo.py`, `atlas_align.py`, `atlas_from_mindmap.py`, `concurrency.py`, `dispatch_log.py`, `execution_profile.py`, `layer_stack.py`, `playbook_service.py`, `role_plugin_graph.py`, `role_router.py`, `roles_catalog.py` |
| `server/routers/` | 20 | 5,503 | `__init__.py`, `rAbility.py`, `rAppAutomation.py`, `rAppGraph.py`, `rAuth.py`, `rCaseRunner.py`, `rClawNode.py`, `rDevice.py`, `rFeishuRegression.py`, `rFile.py`, `rHitl.py`, `rImWebhook.py`, `rLog.py`, `rPacks.py`, `rProject.py`, `rSchedule.py`, `rSettings.py`, `rTask.py`, `rWorkflow.py`, `rWorkflowRun.py` |
| `server/services/ai/regression/` | 5 | 4,333 | `__init__.py`, `llm_client.py`, `planner.py`, `prompts.py`, `schemas.py` |
| `server/websocket/` | 6 | 2,529 | `__init__.py`, `device_manager.py`, `rWebsocket.py`, `wsFile.py`, `wsMap.py`, `ws_handlers.py` |
| `server/services/runtime/` | 10 | 2,226 | `__init__.py`, `app_query.py`, `channels.py`, `device_bind.py`, `device_provision.py`, `env_gate.py`, `menu.py`, `qa_process_lock.py`, `run_context.py`, `session_gate.py` |
| `server/websocket/routers/` | 5 | 1,763 | `__init__.py`, `wAppGraph.py`, `wClawNode.py`, `wCopilot.py`, `wNode.py` |
| `server/services/plugins/` | 6 | 1,594 | `__init__.py`, `compat.py`, `loader.py`, `models.py`, `registry.py`, `tool_schema.py` |
| `server/services/regression/executors/` | 4 | 1,241 | `ai_persona_executor.py`, `hitl_executor.py`, `internal_executor.py`, `vlm_executor.py` |
| `server/services/shared/semantic/` | 3 | 1,139 | `__init__.py`, `case_text_semantic_service.py`, `expectation_semantic_service.py` |
| `server/services/regression/case_memory/` | 5 | 914 | `__init__.py`, `align.py`, `repo.py`, `service.py`, `windows.py` |
| `server/core/` | 7 | 796 | `__init__.py`, `database.py`, `gateway_beacon.py`, `log_database.py`, `migration.py`, `scheduler.py`, `security.py` |
| `server/services/packs/` | 3 | 634 | `__init__.py`, `exec_classes.py`, `store.py` |
| `server/services/ai/plan/` | 2 | 567 | `__init__.py`, `prompt.py` |
| `server/services/shared/run_context/` | 3 | 529 | `__init__.py`, `regression_run_context.py`, `regression_run_report.py` |
| `server/services/regression/hitl/` | 4 | 508 | `__init__.py`, `schemas.py`, `session.py`, `transport.py` |
| `server/services/resources/` | 4 | 453 | `__init__.py`, `catalog.py`, `gateway.py`, `lease.py` |
| `server/models/` | 13 | 417 | `__init__.py`, `app_icon_target.py`, `app_regression_run.py`, `atlas_alias.py`, `case_baseline.py`, `log.py`, `mDevice.py`, `project.py`, `schedule.py`, `task.py`, `timeline.py`, `workflow.py`, `workflow_run.py` |
| `server/models/AppGraph/` | 4 | 279 | `__init__.py`, `app_component.py`, `app_structure.py`, `app_types.py` |
| `server/services/ai/cover/` | 2 | 153 | `__init__.py`, `checks.py` |
| `script/` | 6 | 151 | `__init__.py`, `log.py`, `mPath.py`, `mTask.py`, `singleton_meta.py`, `sleep.py` |
| `script/constPath/` | 3 | 75 | `__init__.py`, `component_code.py`, `error_code.py` |
| `server/schemas/` | 3 | 70 | `__init__.py`, `run.py`, `workflow.py` |
| `server/services/shared/` | 2 | 67 | `__init__.py`, `execution_profile.py` |
| `server/` | 1 | 0 | `__init__.py` |
| `server/services/shared/screenshot/` | 1 | 0 | `__init__.py` |

### 落位映射

| 上游 | 本仓 |
|---|---|
| `server/routers/*` | `mino_nexus/routers/*`（保持 `r*` 命名） |
| `server/websocket/*` | `mino_nexus/websocket/*`，**新增 node 接入侧**（REGISTER / HEARTBEAT / 按 `sn` 路由到 `node_id`） |
| `server/services/ai/**` | `mino_nexus/ai/**` |
| `server/services/plugins/*` + `plugins/**.yaml` | `mino_nexus/catalog/*` + `catalog_entries`（空库 `builtin_seed`） |
| `server/services/regression/agent_executor.py` | `mino_nexus/loop/agent_executor.py`（**代码基本不动**，见下） |
| `server/services/regression/executors/{hitl,vlm,ai_persona,internal}_executor.py` | `mino_nexus/loop/local_executors/*`（零设备访问，不出网） |
| `server/services/regression/router.py` 的 locate 半边 | `mino_nexus/loop/locate.py` |
| `server/models/*` + `server/core/{database,migration,security,log_database}` | `mino_nexus/models/*` + `mino_nexus/core/*` |
| `script/log.py` 等 | `mino_nexus/log.py` |

### `RouterProxy`：让循环代码不用改

上游 `agent_executor.py` / `recovery.py` / `orchestrator.py` 只依赖一个签名：

```python
result: EventResult = router.dispatch(event, ctx, ...)
```

本仓提供 `mino_nexus/loop/router_proxy.py`，**签名与上游 `CapabilityRouter.dispatch` 完全一致**，内部把 event 序列化成 `EXECUTE` 发给 Scout、等 `RESULT`。

因此 `agent_executor.py`（3,533 行）、`recovery.py`（319 行）搬过来后**基本不用改** —— 改的是注入进去的对象。这是本次拆分最省力的一处。

同理 `capture_screen(...)` 的调用点改为 `RouterProxy.observe(...)`，签名对齐 `CapturedScreen`。

### `agent_stream.py` 原样保留

上游 `emit_agent_event`（`agent_stream.py:100`）唯一调用方是 `agent_executor.py`，它做两件事：写进程内 `_RUNS`、广播给 UI observers。

因为执行循环留在 Nexus，**这个文件一行都不用改，UI 的 `/agent/runs`、`/agent/steps/{id}`、WS `agent_step` 也一行都不用改。**

### `device_manager.py` 只搬一半

见 MinoScout 的 `docs/MIGRATION.md` 同名小节。本仓保留 UI observers 广播 + 新写 Scout 节点登记；ClawNode 连接管理整段归 Scout。

## 3. 归 MinoScout 的部分

见 `../MinoScout/docs/MIGRATION.md`。两仓的清单互补，合起来覆盖上游全部 392 个文件。

## 3.1 对 §2 分类表的修正

`server/services/runtime/app_query.py`（113 行）原判归 Nexus，实际**归 Scout** ——
它是纯字符串解析（`parse_package_version` / `parse_foreground` / `FOREGROUND_SHELL`），
被 `adb_executor` 的 `get_app_version` / `get_foreground_app` 直接依赖。
详见 `../MinoScout/docs/MIGRATION.md` §6。

Nexus 侧因此少 113 行：186 文件 / 60,052 行。

## 4. 已知行为变化

| # | 变化 | 影响 |
|---|---|---|
| 1 | **ClawNode 的连接对象从 server 改为 Scout** | 配对配置（`ws_url` / `auth_token` / `gateway_id`）要指向 Scout。**需要与 `../ClawNode` 仓库协同改动**，不是任一新仓库能单方面完成的 |
| 2 | 单机从一个进程变成两个进程 | `../MiniOrange`（Electron）的 `electron/main.js` 要 spawn 两个二进制，并把两个都纳入现有 `taskkill` / `pkill` 清理逻辑（上游 `main.js:69-91`）。UI 端口仍是 `10104`，`vite.config.js:9` 不用改 |
| 3 | Scout 起不来时 | Nexus 必须能独立启动，并在 UI 上明示"无可用执行节点"，而不是整个后端起不来 |
| 4 | `sn` 之外新增 `node_id` | 设备表要加 `node_id` 归属，否则 Nexus 答不出"这台设备派给哪个节点"。见 Nexus 的 `docs/NODE_REGISTRY.md` |
| 5 | 不再有进程内直调 | 上游 `driver/agent/in_process_server_query.py` 那套 `builtins.SERVER_QUERY` 注入不再需要，Scout 通过协议拿一切 |

## 5. 验收

搬迁完成的判据不是"编译通过"，而是**行为对齐**：

1. 在上游 MiniOrangeServer 上跑一条批次用例，存档时间线、每步截图、覆盖度结论、总耗时
2. 在 Nexus + Scout 双进程上跑同一条用例
3. 逐项对照：步骤数、每步 capability 与 executor、断言结论、覆盖度码、UI 表现

**耗时允许劣化（多了两次 WS 往返），结论不允许变。**
