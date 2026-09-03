# ARCHITECTURE — MinoNexus

## 1. 全局

```
┌──────────┐   HTTP :10104   ┌───────────────────────┐   WS /node   ┌──────────────────┐
│ Electron │ ──────────────► │      MinoNexus        │ ◄──────────  │    MinoScout     │
│    UI    │ ◄── WS 推送 ──── │  循环 / LLM /          │   (Scout     │  设备操作         │
└──────────┘                 │  能力目录 / 数据库      │    主动连)    └────────┬─────────┘
                             └───────────────────────┘                       │
                                                                          设备
```

UI 只跟 Nexus 说话，端口仍是 `10104`。Scout 主动 dial 进来，可能有 0 个、1 个或多个。

## 2. 四层套娃

上游 MiniOrangeServer 的执行分四层，拆分后归属如下：

| 层 | 做什么 | 归属 |
|---|---|---|
| ① 任务编排 | 多设备、多用例、闸门、落库、WS 推送 | **Nexus** |
| ② 单用例循环 | observe → decide → dispatch → re-observe | **Nexus** |
| ③ 动作分发 | 选 executor、可选 locate、fallback | **拆开**：选路 Nexus，尝试 Scout |
| ④ 设备动作 | adb / remote / ios_wda / playwright | **Scout** |
| ④' 非设备动作 | hitl / vlm / ai_persona / internal | **Nexus**（不出网） |

③ 为什么拆：选哪个 executor 需要知道能力目录和当前连通性（Nexus 有），依次尝试并处理失败是纯执行（Scout 做）。Nexus 算出 `executor_order` 塞进 `EXECUTE`，Scout 照单执行。

## 3. 循环

```
AgentExecutor.run()                       ← mino_nexus/loop/agent_executor.py
  每一步：
   1. screen = RouterProxy.observe(screenshot)  ──► OBSERVE ──► Scout
   2. dump   = RouterProxy.observe(hierarchy)   ──► OBSERVE ──► Scout
   3. decision = planner.decide_next_action(screen, menu, history)   本地 LLM
   4. 若 impl 声明 locate 且 params 无坐标：
        planner.locate_element(screen)                              本地 LLM
   5. cap 属于 hitl / vlm / ai_persona / internal ？
        是 → local_executors 本地执行，不出网
        否 → RouterProxy.dispatch(event)          ──► EXECUTE ──► Scout
   6. emit_agent_event(...) → _RUNS + broadcast_to_observers → UI WS
   7. 恢复判定：recovery.recover_if_needed(ctx, router_proxy)
```

### 3.1 `RouterProxy`：让循环代码不感知网络

上游 `agent_executor.py` / `recovery.py` / `orchestrator.py` 只依赖一个签名：

```python
result: EventResult = router.dispatch(event, ctx, ...)
```

`mino_nexus/loop/router_proxy.py` 提供**同签名**实现，内部：

```
dispatch(event, ctx)
  ├── 查能力目录：这条 cap 在当前 connectivity 下允许哪些 executor
  ├── 合成 executor_order（AI 的 expected_executor → fallback_executors → 菜单兜底）
  ├── 从 YAML 取 low_level / selected_impl
  ├── 注入 device_hint（sn → adb_serial / password，带 TTL）
  ├── 发 EXECUTE，等 RESULT（超时按 PROTOCOL.md §6 重发一次）
  └── RESULT → EventResult（五态、attempts、executor_used 原样透传）
```

**因此上游那 3,533 行循环代码基本原样搬过来即可**，改的是注入进去的对象。这是本次拆分最省力的一处，也是 `CLAUDE.md` §2.2 那条铁律的由来。

同理 `capture_screen(...)` 的调用点改为 `RouterProxy.observe(...)`，返回值保持 `CapturedScreen` 形状。

### 3.2 trace 与 UI：零改造

`agent_stream.emit_agent_event()` 的唯一调用方是 `agent_executor.py`。它做两件事：

1. 写进程内 `_RUNS`（`OrderedDict`，上限 20 个 run）
2. 广播 `{"type": "agent_step", data}` 给 UI observers

因为循环留在 Nexus，**trace 天然写在 UI 要读的这个进程里**。`agent_stream.py` 一行不改，UI 的 `/case-runner/agent/runs`、`/agent/steps/{run_id}`、WS `agent_step` 一行不改。

> 这是选定「循环留 Nexus」方案的一个直接收益。若循环放在 Scout，就需要一整套 trace 回流管道（emitter / sink / 断连补传 / 缩略图归属），并且 UI 在 trace 到达前什么都看不到。

**已知的多节点隐患**：`_RUNS` 上限 20 是单机假设。多节点并发时会被挤爆 —— 要么按 `node_id` 分桶，要么改成落库 + 短 TTL 内存缓存。见 `docs/DATA_MODEL.md`。

## 4. 能力菜单怎么产生

```
Scout REGISTER 上报 executors[].provides（abstract cap 字符串）
        │
        ▼
catalog/registry：provides ∩ capabilities[].implementations[].requires_caps
        │
        ▼
filter_capabilities_by_connectivity()  按当前通道瘦身
        │
        ▼
tool_schema：capability → OpenAI function tools
        │
        ▼
planner.decide_next_action 的可选动作集
```

**关键：菜单是 Scout 报的能力与 Nexus 目录的交集，不是 Nexus 猜的。** 一台没装 WDA 的 Scout 上，iOS 相关实现不会进菜单，LLM 也就不会选它。

详见 [CAPABILITY_CATALOG.md](CAPABILITY_CATALOG.md)。

## 5. 派单

一次 `POST /case-runner/run` 携带 `sns[]`。Nexus 需要回答"这台设备在哪个节点上"：

```
sn ──► 设备表的 node_id 归属 ──► 该节点的连接 ──► OBSERVE / EXECUTE
```

`node_id` 是拆分新引入的概念（上游只有 `sn`，它既是设备标识又是连接端点）。规则、冲突处理、无节点时的行为见 [NODE_REGISTRY.md](NODE_REGISTRY.md)。

## 6. 无 Scout 时的行为

Nexus **必须**能独立启动。无可用节点时：

| 场景 | 行为 |
|---|---|
| 启动 | 正常起，`:10104` 可用 |
| 设备列表 | 显示已登记设备，但连通性全为 `disconnected`，并标注"节点离线" |
| 下发批次 | 拒绝并给明确原因（"设备 X 所属节点 mac-studio-01 离线"），**不要静默排队** |
| 旁路功能（知识、QA 流程、用例同步） | 全部可用 —— 它们不需要设备 |

## 7. 状态

| 数据 | 存在哪 | Nexus 重启后 |
|---|---|---|
| 用例、任务、设备、知识、覆盖度 | DB | 保留 |
| 能力目录 | `plugins/**.yaml` + 内存 registry | 重载 |
| 循环状态（当前跑到第几步、history、memory） | 进程内存 | **丢失，在途 run 判为中断，不续跑** |
| trace `_RUNS` | 进程内存，上限 20 | 丢失（`AppRegressionRun` 里有落库副本） |
| 节点 manifest / 连通性 | 内存缓存 | 等 Scout 重新 `REGISTER` 恢复 |

循环状态在内存意味着**多实例需要 sticky routing 或状态外置**。单机不涉及，先记账。
