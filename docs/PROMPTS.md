# PROMPTS — prompt 归属与铁律

所有 LLM 调用都在本仓，且只在本仓（`../MinoScout` 有 `verify_no_llm.py` 守门）。

## 1. 真源分工

| 内容 | 真源 | 说明 |
|---|---|---|
| **prompt 正文** | `llm_jobs` 表 | 块列表、call 参数、槽声明（Console Jobs 页） |
| **阶段程序** | `skills.sop_json`（`run-case` 一行） | phases / inspections / pointer |
| **步骤指针措辞** | `loop/step_pointer.py` | 模板常量；运行时注入 `checkpoints_block` / `success_criteria` 槽，替代 job 内 when 分块 |
| **角色 override** | `settings._role_prompts` | 仅 IM 等可编辑角色；跑批读 `llm_jobs` |

```
mino_nexus/
├── ai/
│   ├── llm_client.py      OpenAI-compatible 客户端
│   ├── planner.py         各 job 入口（内部 render()）
│   ├── prompt_render.py   块 → messages
│   └── job_slots.py       槽值生产者（Python）
├── services/job_store.py  读写 / 校验 / preview
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

1. **生产环境**：Console → **Jobs** → 选 job → 编辑块 → 保存 → `GET /settings/ai/jobs/health` 确认无 broken
2. **禁止**在 `mino_nexus/**/*.py` 写模块级中文 prompt（`scripts/verify_no_prompt_literals.py` 守门）
3. **不要**在 `skills.system_prompt` 列维护跑批 prompt —— 该列已废弃，读侧走 `job_store`

每次保存会递增 `prompt_version`（当前生效版本）；`dispatch` 调用记录带 `prompt_version`，Studio 调用详情在技能旁展示。

恢复上一版：`PUT /settings/ai/jobs/{id}` body `{ "reset": true }`。恢复指定历史：`{ "activate_version": 3 }`（会生成新版本号并启用该内容）。

## 4. 铁律

| # | 规则 | 为什么 |
|---|---|---|
| 1 | 坐标一律 **0–1000 千分比** | Nexus 不知道分辨率 |
| 2 | prompt 里只出现菜单里**有**的能力 | 否则白跑 fallback |
| 3 | 观察阶段不准动手 | 校验阶段不能改界面 |
| 4 | 看图决策 ≠ 视觉校验 | 两个 job，两套 prompt |
| 5 | 结构化输出过 `schemas.py` | LLM 会乱填 |
| 6 | 配置 key 必须来自 `loop/registry.py` 闭集 | 禁止 JSON 表达式 |

## 5. 成本与落痕

每次 LLM 调用经 `dispatch_log.record_llm` 落痕；`dispatch.bind(job=...)` 必须带 job id，**禁止**再从 system 文本反推 job。

本地校验（不进 CI）：

```bash
python tests/test_prompts.py
python scripts/verify_no_prompt_literals.py
python scripts/e2e_jobs_smoke.py
```
