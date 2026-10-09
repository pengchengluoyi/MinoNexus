# PROMPTS — prompt 归属与铁律

所有 LLM 调用都在本仓，且只在本仓（`../MinoScout` 有 `verify_no_llm.py` 守门）。

## 1. 真源分工

| 内容 | 真源 | 说明 |
|---|---|---|
| **prompt 正文、技能、角色、编排** | `mino_nexus/prompts/` | git 是唯一真源。`jobs/`、`skills/`、`roles/`、`stack/` |
| **加载** | `ai/prompt_repo.py` | 启动时校验；`MINO_PROMPTS_RELOAD=1` 时按文件 mtime 热加载 |
| **渲染** | `ai/prompt_render.py` | 块 → messages。形状与以前的 job 行一致 |
| **步骤指针措辞** | `loop/step_pointer.py` | 运行时注入槽，不写进 job 模板 |

```
mino_nexus/
├── ai/prompt_repo.py    读 prompts/、校验、指纹
├── ai/prompt_render.py  块 → messages
├── ai/job_slots.py      槽值生产者（Python）
└── prompts/             prompt 文件。不放 Python
```

## 2. 活 job 列表

| job id | 入口 | 引擎 |
|---|---|---|
| `agent-decide` | `planner.decide_next_action` | vision_decide |
| `assert-vision` | `planner.verify_step_expected` | vision_assert |
| `inspect-session` | `planner.inspect_session` | observe |
| `analyze_req` / `draft_mindmap` / `draft_cases` / `propose_atlas` | `services/qa_cover` | json_chat |
| `conductor` / `im-dialogue` / … | `roles_catalog.chat_with_role` | text_chat |

**定位走纯 VLM**（截图 + prompt → 坐标）。不要把 CLIP / OCR 引回 Nexus。

## 3. 改 prompt 的流程

1. 改 `mino_nexus/prompts/` 里的文件。改了正文就把该文件的 `version` 加 1。
2. `python scripts/verify_prompts.py`。内容变了但版本没加会失败；通过后更新 `prompts.lock.json`。
3. 提交。生产环境重启 Nexus 后生效。开发时可设 `MINO_PROMPTS_RELOAD=1`，改文件不用重启。
4. **禁止**在 `mino_nexus/**/*.py` 里写 system prompt 字面量（`scripts/verify_no_prompt_literals.py`，中英文都拦）。

`dispatch_log` 同时记下 `prompt_version` 和 `prompt_sha`。查问题时用 sha 对回当时的文件内容。

Workbench 的 Jobs / 技能 / 角色 / 编排页只读，展示版本、sha 和文件路径。

下面各节是历史版本当时改了什么。正文已经在 `prompts/jobs/` 里，启动时不再打补丁。

## 4. 铁律

| # | 规则 | 为什么 |
|---|---|---|
| 1 | 坐标一律 **0–1000 千分比** | Nexus 不知道分辨率 |
| 2 | prompt 里只出现菜单里**有**的能力 | 否则白跑 fallback |
| 3 | 观察阶段不准动手 | 校验阶段不能改界面 |
| 4 | 看图决策 ≠ 视觉校验 | 两个 job，两套 prompt |
| 5 | 结构化输出过 `schemas.py` | LLM 会乱填 |
| 6 | 配置 key 必须来自 `loop/registry.py` 闭集 | 禁止 JSON 表达式 |
| 7 | NavFSM 的 assist 正文由 `nav_compiler` 现编，**不进 prompt 模板** | 被测 App 的文案属于 `nav_fsm*` 表，硬编码进 job 就没法按 app 隔离 |

## 5. 成本与落痕

每次 LLM 调用经 `dispatch_log.record_llm` 落痕；`dispatch.bind(job=...)` 必须带 job id，**禁止**再从 system 文本反推 job。

本地校验（不进 CI）：

```bash
python scripts/verify_no_prompt_literals.py
```

## 6. `nav_assist` 槽（agent-decide v7）

NavFSM 每轮把「你在哪、下一步走哪条边、本步允许/禁止什么」编译成一段文本注入
`agent-decide`。正文由 `services/nav_compiler.py` 按 `nav_fsm*` 表现编，
**模板里只有一个空槽**，一个 App 字样都不该出现在 job 正文里。

| 项 | 值 |
|---|---|
| 槽名 | `nav_assist`（`kind: text`） |
| 生产者 | `loop/nav_runtime.NavRuntime.observe` → `ai/job_slots.assemble_agent_decide_slots` |
| 块配置 | `user_blocks` 末尾一块，`skip_if_empty: true`，`max_chars: 2400` |
| 空值行为 | **整块跳过** —— 没配导航图或开关关着时，prompt 与 v6 逐字相同 |
| 升级入口 | `ai/job_upgrades.upgrade_agent_decide_to_v7()`，启动时由 `core/bootstrap` 调用；幂等 |

v7 同时往 system 块插了一节「导航 assist」，讲**怎么读这段**（照建议边走、只从允许集里选、
被拦了换路径而不是重试）。规则是框架措辞，具体文案仍来自 DB。

升级会把 v6 正文存进 `overrides_json.revisions`，Console 可以 `{"activate_version": 6}` 回退。

## 7. `doc_context` 槽（agent-decide v9）

文档库每步 FTS 检索结果注入跑批 prompt。见 [DOC_LIBRARY.md](DOC_LIBRARY.md)。

| 项 | 值 |
|---|---|
| 槽名 | `doc_context` |
| 生产者 | `loop/inspections.match_step_docs` → `assemble_agent_decide_slots` |
| 块配置 | `skip_if_empty: true`，`max_chars: 1400` |
| 升级 | `upgrade_agent_decide_to_v9()`，启动时 bootstrap 幂等调用 |

## 8. 去掉每轮 `screen_layout` / `vlm_hierarchy`（v13 / inspect v3 / assert v3）

跑批主耗时是模型每轮生成这两段 JSON（10–18s），不是传图。Scout hierarchy 已经注入 prompt。

| job | 版本 | 改动 |
|-----|------|------|
| `agent-decide` | 13 | 删掉必填布局章节；tool schema 不再带这两字段；`max_tokens` 2048→768 |
| `inspect-session` | 3 | 同上；`max_tokens` 512→320 |
| `assert-vision` | 3 | 去掉 `screen_layout`；`max_tokens` 512→320 |

升级入口：`upgrade_agent_decide_to_v13` / `upgrade_inspect_session_to_v3` / `upgrade_assert_vision_to_v3`，启动时 bootstrap 幂等。旧模型若仍吐这两字段，解析侧忽略，不进 `action.params`。

## 9. 低置信导航不要冻住（v14）

`【导航】` 置信低时页名只是结构猜测。v14 改掉「先 recover / 问人，勿乱点」：以截图和用例步骤为准；达成信号已在屏上则 `signal_done`，禁止连续空等 `wait_ms`。`wait_ms` 同时计入 `ProgressGate` 里程碑（见 `loop/action_fuse.py`）。

升级入口：`upgrade_agent_decide_to_v14`，启动时 bootstrap 幂等。

## 10. 横滑须带起止点（v16）

底部风格条、横向列表等局部滚动：`swipe_direction` 应带 **`from_x/from_y/to_x/to_y`（0–1000 千分比）**，不要只靠 `direction` 在屏中心滑；可见目标名优先 `tap_element`。

升级入口：`upgrade_agent_decide_to_v16`；v15 全文存 `revisions`（见 [prompts/agent-decide-v15-snapshot.md](../prompts/agent-decide-v15-snapshot.md)）。启动 bootstrap 幂等。

## 11. 前置只做三件事（v18）

前置阶段不再 `check_run_env` / 切换测试环境。只保留：**筛选账号**、**筛选设备**、**环境清理**。设备登录态与账号登录态分写，避免「手机未登录」去卡号池 session。

升级入口：`upgrade_agent_decide_to_v18`，启动 bootstrap 幂等。

