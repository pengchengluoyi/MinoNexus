# prompt 改造方案

> **状态**：已按本方案落地（导出、核对、改读取、删补丁链）。社交助手目录只留了空位，不在这次实现。§9 三件事仍待确认。
> **要回答的事**：prompt 从数据库（`llm_jobs` / `skills` / `settings`）搬到 Nexus 仓库，用 git 管版本。具体包括：目录怎么分；文件里怎么写版本号；怎样先把库里的内容完整取出、核对无误后再删 ORM；哪些旧 job 可以趁机删掉；对各模块的影响。
> **关联**：[PROMPTS](../../基础框架/PROMPTS.md)（本方案落地后需要重写）、[mino 接入社交软件实现 24 小时在线](mino接入社交软件实现24小时在线.md)（assistant 的 prompt、工具表、卡片模板都按本方案放文件）。
> **前提**：Nexus、Scout、Workbench 三个仓库都由同一个人维护，不需要考虑「线上有人改了库、发版会不会覆盖」这类冲突。**git 就是唯一真源。**

---

## 0. 拍板

| 题 | 决定 |
|---|---|
| 真源 | **`mino_nexus/prompts/` 下的文件 + git 历史。** 数据库里不再存任何 prompt 正文、技能程序、角色和编排 |
| 目录放哪 | 放在 `mino_nexus/` 下，但**只新增一个 `prompts/` 目录**，五个分类作为它的子目录，不在包根上平铺五个目录（§2.1） |
| 分类 | `assistant/`（独立）、`jobs/`（对应 Workbench `/jobs` 页）、`skills/`、`roles/`、`stack/`（编排） |
| 版本号 | 每个文件头写 `version: <整数>`，改了内容必须加 1。`dispatch_log` 同时记下内容哈希（运行时现算），用来精确对应到文件内容（§4） |
| 怎么改 prompt | 改文件 → 跑校验脚本 → commit → 重启 Nexus。开发时可以开热加载（§5.3） |
| Workbench 四个页面 | 改为**只读**：展示版本号、文件路径、git 提交、渲染预览。不再提供保存 |
| 库里的数据 | **先导出、再核对、再切读取路径、最后删 ORM**，四步分开提交（§6）。旧表不在代码里 DROP，备份后由人工清理 |
| `job_upgrades.py`（3452 行） | 整个删除。它是「在库里给 prompt 打字符串补丁」，换成 git 之后没有存在理由 |
| `im-dialogue` / `im-defect` / `test-engineer-chat` | **可以删**，但要等 assistant 接管微信回复之后再删（§7） |
| 散落在 `.py` 里的 prompt | 一并搬进 `prompts/`（§3.5），以后不允许任何 LLM 调用绕过 `prompts/` |

---

## 1. 为什么要改

现状有四个问题，都直接来自「prompt 存在库里」：

1. **改 prompt 要写迁移代码。** `ai/job_upgrades.py` 已经 3452 行、45 个 `upgrade_*` 函数。`agent-decide` 从 V5 打补丁打到 V22，每一版都是「读库里的正文 → 字符串替换 → 写回」，`bootstrap.py` 启动时逐个执行。字符串一旦对不上，补丁就静默跳过。
2. **库才是真源，代码里的种子已经过时。** `agent-decide` 在库里是 `prompt_version=24`，代码里的补丁链只到 V22，中间两版是在页面上直接改的，只存在于这一个 `mino.db` 里。换台机器、换个库，这两版就没了。
3. **历史版本挤在 JSON 列里。** `agent-decide` 的 `overrides_json` 有 493 KB，里面是 20 个修订版本。没法 diff、没法 blame、没法回滚到某次提交。
4. **真源不止一个。** 除了 `llm_jobs`，还有 `skills.system_prompt`（PROMPTS.md 已声明废弃，但列和数据都还在）、`settings.ai._role_prompts`、`settings.layer_stack`，以及 `skill_defs.py` / `roles_catalog.py` / `layer_stack.py` 里写死的目录。另有 4 处 prompt 直接写在 `.py` 里，根本没进 `llm_jobs`（§3.5）。

改成 git 管理后：diff、blame、回滚、分支实验都有了；`dispatch_log` 记下的版本号能直接对应到一次提交。

---

## 2. 目录

### 2.1 放在 `mino_nexus/` 下是否合理

**合理，但要收进一个 `prompts/` 目录。**

- **必须在包内。** Nexus 以 wheel 发布（`dist/mino_nexus-*.whl`），放在仓库根上的文件不会进安装包，装好的 Nexus 会找不到 prompt。放在 `mino_nexus/prompts/` 下，再在 `pyproject.toml` 的 `package-data` 里加 `prompts/**/*`，就能随包发布。
- **不要在包根上平铺五个目录。** CLAUDE.md §3 规定包根只放 `app.py` / `cli.py` 和既有的几个子包。五个纯数据目录和 `ai/`、`loop/` 这些代码包并列，会分不清哪些是代码、哪些是数据。收进 `prompts/`，一眼就知道「这里全是 prompt 数据，没有 Python」。
- **加载代码不放进 `prompts/`。** 读文件、校验、缓存的代码放在 `mino_nexus/ai/prompt_repo.py`。`prompts/` 下不放 `.py`，也不需要 `__init__.py`。
- `docs/prompts/`（现在只有一份 `agent-decide-v15-snapshot.md`）是说明文档，不是真源，保持不动。

### 2.2 结构

```
mino_nexus/prompts/
├── jobs/                             # Workbench /jobs 页
│   ├── agent-decide/
│   │   ├── job.yaml                  # 元数据、槽、调用参数、块的顺序
│   │   ├── system/
│   │   │   ├── 10-role.md            # 长文本块，一块一个文件
│   │   │   ├── 20-actions.md
│   │   │   └── 30-recovery.md
│   │   └── user/
│   │       └── 10-step.md
│   ├── assert-vision/ …
│   ├── case-step-compile/ …
│   └── analyze_req/ …
│
├── assistant/                        # V4 社交助手，独立维护
│   ├── jobs/
│   │   ├── assistant-route/          # 结构同 jobs/*
│   │   ├── assistant-reply/
│   │   └── assistant-summarize/
│   ├── persona/default.md            # 默认人设
│   ├── tools/                        # 助手工具表（替代 V4 方案里的 assistant_tool 表）
│   │   ├── case.run.yaml
│   │   └── case.search.yaml
│   └── cards/                        # MinoCard 模板（替代 assistant_card_template 表）
│       └── case_run_started.yaml
│
├── skills/                           # 技能：SOP、输入、视图、触发
│   ├── run-case.yaml
│   ├── explore-app.yaml
│   └── analyze_req.yaml
│
├── roles/                            # 角色：身份、归属的 job
│   ├── test-engineer.yaml
│   └── req-analyst.yaml
│
└── stack/                            # 编排：驱动 → 技能 → 角色 → 触发
    └── stack.yaml
```

Workbench 页面与目录一一对应：`/jobs` ↔ `jobs/`（加上 `assistant/jobs/`，在页面上单独分组），`/skills` ↔ `skills/`，`/roles` ↔ `roles/`，`/stack` ↔ `stack/`，`/assistant-tools` ↔ `assistant/tools/` 和 `assistant/cards/`。

> V4 社交方案里原本规划的 `assistant_tool`、`assistant_card_template` 两张表**取消**，改为本目录下的文件。工具描述本身就是给模型看的 prompt，应当和 prompt 一起走 git。

---

## 3. 文件格式

### 3.1 job

`job.yaml` 字段与现在的 `llm_jobs` 列一一对应，导出时不丢信息：

```yaml
id: agent-decide
version: 24                    # 沿用库里的 prompt_version，不从 1 重来
label: 看图决策
summary: 每轮看截图决定下一步动作
engine: vision_decide
role: test-engineer
output_schema: agent_decide
enabled: true
sort_order: 30
flags: []
slots:
  - {name: case_steps, kind: text, required: true}
  - {name: nav_assist, kind: text, required: false}
call:
  temperature: 0.1
  max_tokens: 1200
  timeout_sec: 60
  json_mode: true
image: {...}                   # 原 image_json
system:
  - {id: role, file: system/10-role.md}
  - {id: steps, slot: case_steps, heading: "【用例步骤】", max_chars: 6000}
  - {id: nav, slot: nav_assist, heading: "【导航 assist】", skip_if_empty: true}
  - {id: tail, text: "只输出一个 function call。"}   # 短文本可以内联
user:
  - {id: step, file: user/10-step.md}
```

- 块的字段（`id`、`text` / `file`、`slot`、`heading`、`empty_text`、`skip_if_empty`、`max_chars`、`enabled`）与 `prompt_render.py` 现在读取的字段完全一致。渲染逻辑不用改，只是数据换了来源。
- 长文本放 `.md` 文件，diff 时一行一行看。文件名前缀数字只用来排序，实际顺序以 `job.yaml` 为准。
- 不再有 `builtin`、`seed_rev`、`overrides_json`。修订历史交给 git。

### 3.2 skill

```yaml
id: run-case
version: 3
label: 执行测试用例
category: device
role: test-engineer
engine: agent_loop
sop: {...}            # 原 sop_json：phases / inspections / pointer
input: {...}          # 原 input_json
view: {...}           # 原 view_json
triggers: [...]
enabled: true
sort_order: 10
```

`skills.system_prompt` 列**不导出**。PROMPTS.md 已声明该列废弃，读侧走 job。导出脚本会把它的内容存进原始备份（§6.1），但不进新文件。

### 3.3 role

```yaml
id: test-engineer
version: 1
label: 测试工程师
group: runtime
kind: json
owner: ...
jobs: [agent-decide, assert-vision, inspect-session]   # 原 ROLE_JOB_MAP 及 roles_catalog 中的字段
```

`roles_catalog._product_roles()` / `_runtime_roles()` 里写死的行，以及 `settings.ai._role_prompts` 中的覆盖（当前为空）合并后导出。`PUT /settings/ai/roles/{id}/prompt` 删除。

### 3.4 stack

`stack/stack.yaml` 收纳 `layer_stack.py` 中的 `DRIVERS`、`SKILL_CATEGORIES`、触发列表、技能到驱动的映射，以及 `settings.layer_stack` 的绑定覆盖（当前为空 `{}`）。`PUT /settings/ai/stack` 删除。

### 3.5 散落在代码里的 prompt

以下几处 LLM 调用不经过 `llm_jobs`，system 文本直接写在 `.py` 里。其中英文的两处还绕过了 `verify_no_prompt_literals.py`，因为该脚本只认中文：

| 位置 | 内容 | 迁到 |
|---|---|---|
| `services/doc_learn.py` `DOC_LEARN_SYSTEM` | 文档事实抽取（英文） | `jobs/doc-learn/` |
| `services/knowledge_situation.py` `EXTRACT_SYSTEM` / `EXTRACT_BATCH_SYSTEM` | 知识分类（英文） | `jobs/knowledge-extract/`、`jobs/knowledge-extract-batch/` |
| `services/app_intel.py` 约 696 行 | 应用信息问答（中文，函数内拼接） | `jobs/app-intel-ask/` |
| `services/account_facet_schema.py` 约 224 行 | 账号池 Facet 设计（中文，函数内） | `jobs/account-facet-schema/` |

迁移后这几处改为通过 `prompt_repo` 取 job 并渲染。`verify_no_prompt_literals.py` 同时扩展：不再只认中文，凡是作为 `role: system` 内容的长字符串字面量，不论中英文都拦截。

---

## 4. 版本号

两层，各管一件事：

| 层 | 是什么 | 谁维护 | 用途 |
|---|---|---|---|
| `version` | 文件头里的整数 | 人，改内容时加 1 | 人看得懂的版本；写进 `dispatch_log`，在 Workbench 调用详情里显示 |
| `sha256` | 该条目所有文件内容的哈希 | `prompt_repo` 加载时现算，不落盘 | `dispatch_log` 同时记下，精确对应到文件内容 |

**不再提供独立的校验脚本，也不再有 lock 文件。** 原先设想的 `verify_prompts.py` 取消：它要么挂 pre-commit，要么挂发版流程，都会给开发环境添负担，而且「版本号没加 1」本来就是人改文件时自己的事。校验改为两处：

1. **启动时**：`prompt_repo.load_all()` 解析所有文件，YAML 不合法、必填字段缺失、`file:` 指向的文件不存在、块引用了未声明的槽、role / skill / stack 之间的引用断了，都**直接拒绝启动并打印原因**。沿用 `job_store._validate_job` 的规则。
2. **Workbench 预览**：`POST /settings/ai/jobs/{id}/preview` 用假槽值渲染一遍，看得见就是对的。

版本号加不加由人负责；`dispatch_log` 里记下的 `prompt_sha` 保证即使忘了加，也能查到当时到底用的是哪份内容。

`dispatch_log.record_llm` 增加 `prompt_sha` 字段（运行时对该条目的文件内容现算），原有的 `prompt_version` 保留。查问题时可以用 `git log -S` 或按 sha 找到当时的文件内容。

---

## 5. 运行时

### 5.1 `ai/prompt_repo.py`（新）

```python
# 示意
def load_all() -> Repo          # 启动时扫描 prompts/，解析、校验，失败直接拒绝启动并打印原因
def get_job(job_id) -> JobSpec  # 返回与原 job_store.get_job 同形状的 dict，下游不改
def get_skill(skill_id) -> dict
def list_roles() / get_stack() / list_assistant_tools() / get_card_template(id)
def fingerprint(kind, id) -> (version, sha)
```

- **对外形状不变**：`get_job` 返回的字典与原 `job_store.get_job` 一致，`planner.py`、`prompt_render.py`、`job_slots.py`、`case_step_span_llm.py` 等调用方只需要把 import 从 `job_store` 换成 `prompt_repo`。
- **启动即校验**：prompt 文件有错时 Nexus 启动失败并打印原因。不要像现在这样「补丁对不上就静默跳过」。
- 文件读取用 `importlib.resources`，保证从 wheel 安装后也能读到。

### 5.2 原有模块怎么处理

| 原模块 | 处理 |
|---|---|
| `services/job_store.py` | 只保留 `resolve_job_id`（别名表）、`preview_job`（渲染预览）、`startup_health`（改为读 `prompt_repo` 的校验结果），其余删除 |
| `services/skill_store.py` | 删除读写逻辑，改为薄封装 `prompt_repo`；删除 `seed_skills`、`upgrade_run_case_sop_*` |
| `ai/skill_defs.py` | 内容导出到 `skills/` 后删除 |
| `ai/roles_catalog.py` | 目录数据导出到 `roles/`；保留 `chat_with_role` 的调用逻辑（若 §7 之后仍有使用者） |
| `ai/layer_stack.py` | 目录数据导出到 `stack/`；保留拼装 `get_stack()` 返回值的逻辑 |
| `ai/job_upgrades.py` | **整个删除** |
| `core/bootstrap.py` | 删除所有 `upgrade_agent_decide_*`、`upgrade_assert_vision_*`、`upgrade_inspect_session_*`、`upgrade_case_step_compile_*`、`ensure_nav_*_job`、`upgrade_jobs_*`、`seed_skills` 调用；改为调用 `prompt_repo.load_all()` |

### 5.3 开发时的热加载

设置环境变量 `MINO_PROMPTS_RELOAD=1` 后，`prompt_repo` 每次取用时检查文件 mtime，有变化就重新加载并校验。改 prompt 不用重启 Nexus。生产环境不开，避免运行中途 prompt 被改动。

---

## 6. 迁移步骤：先取出来，再删除

四步，每一步单独提交。前一步验证通过后才做下一步。

### 6.1 第一步：全量导出（只读，不改库）

1. **备份库**：复制一份 `mino.db.bak-prompt-export-<日期>`，与现有 `mino.db.bak-*` 的命名一致。不对 `mino.db` 做任何写入。
2. **原始导出**：`scripts/export_prompts_from_db.py` 以只读方式打开库（`?immutable=1`），把以下内容**原样**导出到 `docs/prompts/db-export-<日期>/`，每张表、每个 key 一个 JSON 文件：
   - `llm_jobs` 全部行、全部列，包括 `overrides_json` 里的修订历史；
   - `skills` 全部行，包括已废弃的 `system_prompt` 列；
   - `settings` 中的 `ai._role_prompts`、`layer_stack`；
   - 导出时刻 `skill_defs.py`、`roles_catalog.py`、`layer_stack.py` 里写死的目录数据。

   这份原始导出是兜底。即使转换出错，也能从这里恢复任何一个字段。修订历史只保留在这里，不伪造成 git 提交。
3. **转换**：同一个脚本把上述数据转成 §3 的文件结构，写进 `mino_nexus/prompts/`；对每个 job 的长文本块拆成 `.md`。
4. **清点报告**：脚本最后打印对照表：库中行数、导出文件数、每个条目的 `version` 和字符数。两边数量不一致就报错退出。

**以哪个库为准**：以实际跑批用的那个 Nexus 实例的 `mino.db` 为准。当前本机 `~/.mino-nexus/mino.db` 里有 24 个 job、16 个技能，`_role_prompts` 与 `layer_stack` 都为空。已确认只有这一个 Nexus 实例，不需要多库对比。

### 6.2 第二步：逐条核对

`scripts/verify_prompt_export.py` 对每个 job 做两次渲染：一次读库里的行，一次读新文件，用同一组假槽值。要求**生成的 messages 逐字节相同**。技能比较 `sop` / `input` / `view` / `triggers` 的 JSON 是否相等；角色和编排比较 `list_roles()` / `get_stack()` 的返回值是否相等。

全部一致后，提交「导出 + 新文件」。这一提交不改任何运行代码。

### 6.3 第三步：切换读取路径

- 加 `prompt_repo.py`，所有读取方改为从文件读取。
- Workbench 的保存接口（`PUT /settings/ai/jobs/{id}`、`POST/PUT /settings/ai/skills`、`PUT /settings/ai/roles/{id}/prompt`、`PUT /settings/ai/stack`）改为返回 410，并提示「prompt 已改为在仓库中维护，文件路径为 …」。
- 删除 `bootstrap.py` 中所有 prompt 相关的种子和补丁调用。

跑几条用例，对比 `dispatch_log` 中的 `prompt_version` 与切换前一致，再做下一步。

### 6.4 第四步：删除 ORM 和死代码

- 删除 `models/llm_job.py`、`models/skill.py`，以及 `models/__init__.py` 中的导出。
- 删除 `ai/job_upgrades.py`、`ai/skill_defs.py`，以及 §5.2 中列出的函数。
- 删除 `settings_store` 中的 `_role_prompts`、`layer_stack` 读写函数。
- 删除 410 的那几个路由，Workbench 同步去掉调用。
- **不在代码里 DROP 表。** `llm_jobs` 和 `skills` 两张表留在库里，不再读写。确认运行稳定后，由人工在备份之后执行删除。这与 V3 清理 `user_plugin_secrets` 的做法一致，也符合 `migration.py` 只做增量变更的约定。

---

## 7. `im-dialogue`、`im-defect`、`test-engineer-chat` 能否删除

**可以删，但 `im-dialogue` 要等 assistant 接管微信回复之后再删。** 三者现在的使用情况：

| job | 现在被谁用 | 删除时机 |
|---|---|---|
| `im-dialogue`（角色 `im-qa-assistant`） | `node_registry._reply_wechat_message`：**线上微信回复就靠它**；`wechat_ilink.py`（遗留）；`plugins_store` 中插件试聊；`layer_stack`、`skill_defs`、`dispatch_log` 的别名 | V4 P1 用 `assistant_gateway` 替换 `_reply_wechat_message` 的**同一个提交**里删除。提前删会导致微信无法回复 |
| `im-defect`（角色 `im-defect-assistant`） | 只有 `plugins_store` 插件试聊的 `mode=defect` 分支，以及目录、别名。没有线上消息路径 | 随时可删 |
| `test-engineer-chat` | 只是 `ROLE_JOB_MAP` 中 `test-engineer` 角色在「设置页试对话」时用的 job。真机执行走 `agent-decide`，与它无关 | 随时可删。`test-engineer` 角色本身保留 |

删除内容：对应的 job 目录（第一步导出时照常导出，原始备份里保留）、`skills/` 中同名技能、`roles/` 中的 `im-qa-assistant` / `im-defect-assistant`、`stack.yaml` 中 `im_chat` 的绑定、`dispatch_log` 中的别名，以及 `plugins_store` 中的 `chat_roles` 和试聊函数。

**其它对话型 job**：`conductor`、`req-qa-bm`、`version-qa-bm`、`report-writer`、`doc-keeper`、`product-expert` 这 6 个对话型 job，在代码中除了目录定义、别名表和「设置页试对话」之外，没有找到任何业务调用方，Workbench 也不调用 `/settings/ai/roles/chat`。**已确认：这 6 个不删**，照常导出为 `jobs/` 下的文件，保持原样迁移。`knowledge-reviewer` 同样保留。

---

## 8. 对各模块的影响

### 8.1 MinoNexus

| 模块 | 影响 | 改动 |
|---|---|---|
| `mino_nexus/prompts/`（新） | **新增** | 全部 prompt、技能、角色、编排、助手工具和卡片模板 |
| `ai/prompt_repo.py`（新） | **新增** | 加载、校验、缓存、热加载、指纹 |
| `ai/job_upgrades.py` | **删除** | 3452 行 |
| `ai/skill_defs.py` | 删除 | 导出到 `skills/` |
| `ai/roles_catalog.py`、`ai/layer_stack.py` | 中 | 目录数据外移，只保留逻辑 |
| `ai/planner.py`、`ai/prompt_render.py`、`ai/job_slots.py`、`services/case_step_span_llm.py`、`services/qa_cover.py`、`services/nav_atlas_morph.py`、`services/account_facet_ai.py`、`catalog/skill_channel.py`、`loop/agent_loop.py`、`loop/registry.py`、`loop/observe/session_log.py`、`ai/knowledge_hint.py` | 小 | 把 import 从 `job_store` / `skill_store` 换成 `prompt_repo`，调用形状不变 |
| `services/doc_learn.py`、`services/knowledge_situation.py`、`services/app_intel.py`、`services/account_facet_schema.py` | 小 | 内联 prompt 搬进 `prompts/jobs/`，改为渲染 job |
| `services/job_store.py`、`services/skill_store.py` | 大幅精简 | 只保留预览、别名、健康检查 |
| `services/settings_store.py` | 小 | 删除 `_role_prompts`、`layer_stack` |
| `ai/dispatch_log.py` | 小 | 增加 `prompt_sha`；删除已废弃 job 的别名 |
| `models/llm_job.py`、`models/skill.py` | **删除** | 表保留，由人工清理 |
| `core/bootstrap.py` | 中 | 删除约 40 个种子和补丁调用，改为 `prompt_repo.load_all()` |
| `routers/rSettings.py` | 中 | 保存类接口先返回 410，再删除；读取类接口改读 `prompt_repo`，返回中增加 `version`、`sha`、`source_path` |
| `routers/rSys.py`、`services/plugins_store.py`、`services/node_registry.py` | 小 | 去掉对已删 job 和角色的引用（`node_registry` 随 V4 P1 一起改） |
| `pyproject.toml` | 小 | `package-data` 增加 `prompts/**/*`；增加 `pyyaml` 依赖 |
| `scripts/` | 新增 2 个一次性脚本 | `export_prompts_from_db.py`、`verify_prompt_export.py`，迁移完成后可删除。**不新增 `verify_prompts.py`**。`verify_no_prompt_literals.py` 扩展为中英文都拦截（该脚本目前未挂进 CI） |
| `docs/基础框架/PROMPTS.md`、`CLAUDE.md` §4 | 重写 | 真源改为 `mino_nexus/prompts/`；改 prompt 的流程改为「改文件 → 校验 → 提交」 |
| `loop/*` 的执行逻辑、协议、能力目录 | **无影响** | 只换了 prompt 的来源 |

### 8.2 MinoWorkbench

| 页面 | 改动 |
|---|---|
| `/jobs` | 只读。列表显示 `version`、`sha` 前 7 位、文件路径；详情页显示分块内容和渲染预览（保留 `POST /settings/ai/jobs/{id}/preview`）；assistant 的 job 单独分组 |
| `/skills`、`/stack` | 只读，显示版本和文件路径 |
| `/roles` | 去掉「编辑 prompt」，只读 |
| `/assistant-tools`（V4 规划） | 改为只读的浏览和预览页：工具描述、参数 schema、卡片多渠道预览。编辑在仓库里完成 |
| `src/api/adminCatalog.ts` | 删除 `PUT` 调用 |

### 8.3 MinoScout

无影响。Scout 不接触 prompt。

---

## 9. 已确认事项

1. 另外 6 个对话型 job **不删**，原样迁移。
2. 只有本机 `~/.mino-nexus/mino.db` 一个 Nexus 实例，不需要多库对比。
3. **不做 `verify_prompts.py`**，不挂 pre-commit，也不进 GitHub Action；校验靠启动时的加载检查。

## 10. 实施归属

本方案**由其他人负责开发**，不在 V4 社交接入的开发范围内。V4 开发期间对 prompt 的处理约定：

- V4 的 assistant job、工具表、卡片模板，**先按现状落在 `llm_jobs` / 代码里**，把内容集中放在一个位置（便于之后整体搬进 `prompts/assistant/`），不抢先建 `prompts/` 目录。
- 不改动 `llm_jobs`、`skills`、`job_store`、`skill_store`、`job_upgrades`、`roles_catalog`、`layer_stack`，避免和正在进行的改造冲突。
