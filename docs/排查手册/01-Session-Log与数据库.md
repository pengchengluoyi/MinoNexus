# Session Log 与数据库

## 1. 真源是谁

一次 `run_case`（单条用例的一次执行）对应 **一条 Session**，轨迹 append 到 SQLite，**重启 Nexus 不丢**。

| 存储 | 表 / 对象 | 作用 |
|------|-----------|------|
| **Session 轨迹（主）** | `session_events` + `session_meta` | 每轮 decide、tool、guard、phase、截图 ref、LLM 互链 |
| 批次快照 | `app_regression_runs` | 任务 `run_id`、用例列表、每条 case 的 `status` / `summary` / `report_run_id` |
| LLM 运维 | `dispatch_calls` | 设置页「调度」；通过 `llm/response.payload.dispatch_id` 与 session 互链 |
| 进程内缓存 | `agent_stream._RUNS` | Studio 旧路径；**优先读 session 投影**，内存有限、重启即丢 |

铁律（与 Harness 对齐）：**凡进入 `decide_next_action` / 改变 `StepCursor` 的事实，应能在 `session_events` 里找到对应 event。**

## 2. 数据目录与库文件

默认数据目录（macOS / Linux）：

```text
~/.mino-nexus/
  mino.db          # 主库（session、跑批、设置、目录等）
  nav/             # 导航采集证据（与 mino.db 同根）
```

覆盖目录：

```bash
export MINO_NEXUS_DATA_DIR=/path/to/data
```

库路径：`$MINO_NEXUS_DATA_DIR/mino.db`（未设置 env 时即 `~/.mino-nexus/mino.db`）。

> Console 文档里偶写 `~/Library/Application Support/MinoNexus`；**以 `mino_nexus/core/paths.py` 为准**（非 Windows 默认 `~/.mino-nexus`）。

## 3. 表结构（排查常用）

### 3.1 `session_meta`（一会话一行）

| 列 | 说明 |
|----|------|
| `session_id` | 主键，通常 `run_id::case_id`（见 [02-按ID查询日志.md](02-按ID查询日志.md)） |
| `run_id` | 批次任务 ID，如 `cr-9478d0b7d12d` |
| `case_id` | 如 `case-7bff9406` |
| `app_id` | 被测应用 |
| `status` | `running` / `pass` / `fail` / `blocked` / … |
| `summary` | 结束摘要（give_up、熔断、校验失败等） |
| `event_count` | 已写入 event 条数 |
| `started_at` / `finished_at` | ISO 时间 |

### 3.2 `session_events`（只追加）

| 列 | 说明 |
|----|------|
| `session_id` | 外键维度 |
| `seq` | 会话内单调递增，从 1 起 |
| `ts` | 时间戳 |
| `type` | 事件类型（见下表） |
| `turn` | 决策轮次 |
| `phase` | `prep` / `do` / `check` / … |
| `payload` | JSON |

### 3.3 Event 类型（优先会搜的）

| type | 何时看 |
|------|--------|
| `session/start` · `session/end` | 起止、最终 status / summary |
| `turn/start` · `turn/end` | 每轮决策边界；`decision_cap` / `decision_status` |
| `inspection/done` | 开跑 inspect：`session_block`、`required=logged_in` 等 |
| `context/slots` | 模型当轮可见槽（goal、checkpoints、session_block 摘要） |
| `llm/request` · `llm/response` | 与 dispatch 互链；看模型原始决策 |
| `tool/call` · `tool/result` | 能力执行；`capability_id`、`status`、`summary` |
| `guard/block` | 守卫拦截；`guard_id`、`reason`、`rewritten_cap` |
| `ops/guard_summary` | 会话结束 guard 统计 |
| `nav/attempt` | `fsm_navigate` 规划与降级 |
| `decision/give_up` · `decision/ask_human` | 模型主动放弃 / 问人 |
| `recovery/match` | 恢复规则命中 |
| `observe/screen` | 截图 thumb / hash（非全图归档） |

完整类型表：[9月8日_Session_Event_Log.md §4.2](../9月8日_Session_Event_Log.md)。

### 3.4 `app_regression_runs`

- `run_id`：任务 ID（`cr-` + 12 位 hex）。
- `payload`：JSON，含 `cases[]`（每条的 `case_id`、`status`、`summary`、`report_run_id`、`sn` 等）。

批次结论看这张表；**逐步骤细节以 `session_events` 为准**。

## 4. Studio / HTTP 怎么读 Log

需 Studio 登录态：`Authorization: Bearer <token>`（见 [HTTP.md](../HTTP.md)）。

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/case-runner/sessions?run_id=cr-…&case_id=…` | 列表 |
| GET | `/case-runner/sessions/{session_id}/events?from_seq=0&limit=500` | 原始 event（分页 `next_from_seq`） |
| GET | `/case-runner/sessions/{session_id}/turns` | 按 turn 聚合 |
| GET | `/case-runner/sessions/{session_id}/eval` | harness 指标投影 |
| GET | `/case-runner/agent/steps/{run_id_or_session_id}` | 轨迹 UI（内部 `resolve_session_id`） |
| GET | `/settings/dispatch/{call_id}` | 单条 LLM 调度详情 |

`session_id` 路径参数可带 `::`，FastAPI 路由已按 path 接收完整 ID。

## 5. 命令行访问数据库

### 5.1 sqlite3

```bash
DB="${MINO_NEXUS_DATA_DIR:-$HOME/.mino-nexus}/mino.db"
sqlite3 "$DB"
```

```sql
.headers on
.mode column
```

JSON 字段用 `json_extract(payload, '$.capability_id')`（SQLite 3.38+）。

### 5.2 Python（适合导出 JSON）

```bash
python3 << 'PY'
import json, os, sqlite3
db = os.path.expanduser("~/.mino-nexus/mino.db")
conn = sqlite3.connect(db)
sid = "cr-9478d0b7d12d::case-7bff9406"
rows = conn.execute(
    "SELECT seq, type, phase, payload FROM session_events WHERE session_id=? ORDER BY seq",
    (sid,),
).fetchall()
for seq, typ, phase, raw in rows:
    p = json.loads(raw) if isinstance(raw, str) else raw
    cap = p.get("capability_id") or ""
    print(f"{seq:4} {typ:20} {phase or '':5} {cap}")
PY
```

### 5.3 不要用生产库跑随意 pytest

个别历史测试曾向真实 `mino.db` 写入占位配置。排查库请只读查询，或复制一份：

```bash
cp "$DB" /tmp/mino-copy.db
```

## 6. Session 与代码入口

| 模块 | 作用 |
|------|------|
| `mino_nexus/loop/session_log.py` | `open_session` / `SessionWriter.append` |
| `mino_nexus/services/session_store.py` | ORM 落盘、`list_sessions`、`resolve_session_id` |
| `mino_nexus/loop/session_project.py` | `project_trajectory` / `project_turns` |
| `mino_nexus/loop/agent_loop.py` | 双写 stream + session |

写入发生在 `run_case` 全程；若 `session_meta` 无记录，常见原因是 **用例未真正开循环**（队列取消、设备忙、启动前失败），或 **看错 run_id / case_id**。
