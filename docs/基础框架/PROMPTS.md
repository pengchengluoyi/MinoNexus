# PROMPTS — prompt 归属与铁律

所有 LLM 调用都在本仓，且只在本仓（`../MinoScout` 有 `verify_no_llm.py` 守门）。

## 1. 真源分工

| 内容 | 真源 | 说明 |
|---|---|---|
| **prompt 正文、技能、角色、编排** | `mino_nexus/prompts/` | git 是唯一真源。`jobs/`、`skills/`、`roles/`、`stack/` |
| **加载** | `platform/llm/repo.py` | 默认只在进程启动时校验并加载。`MINO_PROMPTS_RELOAD=1` 时，每次读取比对整棵 `prompts/` 的内容哈希，变了就重新加载并校验 |
| **渲染** | `ai/prompt_render.py` | 块 → messages。形状与以前的 job 行一致 |
| **步骤指针措辞** | `loop/step_pointer.py` | 运行时注入槽，不写进 job 模板 |

```
mino_nexus/
├── platform/llm/repo.py    读 prompts/、校验、指纹；list_assistant_tools() 也从这里取
├── platform/llm/render.py  块 → messages
├── ai/job_slots.py         槽值生产者（Python）
└── prompts/                prompt 与 assistant/tools/*.yaml。不放 Python
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

## 3. 改 prompt 或助手工具 yaml 的流程

1. 直接修改 `mino_nexus/prompts/` 中的当前文件（含 `prompts/assistant/tools/*.yaml`）；不维护数字版本、不审批、不发布、不切换 A/B。
2. 默认必须重启 Nexus。加载器只在启动时解析；格式错、引用断了会拒绝启动并说明原因。未设置 `MINO_PROMPTS_RELOAD` 时，改完 yaml 不会进入 `list_assistant_tools()`，MCP `tools/list` 仍是旧表。
3. 开发时设 `MINO_PROMPTS_RELOAD=1` 后再启动。之后每次取 job、技能或助手工具都会比对 `prompts/` 的内容哈希（不是只看最大 mtime），有变化就整表重载并重新校验。下一次 MCP `tools/list` 或助手调用能看到新工具，不必再重启进程。该模式下 MCP `initialize` 的 `tools.listChanged` 为 true，支持该通知的客户端会重新拉列表；不支持的客户端要自己再调一次 `tools/list`。生产环境不要开这个开关。
4. 提交。生产环境重启 Nexus 后生效。
5. **禁止**在 `mino_nexus/**/*.py` 里写 system prompt 字面量（`scripts/verify_no_prompt_literals.py`，中英文都拦）。

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

