# Session Event Log — 设计方案

**日期**：2026-09-08  
**版本**：v0.1.4 起规划，分阶段落地  
**状态**：设计稿（未实现）

---

## 1. 背景与动机

MinoNexus 的 Agent 循环（`agent_loop.py`）已能跑通 prep/do/check，但**轨迹数据分散在三处**，无法作为 harness 的真源：

| 现状 | 存什么 | 问题 |
|------|--------|------|
| `agent_stream._RUNS` | 步骤摘要 + thumb | 进程内存，上限 40 run × 200 event，**重启即丢** |
| `dispatch_log` / `dispatch_calls` | LLM 请求与响应 | 与 tool 执行、phase、guard **不在同一条链** |
| `app_regression_runs` + `m_case_run_trace` | 批次/用例结论 | 只有结果，**无法 replay / fork** |

对标现代 Agent Harness（如 DeepSeek Harness）的核心差异不是「有没有 loop」，而是：

> **Model-visible means logged** —— 凡进入模型上下文或影响下一步决策的事实，都必须 append 到同一条 Session Event Log，并可从 log **投影**出模型所见与 UI 轨迹。

本方案定义 MinoNexus 的 Session Event Log：**append-only、可重建、可评测**，且不破坏现有 Scout 协议与「循环不感知网络」铁律。

---

## 2. 目标与非目标

### 2.1 目标

1. **单一真源**：一次 `run_case` 对应一条 `session_id`（可与现有 `report_run_id` / `cr-xxx::case-yyy` 对齐）。
2. **完整轨迹**：screenshot ref、菜单快照、LLM 调用、tool call/result、guard/recovery、phase 切换均可串联。
3. **投影接口**：从 log 生成 Studio Trajectory、dispatch 调试视图、（后续）eval metrics。
4. **持久化**：落 sqlite（主）+ 可选 blob 外置（大图），多实例重启不丢。
5. **渐进迁移**：第一期双写（log + 现有 `agent_stream`），读侧逐步切到 log。

### 2.2 非目标（第一期不做）

- 不做 Cordis 式插件树；hook 仍用 Python 函数注册表。
- 不做 subagent fork 的完整产品 UI（只预留 event 类型与 `parent_session_id`）。
- 不把原图长期归档到 Nexus（仍遵循 Scout 用后即删；log 只存 thumb ref + hash）。
- 不替代 `app_regression_runs` 的批次汇总语义。

---

## 3. 核心概念

```
Session          一次 run_case（或等价 agent 任务）的全生命周期
Turn             一轮「可结束」的决策单元（通常 = agent_loop 里的一次 decide 循环）
Step             Turn 内的一个原子动作（一次 LLM 请求，或一次 tool 执行）
Event            append-only 记录；Turn/Step 由 event 类型与 seq 推导
Projection       从 event 列表推导出的只读视图（给 UI / eval / 调试）
```

与 DeepSeek Harness 对齐的本地铁律：

> **凡是进了 `decide_next_action` / `verify_step_expected` 的 messages 片段，或改变了 `StepCursor` / `ctx` 的执行事实，必须对应至少一条 Session Event。**

---

## 4. Event 模型

### 4.1 公共字段

每条 event 共有：

```json
{
  "session_id": "cr-f2466cc255b4::case-demo-001",
  "seq": 42,
  "ts": "2026-09-08T11:40:00+08:00",
  "type": "tool/result",
  "turn": 7,
  "phase": "do",
  "payload": { }
}
```

| 字段 | 说明 |
|------|------|
| `session_id` | 主键维度；默认 `report_run_id(run_id, case_id)` |
| `seq` | 会话内单调递增，**只追加不重排** |
| `type` | 见 §4.2 |
| `turn` | 决策轮次，从 1 起 |
| `phase` | `prep` / `do` / `check` / `done` |
| `payload` | 类型相关 JSON；大图用 `blob_ref` 外置 |

### 4.2 Event 类型（第一期）

| type | 何时写入 | payload 要点 |
|------|----------|--------------|
| `session/start` | `run_case` 入口 | case_id, sn, app_id, sop_id, provider_id, seq_nodes 摘要 |
| `session/end` | `run_case` 出口 | status, summary, elapsed_ms, step_count |
| `turn/start` | 每轮 decide 前 | step_cursor 摘要, history_lines |
| `turn/end` | decide 完成或 skip | decision_cap, decision_status |
| `context/menu` | 菜单组装后 | phase, tool_kinds, cap_ids[], recovery_brief |
| `context/slots` | job_slots 填入后 | 槽名 → 字符串（**截断**；不含 image base64） |
| `observe/screen` | 截图成功后 | thumb_ref, screen_hash, w, h, mime |
| `llm/request` | 调 LLM 前 | job_id, messages 摘要（role+len+hash） |
| `llm/response` | 调 LLM 后 | raw_hash, parsed decision, tokens, latency_ms, dispatch_id |
| `tool/call` | dispatch 前 | capability_id, executor, params（坐标保留） |
| `tool/result` | dispatch 后 | EventResult：status, reason, evidence 摘要 |
| `guard/block` | registry 拦截 | rule_id, reason, rewritten_cap |
| `recovery/match` | recovery preflight / apply | rule_id, cap_id, match_reason |
| `inspection/done` | run_inspections 后 | job_id, session_block 摘要 |
| `phase/change` | SOP 阶段切换 | from, to, trigger |

第二期预留：`session/fork`, `subagent/start`, `subagent/end`, `cancel/request`, `llm/attempt`（失败重试）。

### 4.3 与 dispatch_log 的关系

- `dispatch_log.record_llm` **保留**；写入 `llm/response` 时携带 `dispatch_id` 互链。
- 逐步淘汰「从 dispatch 反推轨迹」；dispatch 专注 **LLM 运维**（设置页调度），session log 专注 **跑批轨迹**。

### 4.4 与 agent_stream 的关系

- 第一期：**双写**。`emit_agent_event` 内部 `session_log.append(...)` + 现有 `_RUNS`。
- 第二期：Studio `GET /case-runner/agent/steps` **优先读 log 投影**；`_RUNS` 降为短 TTL 缓存。
- 第三期：删除 `_RUNS` 上限逻辑或改为 log 的 LRU 索引。

---

## 5. 存储设计

### 5.1 表结构（sqlite）

新表 `session_events`（append-only）：

```sql
CREATE TABLE session_events (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id  TEXT NOT NULL,
  seq         INTEGER NOT NULL,
  ts          TEXT NOT NULL,
  type        TEXT NOT NULL,
  turn        INTEGER DEFAULT 0,
  phase       TEXT DEFAULT '',
  payload     JSON NOT NULL,
  UNIQUE(session_id, seq)
);
CREATE INDEX ix_session_events_session ON session_events(session_id);
CREATE INDEX ix_session_events_type ON session_events(type);
```

元数据表 `session_meta`（一行一会话）：

```sql
CREATE TABLE session_meta (
  session_id    TEXT PRIMARY KEY,
  run_id        TEXT NOT NULL,
  case_id       TEXT DEFAULT '',
  app_id        TEXT DEFAULT '',
  status        TEXT DEFAULT 'running',
  event_count   INTEGER DEFAULT 0,
  started_at    TEXT NOT NULL,
  finished_at   TEXT,
  format_version INTEGER DEFAULT 1
);
```

与现有表：

- `m_case_run_trace`：第一期仍写 `payload` 摘要；**详情以 session_events 为准**，trace 表逐步变为索引行。
- `app_regression_runs`：不变，批次级汇总。

### 5.2 Blob 外置（可选，第一期可只用 thumb）

路径：`{data_dir}/sessions/{session_id}/blobs/{hash}.webp`

- `observe/screen` 的 `thumb_ref` 指向 blob 文件或 inline base64（≤32KB 可 inline）。
- **禁止**在 event 行存全尺寸 PNG（sqlite 膨胀 + 违背 DATA_MODEL 截图策略）。

### 5.3 Retention

| 层级 | 策略 |
|------|------|
| 内存缓存 | 最近 N 个 running session 的热索引 |
| sqlite events | 默认永久；后续加按 app_id  TTL 清理 job |
| blobs | 与 events 同生命周期；清理 orphan blob 走后台 task |

---

## 6. 模块划分

```
mino_nexus/
├── loop/
│   ├── session_log.py      # append / flush / read_range（核心 API）
│   ├── session_project.py  # 投影：trajectory, llm_trace, metrics
│   └── agent_loop.py       # 调用点（见 §7）
├── models/
│   └── session_event.py    # ORM
├── services/
│   └── session_store.py    # DB 读写、meta 更新
└── routers/
    └── rCaseRunner.py      # GET .../sessions/{id}/events（扩展）
```

### 6.1 核心 API（草案）

```python
# loop/session_log.py

def open_session(*, session_id, run_id, case_id, ...) -> SessionWriter: ...

class SessionWriter:
    def append(self, type: str, payload: dict, *, turn: int, phase: str) -> int: ...
    def close(self, *, status: str, summary: str) -> None: ...

def read_events(session_id: str, *, from_seq=0, limit=500) -> list[dict]: ...
def project_trajectory(session_id: str) -> dict: ...  # Studio 用
def project_llm_calls(session_id: str) -> list[dict]:  # 对齐 dispatch
```

**线程安全**：`run_case` 单线程 async；batch 并发时每个 session_id 独立 writer，DB 写用 `session_id` 行锁或 sqlite 事务。

---

## 7. 接入点（agent_loop）

在 `run_case` / `_run_loop` 插桩，**不改变控制流**：

```
run_case
  ├─ session/start
  ├─ _run_loop
  │    ├─ [phase change] → phase/change
  │    ├─ run_inspections → inspection/done
  │    ├─ turn/start
  │    ├─ 组装 menu → context/menu
  │    ├─ job_slots → context/slots
  │    ├─ screenshot → observe/screen
  │    ├─ decide_next_action
  │    │    ├─ llm/request, llm/response  （在 planner 或 loop 包装）
  │    ├─ run_guards → guard/block（若触发）
  │    ├─ tool/call → dispatch → tool/result
  │    ├─ recovery → recovery/match
  │    └─ turn/end
  └─ session/end
```

`planner.py`：在 `_chat` 前后各 append 一条；messages 存 **hash + 长度 + job_id**，完整正文仍走 `dispatch_log`（或通过 `dispatch_id`  join）。

---

## 8. HTTP / Studio

扩展 `rCaseRunner`（请求模型保持模块级）：

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/case-runner/sessions/{session_id}/events` | 分页 raw events |
| GET | `/case-runner/sessions/{session_id}/trajectory` | 投影后的步骤时间线 |
| GET | `/case-runner/sessions/{session_id}/llm` | LLM 调用列表（链 dispatch_id） |

现有 `GET /case-runner/agent/steps?run_id=` **兼容**：内部改为 `project_trajectory(session_id)`，字段形状与现 Studio 对齐。

WebSocket：`emit_agent_event` 仍 broadcast；payload 可带 `seq` 便于 UI 与 log 对齐。

---

## 9. Eval Harness（第三期）

Session Log 落地后，可增 `scripts/eval_case_smoke.py`（或独立包）：

```yaml
# benchmark 概念稿
cases:
  - case_id: case-demo-001
    app_id: "..."
    expect:
      status: pass
      max_steps: 20
      tools_must_include: [lease_account]  # 可选
metrics:
  - task_success
  - step_count
  - llm_tokens
  - invalid_tool_calls
  - recovery_hits
```

指标从 `session_events` 聚合，不依赖内存 `_RUNS`。

---

## 10. 分阶段实施

| 阶段 | 内容 | 验收 |
|------|------|------|
| **P0** | 表 + `session_log.append` + agent_loop 双写 | 跑完一例可在 DB 看到 ≥20 条 events；重启后仍在 |
| **P1** | `project_trajectory` + HTTP + Studio 读 log | Studio 步骤与现网一致；dispatch_id 可跳转 |
| **P2** | planner 全量互链、guard/recovery 全覆盖 | 「为何 ask_human」单 session 可解释 |
| **P3** | eval 脚本 + fork/replay（CLI） | 同一 fixture 改 prompt 可 A/B 对比 step_count |
| **P4** |  deprecate `_RUNS` 或仅作缓存 | DATA_MODEL §3 隐患 #1 关闭 |

---

## 11. 风险与约束

1. **体积**：严格 thumb + hash，禁止 full screenshot 进 sqlite。
2. **性能**：每 turn 约 8–12 条 event；24 step 用例 ≈ 300 行 JSON，可接受。
3. **多实例**：session writer 跟 `run_case` 进程走；若未来 Nexus 水平扩展，需 sticky session 或 writer 只写 DB（已满足）。
4. **隐私**：`context/slots` 不含账号明文；`accounts_brief` 沿用现有脱敏规则。
5. **协议**：Session Log 纯 Nexus 内部，**不同步 Scout**。

---

## 12. 开放问题

| # | 问题 | 倾向 |
|---|------|------|
| 1 | `session_id` 用 `report_run_id` 还是新 UUID | 沿用 `report_run_id`，减少 Studio 改动的 |
| 2 | messages 完整正文是否进 event | 否，hash + dispatch_id；详情查 dispatch_calls |
| 3 | `m_case_run_trace.payload` 是否废弃 | P2 后仅留索引字段 |
| 4 | blob 文件是否上对象存储 | 上云阶段再议；单机先本地目录 |

---

## 13. 参考

- 本仓：`docs/DATA_MODEL.md` §3 trace 两处存储、`mino_nexus/loop/agent_stream.py`、`mino_nexus/ai/dispatch_log.py`
- 外部：DeepSeek Harness — Session Log、`deriveMessages()`、Model-visible means logged（`docs/architecture.md`）

---

**下一步（P0 开工清单）**

1. `models/session_event.py` + `migration.py` 加表  
2. 实现 `loop/session_log.py`  
3. `agent_loop.run_case` 双写 5 个关键 type：`session/start|end`, `turn/start|end`, `tool/call|result`  
4. 单测：`append` 单调 seq、投影非空  
5. 更新 `docs/DATA_MODEL.md` §3 指向本文件  
