# PROMPTS — prompt 归属与铁律

所有 LLM 调用都在本仓，且只在本仓（`../MinoScout` 有 `verify_no_llm.py` 守门）。

## 1. 归属

```
mino_nexus/ai/
├── llm_client.py     纯文本 OpenAI-compatible 客户端（重试、超时、JSON 抢救、流式早停）
├── planner.py        各 job 的入口函数
├── prompts.py        prompt 常量与拼装
└── schemas.py        结构化输出的 dataclass / 校验
```

| job | 入口 | 输入 | 输出 |
|---|---|---|---|
| `agent-decide` | `planner.decide_next_action` | 截图 + 菜单 + history + 知识 | 下一个 capability + params |
| `locate-vision` | `planner.locate_element` | 截图 + 元素描述 | 坐标 + 置信度 |
| `assert-visual` | `planner.assert_visual` | 截图 + 预期 | 成立/不成立 + 理由 |
| `persona-expand` | `planner.expand_persona_task` | 拟人任务描述 | 子事件序列 |
| `hitl-compose` | `planner.compose_hitl_prompt` | 卡点上下文 | 问人的话术 |
| `plan-overview` / `single-step-replan` | `ai/plan/prompt.py` | 用例原文 | 计划 |

**定位走纯 VLM**（截图 + prompt → 坐标）。拆分时已确认批次路径不经 CLIP / OCR / 组件检测 —— 不要把它们引回来。

## 2. 铁律

| # | 规则 | 为什么 |
|---|---|---|
| 1 | **坐标一律 0–1000 归一化千分比**，不是像素 | Nexus 不知道设备分辨率。换算在 Scout 侧做 |
| 2 | prompt 里只出现菜单里**有**的能力 | LLM 选了一个当前连通性下不可用的 executor，等于白跑一轮 fallback |
| 3 | 观察阶段不准动手 | 预期结果列只能看，用通用点击"点掉"预期来凑绿是禁止的 |
| 4 | 看图决策 ≠ 视觉校验 | 前者决定**下一步做什么**，后者判断**这一帧成不成立**。两个 job，两套 prompt，不要混 |
| 5 | 登录相关用例不自动登录 | 会把用例自己要测的步骤做掉 |
| 6 | 结构化输出必须过 `schemas.py` 校验 | LLM 会填不存在的 capability、越界坐标、缺字段 |
| 7 | 不重试业务错误 | 401/403/404/422 重试只浪费额度。只重试 408/409/425/429/5xx 和 JSON 解析失败 |

## 3. 改 prompt 的流程

1. 改 `prompts.py`，**不要就地改 `planner.py` 里的拼装逻辑**
2. **不要在 prompt 里硬编码具体 app 的包名、按钮文案** —— 那属于知识库（`knowledge_*`），有 owner、有作用域、有审核闭环

## 4. 成本与超时

| 项 | 规定 |
|---|---|
| 连接超时 | 10s |
| 读超时 | 跟调用方 `timeout_sec`，上限 600s |
| 流式空闲超时 | 20s |
| 流式早停 | `{` 之后一直空白、不出 JSON 键 → 4s 后掐掉，避免等满 `max_tokens` |
| `max_tokens` 截断 | **不重试**（同预算再打一次几乎必然再截断），由调用方拆批或加预算 |

每次调用都要经 `dispatch_log.record_llm` 落痕，便于事后归因"这一步为什么这么决策"。
