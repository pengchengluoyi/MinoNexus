# Mino Bots 方案（V7）：测试工作区

修订：2026-10-19（第五版，收敛重写）。状态：**方案，未实施；本版只改文档，不改代码**。受 [V7 总清单](README.md)、[取消与不恢复事项](取消与不恢复事项.md) 和仓库 `CLAUDE.md` 约束。取代第四版及此前所有 MinoBots-* 文档。

---

## 0. 这一版为什么重写

第四版为了「像 Dots、像 Bot」，把一个聊天入口设计成了一整套并行的执行系统：自己的任务表、自己的派发与预检、自己的线程表、七种事件触发。落地后出现两个问题：

1. **像 IM**：对话没接到真正干活的代码上，用户说话得不到有用的回应。
2. **挡住原功能**：为了「同一用例只有一个 run」，在 `run_store` 里加了全项目闸门，手动在任务页触发也会被拦。

两个问题同一个根源：**把「工作区」当成了执行系统，而不是一个入口。**

### 0.1 过度设计清点（源码核实）

与协作相关的文件合计约 7000 行（含此前已有的议题与流水线部分）。第四版新增的部分逐项评估：

| 第四版的设计 | 代码位置 | 它在做什么 | 为什么不必要 | 处理 |
| --- | --- | --- | --- | --- |
| 全项目「同用例只许一个 run」闸门 | `run_store.py:194-206` | 任何新 run 与进行中 run 有同一用例即抛 `workspace_case_busy` | **对所有来源生效，挡住手动执行**；设备与账号占用已有现成准入 | **删除** |
| 工作区自己的任务表 `collab_tasks` | `models/collab_task.py` | 复制 run 的状态、计数、结果 | run 的唯一真源本来就是 `run_store` | 删除 |
| 工作区自己的预检、派发、结算、诊断、重试 | `collab_batches.py`（600 行） | 重新实现 `case_runner` 已有的 preflight → 派发 → 盯进度 → 失败归类 | 与现有执行链并行两套，两套都得维护，行为必然分叉 | 删除；工作区直接调现有 `case.run` |
| 线程表 `collab_agent_runs` + `input_version` / `applied_version` / 心跳 / 认领 | `models/collab_agent_run.py`、`collab_roles.py`（377 行） | 给每个角色步骤一条持久记录，支持对线程追加要求 | 要的只是「谁在按什么顺序做什么」，一个计划卡加一个后台任务就够 | 删除 |
| 七种事件触发 + 六条闸门 | `collab_workspace_events.py` 等 | 范围变化、编译完成、资源恢复、新 build、定时…自动建任务 | 没人要求；且每种都会重复写卡片 | 只留一种：run 结束时播报 |
| `scope_revision` 与「结果过期」 | 工作区字段 | 范围变了就把旧结论标过期 | 无人读取，徒增状态 | 删除 |
| `collab_decisions`、策略开关、通知渠道、定时回归 | 多处 | 人写结论、自动重试、飞书出站… | 先让对话能干活，这些是后置需求 | 后置到 §9 |
| 自造四个「角色」 | 第四版 §2A.3 | — | 后台已有 18 个角色，不另造 | 复用后台角色 |
| 角色白名单字段 `workspace_duty` / `workspace_tools` | 第四版 §2A.3 | 限制每个角色能调哪些工具 | 写类工具本来就有 `risk: write` 的确认规则，沿用即可 | 删除 |

### 0.2 第四版里一个应当撤回的判断

第四版 §2A.8 说「在工作区里让助手直接调 `case.run` 会导致 run 不挂在工作区下，所以必须用工作区自己的写工具」。**这个判断是错的**，它正是造出上面那套并行执行系统的理由。

正确做法：工作区调用的就是 `case.run`，与任务页手动触发是同一条路径。「在工作区里能看到这个 run」靠一条带 `run_id` 的消息卡片解决（§3.4），不需要另建任务表，也不需要任何前置绑定。

---

## 1. 唯一原则

> **工作区是入口，不是门。** 执行只有一条路径（`case.run` → `case_runner` → `run_store`）。工作区、任务页、助手 IM 都只是触发它的方式。工作区不能是执行的前提，也不改变执行的任何规则。

推论：

- 在任务页手动跑，不需要任何工作区，行为与现在完全一致。
- 工作区里跑的 run，就是一个普通 run，`run_store` 里没有任何工作区专有字段或规则。
- 工作区只负责三件事：**记住范围**、**对话**、**把相关 run 展示出来**。

---

## 2. 概念：只有三个

| 名词 | 定义 | 存哪 |
| --- | --- | --- |
| **工作区** | 一个聊天，**可以**绑定一个需求或一组模块。绑定后同项目同范围只有一个 | `collab_topics`（沿用，已有 `requirement_id` / `modules` / `scope_case_ids`） |
| **范围** | 需求 ∪ 模块 ∪ 钉住的用例。创建时可以为空，后在对话里补 | 工作区字段 |
| **执行** | 就是一个 run，不另立「任务」 | `run_store`（不动） |

创建与范围规则：

- **一键新建对话**，不要求先选需求、模块、应用或环境；缺什么在对话里问、补。
- **空范围不等于全部用例**：范围为空时不能执行，管家要先问清楚跑哪部分，**不得静默扩大到整个项目**。
- 对话里提到已有模块或需求后，管家把它写进范围；也可以由用户在页面上直接绑定。
- 明确绑定了同一范围，则复用已有工作区；未绑定范围的新对话互不去重。
- 用例页「加到工作区」只把用例钉进已有工作区，不新建单用例工作区。
- 执行时才需要确认范围、应用、环境，缺哪项就在聊天里问哪项，已经说过的信息不丢。

---

## 3. 对话怎么承接

### 3.1 一个入口：工作区管家

用户在工作区里说话，进入一个后台处理（沿用已有的持久消息队列 `collab_message_jobs`，聊天立即返回，重启后未完成的标记为中断，**已发起的动作不重放**）。

处理方式是**一个工具循环**，新模块 `workspace_agent`：

- 提示词视角：后台角色 `conductor`（分析师），输入是工作区事实包（§3.2）。
- 手：`prompts/assistant/tools/*.yaml` 里已有的工具（见 §3.3），不新增权限层。
- 循环骨架复用 `assistant_agent` 的底层件（`render`、`_call_model`、`run_tool`、墙钟预算、无进展判断）。**不改 `handle_utterance`**，因为它绑定 IM 的 channel / Pending，回归面是全部 IM 入口。

不用正则分流，不用固定话术兜底。听不懂时基于事实反问并给可选项。

### 3.2 事实包（模型看到的东西）

代码取事实，模型只负责理解和措辞：范围与用例数、可执行/阻塞分布（现有 preflight 结果）、最近引用的 run 及其状态（读 `run_store`）、资源概况。数字只来自代码，不让模型回填。体积有上限，超出按字段裁剪，不截断 JSON。

### 3.3 工具：全部复用

| 用途 | 工具 | 状态 |
| --- | --- | --- |
| 看用例、设备 | `case.list`、`device.list`、`app.explore` | 已有 |
| 跑、重跑失败 | `case.run`、`case.retry_failed` | 已有，**与任务页同一路径** |
| 看进度、结果、取消 | `task.get`、`task.list`、`task.cancel` | 已有 |
| 看证据 | `session.list`、`session.events`、`session.screenshot`、`llm.get` | 已有 |
| 看版本/编译 | `case.version_chain`、`case.compile_job`、`case.recompile` | 已有 |
| 看工作区范围、钉用例 | `collab.scope_get`、`collab.pin_cases` | **尚未实现**。范围已在事实包里；钉用例走页面「加到工作区」，需要时再补，各一份 yaml |

写类工具（`case.run`、`case.recompile` 等）沿用现有 `risk: write` 规则，不另建权限层。

### 3.4 把 run 展示在工作区里

`case.run` 返回 `task_id`（即 run）后，管家往工作区写一条**run 卡**（`card_kind=run`，`ref=run_id`）。卡片内容渲染时读 `run_store`，不冻结数字。

反向关联：`run_store.finish` 已有一个钩子（`collab_events.on_run_finished`）。run 结束时，若有消息卡片引用了该 `run_id`，就在对应工作区播报终态。没有引用就什么都不做——**手动执行的 run 完全不受影响**。

这是工作区与执行之间唯一的连接，单向、只读、可缺失。

### 3.5 角色：复用后台，只用一个

- 管家用后台已有角色 `conductor`（分析师）的视角，提示词在 `prompts/assistant/jobs/workspace-route`。
- 不新增角色，不改角色目录，不给角色加字段。
- `req-qa-bm`、`version-qa-bm`、`report-writer`、`product-expert` 先不接入。需要时（例如「这条需求覆盖了吗」）作为后续独立需求，用它们的现有规则函数 `map_cases` / `classify_fail` / `pick_regression`，不新建机制。`report-writer` 当前 `called: sandbox`，补完业务输入合同前不接入。

**现状事实**（决定为什么只能这样）：运行时一个角色 = 取一个 job 的 system 提示词 + 聊天历史，调一次模型（`ai/roles_catalog.py:247 chat_with_role`）；YAML 里的 `jobs:` 列表只用于展示，没有任何地方据此限制调用。角色本身不会调工具，**能力在工具里，角色只提供视角**。

### 3.6 多步怎么做：就是工具循环本身

不做「计划卡 + 多角色按序交接」。用户说一个目标（「把登录模块回归一下」），管家在同一个工具循环里自己多步完成：查事实 → 确认范围、应用、环境 → 调 `case.run` → 回复。

- 一次循环有步数和墙钟上限；相同查询无新结果即停止，不无限查。
- **一条消息最多提交一次写操作**；已提交后再要求写，拒绝并让模型把结果告诉用户。
- 写类工具沿用现有幂等收据（`assistant_receipts`），重复请求返回原 run，不重复创建。
- 信息不足、工具返回 ask 或失败时停止，把原因和可选项告诉用户，不把失败说成通过。
- 中途重启：持久队列把运行中的消息标为中断，**不重放已发起的写操作**；用户再说一遍即可。
- 用户插话：发新消息即可，管家按最新话重新处理。没有线程，没有版本化追加。
- 上下文：只给工作区事实包 + 最近 6 条消息，不拼整段历史。

---

## 4. 触发：两种

| 触发 | 做法 |
| --- | --- |
| 用户说话 | 进入 §3.1 |
| run 结束 | 钩子播报终态（仅限被工作区引用的 run） |

其余（范围变化、编译完成、资源恢复、新 build、定时回归、手动 run 失败纳入）**全部后置**，见 §9。

---

## 5. 数据：几乎不新增

| 表 | 变更 |
| --- | --- |
| `collab_topics` | 沿用；`anchor` 废弃，不再按用例建工作区 |
| `collab_messages` | 沿用；用到 `message_kind` / `card_kind` / `ref_id` / `role`。同类卡片（如资源卡）按 `topic_id + card_kind` **原位更新**，不追加 |
| `collab_message_jobs` | 沿用（持久队列） |
| 其余全部 | **模型与服务已删除，不再写**：`collab_tasks`、`collab_agent_runs`、`collab_decisions`。**库里的旧表和旧数据原样保留，不做迁移、不做删除**。`collab_policies`、`collab_approvals`、`collab_work_items`、`project_bots` 工作区不再使用 |

`run_store` 与执行链：**不新增任何工作区字段或规则**。

---

## 6. UI

- 左侧「测试工作区」：「新建对话」一键创建、自动命名；每行标题、一句进度、未读。
- 工作区页：默认就是聊天（文字、run 卡、资源卡），底部输入框，加号菜单放导入用例文件/飞书表格与截图。
- 范围、用例、资源等详情放右侧抽屉按需展开；管理操作（暂停、归档、删除）放省略菜单，页面顶部不放任务控制按钮。
- 去掉：四格统计、进度带、任务/问题/决策/备注/活动页签、子任务线程抽屉、角色头像与「圈定员」等署名、计划卡、定时与通知配置。
- 用例页入口：「加到工作区」。
- **前端必须随后端收敛同步调整**：前端现在仍调用已删除的接口（见 §7.2），否则页面会报错。

---

## 7. 收敛状态（2026-10-19，后端已执行，前端未跟上）

### 7.1 后端已删除或改动

| 处理 | 对象 |
| --- | --- |
| **已删除，原功能恢复** | `run_store.py` 的 `workspace_case_busy` 全项目闸门（手动执行不再被拦） |
| 已删除 | `collab_batches.py`、`collab_roles.py`、`collab_workspace_events.py`、`collab_reports.py`、`collab_bot_orchestrate.py`；模型 `collab_task`、`collab_decision`、`collab_agent_run`；`core/collab_plan_schemas.py`；job `collab_intent` 及 `qa_cover` 中对应分支 |
| 已改造 | `collab_conductor.py`（只入队，处理交给 `workspace_agent`）；`collab_workspace.py`（去掉任务、决策、策略；`detail` 只返回消息、范围快照和 run 实时数据）；`rCollabWorkspace.py`（去掉任务/重跑/取消/决策/通知/子任务接口）；`collab_events.py`（run 结束只通知被 run 卡引用的工作区） |
| 已新增 | `services/workspace_agent.py`（工具循环）；`prompts/assistant/jobs/workspace-route/`；`models/collab_read.py`（已读标记，原定义随 `collab_task` 删除后单独保留） |
| 保留未动，不进主线 | `collab_steward.py`、`mino_bots.py` 与 `rMinoBots.py` 中旧议题接口、`collab_langgraph.py`、`collab_agentscope*.py`、`collab_openhands.py`、`collab_publish.py`、`collab_extended.py`、`collab_apply.py`、`collab_digest.py`、Bot 市场 |

备份：删除前的未提交 collab 文件已打包到仓库外 `MinoNexus-backup-collab-20261019`。

### 7.2 前端尚未调整（会报错）

`MinoWorkbench/src/domains/testing/collab/CollabPanel.tsx` 仍引用已删除的接口：`/tasks`（创建、取消、重跑）、`/decisions`、`/notifications`、`/agents/*`，以及 `detail` 里已不存在的 `tasks`、`issues`、`agent_runs`、`decisions`、`activities` 字段。需要按 §6 重写该组件，只保留：列表、新建、消息收发、run 卡、范围/用例抽屉、附件、归档与删除。

### 7.3 还没做的收尾

- 旧数据里已有的 `collab_topics.workspace_kind` / `scope_key` 等列保留；迁移脚本里对它们的处理不变。
- `collab_worker.tick` 现在只剩旧议题的 work item 处理，后续整理旧议题时一并删除。
- 后端只验证了应用能导入、8 条工作区路由挂载、新提示词与工具能加载；**尚未做任何真实对话和真实跑批验证**。

---

## 8. 分期与验收（以 `run_store`、`dispatch_log`、`collab_messages` 为证，不写 pytest）

### P0：先恢复原功能

1. 闸门已删除。在任务页手动触发同一条用例两次（第一次未结束时），行为与改动前一致，不出现 `workspace_case_busy`。
2. 没有任何工作区的项目，手动执行、助手 IM 执行均不受影响。

### P1：工作区能干活

1. 一键新建对话成功，无需选范围；对话里说出已有模块名后，范围被写入；明确绑定同范围时复用原工作区。范围为空时说「跑一下」，管家反问跑哪部分，不执行、不默认全部。
2. 在工作区里问「当前的任务都有什么」，回答列出真实的 run（读 `run_store`），不再回「你想测试什么功能」。
3. 说「把登录模块回归一下」，管家补齐应用与环境后调用 `case.run`，产生一个普通 run，`run_store` 里它与手动 run 没有任何区别；聊天里有 run 卡，数字与 `run_store` 一致；同一条消息不会提交两次。
4. 该 run 结束后，工作区里有终态播报；同一时刻在任务页手动跑的另一个 run，不会在工作区里出现，也不受影响。
5. `dispatch_log` 里这次对话的模型调用，`job` 是 `workspace-route`，`role` 是 `conductor`。
6. 无法理解的话，回复基于事实给出具体反问，不是固定话术。
7. 前端按 §6 调整后，工作区页面不再请求已删除的接口（浏览器网络面板无 404）。

### P2：按需再加

问「这条需求覆盖了吗」走 `req-qa-bm` 与规则函数；`report-writer` 补合同后接入；用例页「加到工作区」。

---

## 9. 后置（需要时单独评估，不在本方案内）

范围变化/编译完成/资源恢复/新 build 的自动触发、定时回归、失败自动重跑、人写决策、飞书出站通知、问题聚合页、范围变化后的「结果过期」。

## 10. 不做的事

- 不改执行链：`case.run` → `case_runner` → `run_store` 的任何规则，工作区都不增加。
- 不另建任务表、派发器、预检、诊断；不做线程表、计划卡、多角色交接。
- 不生成需求、用例、脑图；不做 Bot 市场、自定义角色。
- 不用正则 + 固定话术充当对话。
- 完成只认 `run_store` 与 session 证据；「已提交」「Bot 已回复」不算完成。

## 11. 待定

1. 旧议题接口（`rMinoBots.py` 的 topics/steward 等）与 `collab_steward.py`、`collab_worker.py`：何时整体删除。假设：前端调整完、确认没有入口再调用后一起删。
2. 是否需要「只读一眼看到当前所有 run」的页签。假设：先不做，聊天里问「当前的任务都有什么」即可。
3. 旧数据：按用例建出来的议题保持原样，不迁移。
