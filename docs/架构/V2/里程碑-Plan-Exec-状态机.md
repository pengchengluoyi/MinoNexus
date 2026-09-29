# 里程碑 Plan / Exec 状态机

> **状态**：已落地（`mino_nexus/loop/milestone_orchestrator.py`）  
> **关联**：[看图规划-执行与步骤状态机方案.md](看图规划-执行与步骤状态机方案.md)、[用例执行-程序图专注与异常栈方案.md](用例执行-程序图专注与异常栈方案.md)、[开发手册.md](../../开发手册.md) §5

## 1. 目标

- **vision-plan** 只维护 `success_criteria.milestones`（追加 / 状态更新 / 本步是否已满足要求）。
- **vision-exec** 只在存在 **`in_progress`** 里程碑时执行，且工具 pass/fail **只写回该条**。
- **阶段收工** 见 [步骤准出与登录态方案.md](步骤准出与登录态方案.md)：prep/do 在最后一条终态之后才读 `exit_allowed`。未准出时再规划，不因列表变空就进入下一阶段。

## 2. 里程碑状态

| status | 含义 |
|--------|------|
| `pending` | 未开始 |
| `in_progress` | **当前执行焦点**（全表至多一条） |
| `pass` / `failed` / `skipped` | 终态 |

程序在 plan 写入或 append 之后调用 `ensure_single_in_progress`：若无 `in_progress`，则将**第一条** `pending` 标为 `in_progress`。

工具执行成功后：`complete_in_progress_milestone` → 将焦点标 `pass`（或 fail）→ `advance_in_progress_focus` 将下一条 `pending` 标为 `in_progress`。

## 3. 何时调用 plan / exec

### 3.1 禁止 exec（1.1.1）

当 **不存在** `pending` 且 **不存在** `in_progress` 时，**不得**调用 `agent-vision-exec`。

### 3.2 必须 plan（1.1.2）

满足以下任一条件时，本 turn **先**调用 `agent-vision-plan`（可阻塞 exec）：

1. `milestones` 为空（初次进入本 phase / 本步，**先行策略** 1.3.1）。
2. 开放条数 `open = 0`（全部终态）→ **不**调用 plan；由 `evaluate_milestones` + `apply_phase_transition` 收工（见 `milestone_orchestrator`）。
3. 开放条数 `open == 1`（需 plan 补 digest / 声明 `step_requirements_complete`）。

### 3.3 plan 之后是否 exec（1.3.3）

plan 返回 `step_requirements_complete`（bool）：

- **`false`** 且存在 `in_progress`：本 turn 可调用 exec 执行焦点事件。
- **`true`** 且已无 `pending`/`in_progress`：程序 **阶段流转**（1.3.4），本 turn 不 exec、不再 plan。
- 若 plan 做了 `milestones_append`：程序 `ensure_single_in_progress` 后，若存在 `in_progress` 且 `step_requirements_complete=false`，再 exec。

### 3.4 exec 注意力（1.2.4）

exec 的 system 约定：仅处理 `status=in_progress` 的那一条（`success_criteria` 中其余条仅作只读上下文）。  
`note_tool_pass_milestone` 只匹配 **当前 `in_progress` 的 id**（hook 步另按 `hook_cap` 匹配）。

## 4. vision-plan 输出契约（v6）

JSON 字段（在 v5 基础上）：

| 字段 | 说明 |
|------|------|
| `thought` | 必填。**追加** 时须含「虽然…，但是…」。 |
| `milestones_append` | 末尾追加，不重排 |
| `step_requirements_complete` | 本 phase/本步要求是否已由列表覆盖；`true` 且全部终态后程序流转 |

**禁止** plan 输出 `milestone_updates`；`pass`/`failed`/`skipped` 仅由程序在 tool 结果后写回。

## 5. 单 turn 时序（prep / do）

```text
种子/同步（claim、sync_prep_internal、app_launch_confirmed）
    → ensure_single_in_progress
    → 若需 plan：vision-plan → apply → ensure_single_in_progress
         → 若 step_requirements_complete 且无 open：phase transition，结束 turn
    → 若无 in_progress：结束 turn（仅规划）
    → vision-exec（仅 in_progress）
    → 派发 tool → pass/fail 写回 in_progress → advance_in_progress_focus
```

下一 turn 重复；当 `open<=1` 时再次 plan（**循环判定** 1.3.3）。

## 6. 与用例密钥的关系

- **prep**：仍仅 **程序** `seed_prep_milestones_from_claim` 注入首包；plan 负责验收类追加（如「确认未登录」），**不**在 prompt 注入 `task_context`。
- **do**：首包由程序 `seed_do_milestones_from_plan`（与 vision-plan 解耦）；有 `do_program_plan` 时 plan **不得** `milestones_append` 新业务边（`plan_append/rejected`）。

## 7. Session 可观测事件

| type | 含义 |
|------|------|
| `milestone/focus` | 当前 `in_progress` id |
| `orchestrator/plan_gate` | 因何条件触发 plan |
| `orchestrator/exec_skip` | 因无 in_progress 跳过 exec |
| `plan_append/rejected` | do 有 program_plan 时丢弃 plan 的 `milestones_append`；prep 非验收类 append 同理 |
| `orchestrator/plan_gate` `reason=stuck_replan` | 全终态未收工 → 窄 scope plan（`failure_verdict`） |
| `interrupt/push` / `interrupt/pop` | interrupt 栈 |
| `phase/transition` | `step_requirements_complete` 或 evaluate 收工 |
