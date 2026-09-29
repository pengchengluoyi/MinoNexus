# 执行改造方案 V2：步骤记忆、看图决策 Prompt、FSM 逻辑块

> **状态**：部分已落地（P0–P5 见同目录进度文档）；**收工与 Job 拆分**以 [看图规划-执行与步骤状态机方案.md](看图规划-执行与步骤状态机方案.md)（V2.1）为准。  
> **范围**：`agent_loop` Turn 循环、`StepCursor`、`planner` / `llm_jobs`、`nav_flow_block_catalog`、Console（扩展包 / Jobs / 新 FSM 逻辑块页）  
> **目标**：单步骤内**有序、可观测、可熔断**；LLM 注入**瘦身**；排查用**全量上下文**与 session 对齐；登录等**编排进 Nav FSM 逻辑块**，与 `email_login_auto` / `login_flow_macro` 等散落逻辑收敛。

---

## 0. 与现状的对照（避免重复造轮子）

| 现状 | 问题 | 本方案 |
|------|------|--------|
| `StepCursor.prompt_block()` 列出**已完成/未到**多步文案 | 注入过长、模型易跳步 | 仅当前 phase 的**一步**文案 + 子里程碑列表（§1） |
| `history_block` = 整案 `_history(history)` | 与当前步无关 | **当前用例步 + 当前 phase** 的操作子集（§1.3） |
| `success_criteria` = `decide_success()` 字符串 | 无结构化进度 | JSON **里程碑** `milestones[]`（§1.6） |
| `session_block` / `accounts_brief` 字符串 | 解析脆、guard 靠 regex | **JSON 槽**（§1.5） |
| `device_brief_json` 进 prompt | 对决策噪声大 | 仅 **Run 排查上下文**（§1.7） |
| `context/slots` 已写入 session | Console 未展示或字段不全 | **context/trace** 规范 + UI 恢复（§1.4、§2.8） |
| `fb.global.login` + `flow_block_runner` + `email_login_auto` 并行 | 与 Nav FSM 平级、难排查 | **逻辑块编排**统一入口（§2.3–2.6） |
| `step_effect` 仅 do、靠 expected 探针 | prep/check 无统一收工 | 统一 **里程碑评估器**（§2.2） |
| `launch_if_hierarchy_away` 等不区分渠道 | Web 误触发 App 逻辑 | **渠道门**（§2.1） |
| Registry 硬编码 guards | 与登录块重复 | 块内 **fuse / repeat** 声明（§2.6） |

代码锚点：

- Turn 主循环：`mino_nexus/loop/agent_loop.py` `for seq in range(1, max_steps + 1)`
- Prompt 槽：`mino_nexus/ai/job_slots.py` → `assemble_agent_decide_slots`
- 指针文案：`mino_nexus/loop/step_pointer.py` `prompt_block` / `decide_goal` / `decide_success`
- 逻辑块表：`nav_flow_block_catalog`（已有 `steps_json`，见 `catalog/flow_block_seed.py`）
- Session 排查：`session_events`（`context/slots`、`turn/start`、`guard/block`）

---

## 1. 历史记忆与看图决策 Prompt（1.1–1.9）

### 1.1 仅上传「当前一步」的 phase 文案

**规则**

- **prep**：只传 `precondition` + 前置进度（如「前置 1/3」若将来前置拆条）+ `goal`（与 `decide_goal()` 对齐，仅描述本条前置要达成的机态/资源态）。
- **do / check**：只传**当前用例步** `cur.n` 的 `instruction` 或 `expected`，**不再**在 `checkpoints_block` 里枚举 `[x] 步骤 k` / `[ ] 未来步骤`（见 `step_pointer.prompt_block` 今日行为）。
- `goal` 槽：与上同构，避免「整案 overview」重复。

**实现要点**

- 新增 `StepCursor.prompt_block_scoped()`（或重构 `prompt_block`）：`phase == prep` 时无 `nodes` 遍历；`do/check` 只渲染 `cur`。
- `decide_goal()` 同步收窄（今日 do 仍可能带多步语义）。

### 1.2 `checkpoints_block`：当前步「要做什么」+ 里程碑

- **不再**记录「已执行过的别步摘要」。
- 内容结构（注入 LLM 的文本可仍为多行，但语义固定）：

```text
【阶段】prep|do|check
【用例步】n / total（prep 时 n=0）
【本步 instruction|precondition|expected】…
【子里程碑】见 success_criteria.milestones 人类可读摘要
【纪律】原 prep_tail / do_tail / check_tail 的短版（可配置进 job 或 pointer 常量）
```

- **子里程碑**真源是 §1.6 的 JSON；`checkpoints_block` 只做**当前未完成项**的简短列表（pending / in_progress 各 1–3 条），避免全文复制 JSON。

### 1.3 `history_block`：当前步操作史

- 划分键：`(case_step_index, loop_phase)`，prep 用 `case_step=0`。
- 来源：在 `_record` / `_consume_login_chain` 等处写入时带 `extra.loop_phase` + `extra.case_step`（已有部分字段，需**过滤**后再拼 history）。
- 格式保持「`seq. cap → status: summary`」以利模型读；**上限**如 24 条/步，超出截断并只在 **trace** 留全量（§1.4）。
- **程序链**、**guard remediate** 同属本步 history。

### 1.4 排查上下文（不注入模型）

**问题**：曾有一版全量上下文，UI 不显示。

**方案**

| event type | 内容 | 消费者 |
|------------|------|--------|
| `context/slots` | **继续**写 LLM 实际槽（瘦身后） | Console Session 页「本 Turn 注入」 |
| `context/trace`（新） | 单步全量：`history_full[]`、`device_brief`、`menu_snapshot`、`milestones` 快照、`session_json`、`accounts_json` | Console「排查上下文」面板（默认折叠） |
| `turn/start` | 保留 `history_lines` 摘要 | 已有 |

- **铁律**（与排查手册一致）：凡改变 `StepCursor` / 进入 `decide_next_action` 的事实，须能在 `session_events` 找到对应 event；trace 是超集，不替代 `tool/call`、`guard/block`。
- Console：Session 详情增加 Tab「注入 vs 轨迹」，读取 `context/trace` + 按 turn 过滤。

### 1.5 `session_block` / `accounts_brief` 改为 JSON

**注入 LLM**（`job_slots`）：

```json
// session_json（示例）
{
  "session": "logged_in|guest|logged_out|unknown",
  "identity": "",
  "source": "inspect_session|hierarchy|lease_account",
  "dirty": false,
  "execution_line": "…"
}

// accounts_json（示例）
{
  "leased": true,
  "account_id": "…",
  "login_channel": "email|phone|…",
  "hints": { "email": "t***@…" },
  "otp": { "fixed_allowed": false }
}
```

**兼容**：`parse_session_value` / guards 读字符串处改为读 JSON；迁移期双写 `session_block` 字符串（deprecated）一个版本。

**落库**：`context/trace` 存完整 JSON；`effective_session_block` 产出对象而非拼接串。

### 1.6 `success_criteria` 结构化（里程碑 `milestones`）

为避免与「用例步骤 n」混淆，方案采用：

```json
{
  "schema": "mino.success_criteria.v1",
  "phase": "prep|do|check",
  "case_step": 2,
  "status": "pending|in_progress|pass|failed|skipped",
  "milestones": [
    {
      "id": "m1",
      "title": "点击登录入口",
      "kind": "visual_action",
      "status": "pass|pending|skipped|failed",
      "optional": false,
      "skip_reason": "",
      "evidence": ""
    },
    {
      "id": "m3",
      "title": "输入邮箱",
      "kind": "hook",
      "hook_cap": "lease_account",
      "status": "pending"
    }
  ],
  "block_ref": { "block_id": "fb.global.login.email", "from_milestone": "m2" }
}
```

**生成时机**

| 场景 | 谁生成 milestones |
|------|-------------------|
| 普通 do/check | **首次**本步本 phase 看图决策时，LLM 输出 `milestones` 字段（或独立 job `plan-milestones`，推荐 **合入 agent-decide 首轮** 强制 JSON 段） |
| 挂载逻辑块 | 块编排 **展开** 为 milestones（顺序固定），LLM 只填 `status` / 视觉坐标 / skip |
| check | 来自扩展包 **check 检查点** 编译为 `kind: checkpoint`（§2.9） |

**收工规则（替代/统一 `step_effect` + `require_do_work`）**

- `milestones` 全部 `pass` 或 `skipped(optional=true)` → 当前 phase 可 `signal_done` / 自动 `enter_check`（do）。
- 任一 `failed` → 允许 LLM 标 `give_up` / 程序熔断（§2.7）。
- **prep / do / check 共用**同一评估函数 `evaluate_milestones(cursor, ctx, shot, nav)`。

**命名**：对用户文档称 **「子里程碑」**；代码与 JSON 用 `milestones`；不用 `steps` 顶层字段以免与用例步骤冲突。若坚持 `success_criteria.steps`，可做为别名只读一个版本。

### 1.7 移除注入中的 `device_brief`

- `assemble_agent_decide_slots` 删除 `device_brief_json`。
- `RunContext.to_prompt_brief()` 仅写入 `context/trace`。
- Job 模板删除对应块；`prompt_version` 升至 **19**（见 §1.8）。

### 1.8 Prompt 清理（v19）

- 删除 HTML 注释型版本标记（`<!-- prompt_version >= 18 … -->` 等）；版本只留在 `llm_jobs.prompt_version` 与 `dispatch` 元数据。
- `job_upgrades.py` 增加 `upgrade_agent_decide_v19` 迁移脚本（DB 补丁，与现有 v5–v18 同模式）。

### 1.9 评估「工具 | 参数 |」表与其它槽

| 槽/块 | 建议 |
|--------|------|
| Markdown 工具表 | **删除**；能力以 **OpenAI function tools**（`menu` → `tool_schema`）为唯一真源，避免与 `menu_json` 重复矛盾 |
| `menu_json` | **移出 prompt 正文**；仅 trace 记录 cap 列表；模型只看 tools |
| `hierarchy_text` | 保留（非 Web 或降级时仍需要）；与 `vlm_hierarchy` 策略保持 PROMPTS.md |
| `nav_assist` | 保留，`skip_if_empty` |
| `knowledge_*` / `doc_context` | 保留，仍按步/遇阻策略 |
| `memory_block` | 若为空壳可删；长期记忆另立项 |
| `screen_size_block` | 保留 |

**agent-decide 输出扩展**（`schemas.AgentDecision`）：

- `milestones`（可选，首轮必填）
- `milestone_updates`: `[{ "id", "status", "evidence" }]`
- `step_outcome`: `pass|give_up|ask_human|skip` + `reason`（§2.7）

---

## 2. Turn 与 FSM 导航（2.1–2.9）

### 2.1 程序恢复：按渠道门控

**现状**：`launch_if_hierarchy_away`、`ensure_target_app_foreground`、`reset_native_app_before_case` 等对 Web/App 混用。

**改造**

```text
UiChannel.WEB  → 仅：开页/刷新、Playwright 前台、Web 专用 recovery（若有）
UiChannel.ANDROID / IOS → adb/wda 前台、launch、clear、hierarchy 远离检测
```

- 抽 `channel_program_recovery.py`：`maybe_recover_foreground(ctx, proxy, shot) -> ContinueTurn|None`
- Turn 内 **C 段**（原程序恢复）先 `if not channel_supports(cap): skip`。
- **指标**：Web 跑批 session 中 `launch_app` 误触发次数应降为 0。

### 2.2 探针与进 check：统一里程碑评估

- 删除「仅 do」的 `step_effect` 自动进 check 独立语义，改为：

```python
verdict = evaluate_milestones(...)  # 读 milestones + 屏/ hierarchy / checkpoint DSL
if verdict.phase_complete and cursor.phase == "do":
    cursor.enter_check()
```

- prep：`milestones` 可表示「已 lease」「已 clear」「session 符合 claim」。
- check：`milestones` 来自检查点 DSL（§2.9），**禁止**靠 mutate 凑 pass。

### 2.3 登录：纳入 FSM 逻辑块，不再与 FSM 平级

**目标结构**

```text
NavRuntime（页面 FSM）
  └── 触发条件：guest 弹窗 / 步进需要 logged_in / 逻辑块入口
        └── FlowBlockRunner（登录块实例）
              ├── 渠道 variant：email_web | phone_sms | …
              ├── milestones 同步到 StepCursor.success_criteria
              └── 步骤类型：
                    visual_tap / visual_input（LLM 坐标）
                    hook（lease_account | get_otp | accept_legal_consent）
                    optional（隐私勾选，可 skip）
```

**收敛代码**（分阶段删）：

- `email_login_auto.run_email_login_ui_chain`
- `login_flow_macro` / `try_run_login_flow_macro` 中与登录块重叠部分
- Turn 初 **长段** login 链（`agent_loop` 1710–1995 行量级）改为：`FlowBlockRunner.tick(block_id, ctx, decision)`

**邮箱 Web 七步示例**（编排数据，非硬编码）：

| ord | milestone | kind | 底层 |
|-----|-----------|------|------|
| 1 | 点登录 | visual_tap | tap_element |
| 2 | 点邮箱框 | visual_tap | tap_element |
| 3 | 输入邮箱 | hook+input | lease_account → input_text |
| 4 | 发送验证码 | visual_tap / cap | request_sms_code 或 tap Send |
| 5 | 输入验证码 | hook+input | get_otp → input_text |
| 6 | 隐私勾选 | visual_tap, optional | tap_element |
| 7 | 提交登录 | visual_tap | tap_element |

### 2.4 Console：FSM 逻辑块能力页

**位置**：能力目录下方新增 **「FSM 逻辑块」**（全局 `app_id=__global__`，应用可 override）。

**列表页**

- 列：block_id、名称、渠道、步骤数、版本、启用
- 操作：新建 / 复制 / 禁用

**编排页**（单块）

- 有序步骤列表：拖拽排序
- 每步：类型（visual / hook / internal cap）、目标文案、参数模板、**渠道覆盖**（web/android 不同 cap 或 skip）
- Hook 配置：`lease_account`、`get_otp` 参数映射
- **熔断规则**（§2.6）可视化：「同一 milestone 连续 N 次 tap → fuse」

**API**（草案）

- `GET/POST /nav/flow-blocks`（global + `?app_id=`）
- `GET/PUT /nav/flow-blocks/{block_id}`
- 数据：`nav_flow_block_catalog.steps_json` 升级为 **schema v2**（见 §2.6）

**代码路径**：执行只认 catalog + runner；删除 runner 内写死的 `fb.global.login` 三步。

### 2.5 可选跳过（LLM + 编排）

- 步骤字段 `optional: true` + `skip_policy: llm|never|if_not_visible`
- LLM 在 `milestone_updates` 标 `skipped` + `evidence`（如「无隐私勾选控件」）
- 程序校验：optional 且 `skip_policy=llm` 时必须带 evidence 才接受 skip

### 2.6 熔断与 repeat block 下沉到编排

**steps_json v2 片段**

```json
{
  "id": "email_tab",
  "kind": "visual_tap",
  "fuse": { "same_target_repeat": 2, "action": "fuse_block" },
  "guards": [{ "type": "block_repeat", "key": "email_tab", "remediate": "hook_chain" }]
}
```

- Runner 执行前查块内 guards；命中则写 `guard/block`（`dispatch_gate_code` 带来源 `flow_block:…`）。
- 全局 registry 中登录相关 guard **逐步下线**，保留通用 `deny_mutate`、`action_fuse` 作兜底。

### 2.7 步骤级 outcome 四态

- 字段：`step_outcome`: `pass | give_up | ask_human | skip`
- 绑定到 **当前用例步 + phase**；写入 `decision/step_outcome` event。
- `give_up` 必须 `reason`；与现 `decision/give_up` 合并语义。
- Runner / 评估器根据 outcome 决定 `_leave` 或 continue。

### 2.8 扩展包页恢复 + 菜单与逻辑块互斥

- **恢复** Console「扩展包」(`/packs`) 展示（HTTP.md 已写只读曾可用；查前端路由是否隐藏）。
- **互斥规则**：某 cap 若仅由逻辑块 hook 触发（如块内独占 `get_otp` 序列），则从 **function tools 菜单剔除**；通用 `tap_element` / `input_text` 保留。
- 文档更新：`CAPABILITY_CATALOG.md` 增加「逻辑块独占能力」表。

### 2.9 check：扩展包「预期 / 检查点」

**扩展包新增 kind**：`check_checkpoint`（或挂在现有 expected 编译器）

**DSL 示例**（存扩展包 / 用例 expected 编译）

```json
{
  "schema": "mino.checkpoints.v1",
  "checkpoints": [
    { "id": "c1", "type": "element_exists", "selector": "…", "status": "pending" },
    { "id": "c2", "type": "counter_delta", "selector": "…", "delta": 1, "status": "pending" },
    { "id": "c3", "type": "selected_or_lit", "selector": "…", "status": "pending" }
  ],
  "status": "pending"
}
```

- 编译进 `success_criteria.milestones`（`kind: checkpoint`）。
- 程序侧 `run_programmatic_checks` 升级为读该结构，输出 `status` + `reason`；失败写入 trace，**不**注入长文本。
- LLM check 阶段：仅处理 **无法程序化** 的检查点 + `assert_visual` 兜底。

---

## 3. Turn 1..N 在新架构下的单轮流程（替换旧叙述）

```text
for seq in 1..N:
  turn/start + trace 快照
  observe → nav → session_json 刷新
  [渠道] program_recovery? → continue?
  evaluate_milestones? → enter_check / phase_complete?
  FlowBlockRunner.tick? → continue?
  build menu（剔除逻辑块独占 cap）
  context_pack（瘦身）
  decide（tools only；首轮可写 milestones）
  parse milestone_updates + step_outcome
  nav.guard → registry.guard（兜底）→ block guards
  dispatch / remediate
  更新 milestones + history_step + tool/result
  trace 全量写入
  if phase_complete → signal_done 门闩（prep 资源门 / do→check / check advance）
  turn/end
```

**与今日差异**：中间不再有「平行」的 email 链与 FSM macro 两套；**里程碑**为单一进度真源。

---

## 4. 数据与迁移

| 项 | 动作 |
|----|------|
| `llm_jobs` agent-decide | v19 升级；删 device_brief、工具表 |
| `nav_flow_block_catalog.steps_json` | v1→v2 迁移；seed 增加 `fb.global.login.email_web` |
| `session_events` | 新增 `context/trace`、`decision/step_outcome` |
| `StepCursor` | 字段 `success_criteria: dict`；序列化进 fork_state |
| Console DB | 无新表；逻辑块用现有 catalog 表 |
| 协议 | **不**改 Scout EXECUTE（除非 hook 参数标准化）；Nexus 内聚 |

**兼容**：旧用例无 milestones 时，fallback `legacy_step_effect` 一个版本（feature flag `MINO_MILESTONE_V1=0`）。

---

## 5. 实施分期（建议）

| 期 | 内容 | 验收 |
|----|------|------|
| **P0** | §1.1–1.5、1.7–1.8；`context/trace` + Console 排查 Tab | 同用例 session 可见全量上下文；prompt token 降 ≥30% |
| **P1** | §1.6、§2.2 评估器；agent-decide 输出 milestones | Hi3D 邮箱登录步不再死循环 Continue |
| **P2** | §2.1 渠道门；§2.8 扩展包页 | Web 无 launch_app 误触发 |
| **P3** | §2.4 Console 编排 + runner v2；§2.3 登录块 | 块可配七步邮箱流 |
| **P4** | §2.5–2.7、§2.6 fuse 编排；删旧 login 链 | registry 登录 guard 减少 |
| **P5** | §2.9 check DSL + 扩展包 | check 步程序化占比可度量 |

---

## 6. 风险与原则

- **模型自主性下降**：用里程碑 + 工具菜单收敛换精准度；optional skip 保留必要弹性。
- **首轮生成 milestones 质量**：可增加「仅 Turn1 本步」小模型或规则模板（登录块展开则不依赖 LLM 排序）。
- **不要**在 Nexus 硬编码 Hi3D 文案（遵守 no-app-keywords）；块编排进 DB。
- **测试**：按仓库铁律不以 pytest 验收；以 `session_events` + 本方案 trace 为准。

---

## 7. 已确认（2026-03）

1. 对外 JSON 用 **`milestones`**（置于 `success_criteria` 文档内）。
2. **首次**单步看图由 LLM 生成里程碑列表；检测到需登录时，将**登录逻辑块**拆入当前步 milestones **顶部**优先执行，再由 LLM 更新状态。
3. check **仅维护 success_criteria**，移除 `assert_visual` 分叉（实施见 P1+）。
4. Console：**FSM 逻辑块** = 程序流转；**导航图** = 页面 UI 跳转（两件事）。

---

## 8. 文档与图更新

- 更新：`docs/架构/V1/单用例执行全景.md` Turn 段、Canvas `run_case` 视图（里程碑 + 逻辑块 runner）。
- 新增：`docs/基础框架/FLOW_BLOCKS.md`（编排 schema、渠道、hook）。
- 更新：`docs/基础框架/PROMPTS.md` v19 槽位表。

**下一步**：确认 §7 四项后，从 **P0** 开工（`step_pointer` + `job_slots` + `context/trace` + Console）。
