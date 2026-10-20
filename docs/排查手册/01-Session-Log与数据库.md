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

Agent / 自动化排查：**禁止**为定位问题新增 `tests/test_*.py` 或跑 pytest；以本节 SQL / Console 会话日志为准。若缺关键字段（如 `tool/call` 完整 `params`、`guard/block` 原因），改 `session_log` / `agent_loop` 写入，而不是写测试复现。

## 2. 数据目录与库文件

开发源码运行默认使用 `~/.mino-dev/nexus/mino.db`，覆盖变量为 `MINO_NEXUS_DEV_DATA_DIR`。
正式运行使用 `MINO_NEXUS_DATA_DIR`；未指定时 macOS 为 `~/Library/Application Support/Mino/nexus/data/mino.db`，Windows 为 `%APPDATA%/MinoNexus/mino.db`，Linux 为 `~/.mino-nexus/mino.db`。
以当前进程模式和显式配置为准，不能仅因旧目录里存在库文件就去查旧库。开发与正式路径不得共用。

V7 不再自动迁移旧 schema 或环境结构；发现不支持结构时应保留原库、报告问题，不得在排查中直接改库或清空数据。

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
| `context/slots` | 模型当轮可见槽（goal、checkpoints、session_json、history 本步等） |
| `context/trace` | 排查用全量（history_full、device_brief、prompt_block_full、menu 快照）；**不注入 LLM** |
| `llm/request` · `llm/response` | 与 dispatch 互链；看模型原始决策 |
| `tool/call` · `tool/result` | 能力执行；`capability_id`、`status`、`summary` |
| `guard/block` | 守卫拦截；`guard_id`、`reason`、`rewritten_cap` |
| `milestone/exit_eval` | 逻辑块步级准出；`exit_status`、`would_block`、`reason` |
| `block/exit_eval` | 逻辑块块级准出影子（如 `do_to_check` 前）；`would_block`、`issues[]` |
| `ops/guard_summary` | 会话结束 guard 统计 |
| `nav/attempt` | `fsm_navigate` 规划与降级 |
| `decision/give_up` · `decision/ask_human` | 模型主动放弃 / 问人 |
| `recovery/match` | 恢复规则命中 |
| `observe/screen` | 截图 thumb / hash（非全图归档） |

完整类型表：[9月8日_Session_Event_Log.md §4.2](../9月8日_Session_Event_Log.md)。

### 3.3.1 Scout 执行信息（2026-10-12）

Scout 经 `RESULT` 返回的执行状态、摘要、错误和耗时已写入 `tool/result`，执行参数在配对的 `tool/call`。这不是 Scout stdout/stderr 的全文镜像。当前历史记录只有基本字段；新增字段只对更新 Nexus 后的新任务生效，不回填旧库。

新增 `tool/result` 字段：`scout` 包含服务端确认的 node_id、设备 sn、platform/os、scout_version、run_id 和 step_idx；`attempts` 包含执行器尝试、状态、耗时和精简错误；`desktop` 包含 capture_id、session_generation 和动作 effect/route/delivery/error.code/escalation。不把图片 base64、输入正文、token 或 SDK 原始 JSON 复制进诊断字段；截图仍由 `observe/screen` 的缩略图与 hash 查看。

Session Log 的事件接口与 turns 投影保留上述字段；Workbench 会话详情的原始事件可展开 payload 查看。Scout 本地 `RESULT` 日志使用相同 run_id/step_idx/sn 关联。进程启动、注册拒绝、断线重连等没有任务上下文的日志仍看节点日志；它们不会凭空成为某个会话事件。

例如按已有 session_id 查询 `tool/result`，检查 `json_extract(payload, '$.scout.node_id')`、`json_extract(payload, '$.attempts')` 与 `json_extract(payload, '$.desktop.action.effect')`。旧记录返回 NULL 不代表 Scout 没执行，而是旧版未持久化这些诊断字段。读取仍受 session 的 owner_user_id 授权约束。

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

外部 Agent 优先使用只读 MCP 诊断工具查询会话及 LLM 正文，见 [MCP 与 LLM 响应查询](06-MCP与LLM响应查询.md)。无需直接读取数据库或索取 token。

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
