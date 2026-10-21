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

1. 直接修改 `mino_nexus/prompts/` 中的当前文件；不维护数字版本、不审批、不发布、不切换 A/B。
2. 重启 Nexus。启动时会解析所有 prompt 文件，格式错、引用断了会直接拒绝启动并说明原因。
3. 提交。生产环境重启 Nexus 后生效。开发时可设 `MINO_PROMPTS_RELOAD=1`，改文件不用重启。
4. **禁止**在 `mino_nexus/**/*.py` 里写 system prompt 字面量（`scripts/verify_no_prompt_literals.py`，中英文都拦）。

Git 保存提示词修改历史；`dispatch_log` 记录实际使用内容的 `prompt_sha`。兼容历史记录的数字字段不参与选择或准入。

Workbench 的 Jobs / 技能 / 角色 / 编排页只读，展示当前内容、sha 和文件路径。

历史修改通过 Git 回溯。运行时只加载当前文件，不应用历史升级补丁。

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

