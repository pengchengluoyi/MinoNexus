# 9月16日 — 用例执行调用 fsm_navigate 能力异常（方案 v2）

**应用**：造物相机 `app_id = 3d2b9799-0027-4c7d-bfe7-8c5b88f4087d`  
**关联**：`9月15日-fsm_navigate异常导致任务失败.md`、`9月16日-增加页面切换多态识别能力.md`、Atlas 真源 `page.sk*`（`nav_screen_registry.atlas_doc_for_navigation`）

**本版变更（相对 v1 方案）**：

1. 导航边语义 **`target_tab` → `target_page`**（全局改名 + 含义升级）。  
2. **Hierarchy 弱 / degraded** 时，localize 不单靠骨骼顶满置信，引入 **VLM 层级补全** 与 **agent-decide v10 `vlm_hierarchy`**。  
3. **agent-decide prompt v10**：结构化输出新字段；升级时 **v9 全文入 `revisions` 历史**。

**优先级**：**P0**（命名、target_page、assist/nav_attempt 对齐、v10 字段落库）；**P1**（VLM→localize 融合、组件映射）；**P2**（虚拟边 / 连通分量 UI）

---

## 0. 结论（先读）

1. **`nav_assist` 定位 100% ≠ `fsm_navigate` 能走通。** 定位来自 **localize**（结构/骨骼，弱 hierarchy 时更偏骨骼）；导航来自 **nav 边图 + `target_page` 可编译 tap**。两页都在 `states` 里但 **无 nav 边** → 无【路线】、规划 declined，属预期，需 **同文案降级**。

### 0.1 复盘：`cr-b758dd96c238::case-c516fa0a`（2026-09-16）

| 现象 | 真因 |
|------|------|
| `nav/attempt`：`resolved_to` 空，`to_name_score=0`，`plan_ok=false` | 目标口语「我的」**未出现在任何 state 的 display_name/aliases**；个人页实为 `page.sk9f40e317be03`（展示「页面 / 用户信息区」） |
| 多轮改 fuzzy / vlm / degraded 仍失败 | 修的是 **定位与证据**，未修 **Tab 文案 ↔ 节点命名** 断层 |
| 图上已有 `skd568…s0 → sk9`，`execute.text=我的` | **边上有语义，解析器只扫 state meta** → 规划在 `plan_route` 前终止 |
| fallback `tap 我的` 失败 | 开环在 `page.sk3f5a31a92f9fs5`（无底栏），与 9/15 fsm 文档同类 |

**已落地（不改用例 pass 逻辑）**：

- `nav_edge_resolve.resolve_state_via_nav_edges`：`to_ref` 对齐边的 `target_page` / `selector_text` → `edge.to`  
- `enrich_state_aliases_from_nav_edges`：把边点击文案写入目标 state `aliases`（live atlas 构建链）  
- `nav_alias_governance`：造物相机 `用户信息区*` → 展示名「我的」+ 别名（Console 规则，非聚类硬编码）  

**验收**：`plan_route_resolved(from=sk3f5a31a92f9fs5, to=我的)` → `resolved_to=page.sk9f40e317be03`，`ok=true`（2 hop）。

### 0.2 复盘：`cr-21861c667cf3::case-c516fa0a`

| 阶段 | 结果 |
|------|------|
| 解析 | `plan_ok=true`，`resolved_to=page.sk9f40e317be03`（**已越过** b758 的「解析拒绝」） |
| 规划 | 2 hop：详情 → `skd568…s0` → 我的页 |
| 执行 | `declined`：`tap_element` 无坐标；本步边为 **返回**（曾误编译成目标 Tab「灵感」；修文案后仍可能无「返回」节点） |
| 修复 | 返回边 → **`press_key` BACK**（`dispatch_spec_for_edge`）；见 **0.3** `cr-8498634a10fc` |

### 0.3 复盘：`cr-8498634a10fc::case-c516fa0a`

与 21861 相同：`plan_ok=true`，第一步 `…fs4_back_skd568…`。同任务 turn 2 模型 `press_key back` **pass**，说明应走系统返回键而非 `tap_element` 点文案。

| 改动 | 文件 |
|------|------|
| `dispatch_spec_for_edge` / `edge_is_system_back` | `nav_route.py` |
| fsm 按边派发 `press_key` / `tap_element` + `enrich_tap_params` | `local_executors.py`、`agent_loop.py`（`nav_hierarchy_nodes`） |
| 边 `execute.key=BACK` | `nav_screen_registry._hydrate_atlas_edge_execute` |

### 0.4 复盘：`cr-f3c66bc2d00a::case-c516fa0a`

| 阶段 | 结果 |
|------|------|
| 整案 | **pass**（`fsm_navigate` 不再 declined；`press_key BACK` 与边解析均生效） |
| Turn 1–2 | 目标「我的」、`plan_ok=true`，但每轮只执行路线图 **第 1/2 步**（`press_key` 回灵感壳），能力仍记 **pass** |
| Turn 3 | 在 `page.skd568…s0` 上 `tap_element(我的)` 才进入 `page.sk9…` |
| 旁证 | 全程 `hierarchy text_len=0`、`degraded=true`；Turn 3 在灵感壳上 `probe_hit` 命中「我的」底栏文案 |

**根因（本轮）**：不是路线缺失，而是 **多 hop 规划与单步执行语义错位**——执行层只做第一步，摘要却像「导航已完成」；深页→Tab 页最短路先走返回链，与用例话术「点底部 Tab」不一致但图结构如此。

**已落地（不改用例 check / `assert_visual` pass 逻辑）**：

| 改动 | 文件 |
|------|------|
| `nav_attempt.arrived_at_target` / `hops_remaining` + `correction_hint`（多 hop 未到目标仍可对单步标 pass） | `local_executors._fsm_navigate` |
| Tab 根态上若最短路为「先 back 再 Tab」，本步优先执行 **终端 Tab 边** | `nav_route.pick_fsm_first_step` |
| 步骤预期含「进入/跳转…页面」时，probe 仅命中短 Tab 文案 → **弱达成提示**（不怂恿 signal_done） | `step_effect.achievement_hint`、`step_pointer`、`agent_loop` |

**仍开放**：登录用例与冷启动落点不一致（prep/账号态）；`vlm_hierarchy` 融合（P1）；批跑不强制冷启动（§8 非目标）。

### 0.5 复盘：`cr-3fd3e784802f::case-c516fa0a`

| 阶段 | 结果 |
|------|------|
| Turn 1 `fsm_navigate` | `plan_ok=true`，本步误点路线图边「下一个结果」（内容区轮播），`arrived_at_target=false`，能力仍 pass + `correction_hint` |
| Turn 2 | **declined**：同规划首边，`degraded=true`，`tap_element` 无坐标 |
| 后续 | 模型 `tap_element(我的)` 成功 |

**根因**：深页→Tab 最短路第一步被 Atlas 记成 **内容控件边**（非 BACK / 非 Tab）；`pick_fsm_first_step` 此前只在 **Tab 壳起点** 直跳终端 Tab。

**已落地（2026-09-16 下午）**：

| 改动 | 文件 |
|------|------|
| 深页、目标为 Tab 页、首跳非 BACK 时本步 **直执行终端 Tab 边**（`deep_page_direct_tab`） | `nav_route.pick_fsm_first_step` |
| agent-decide：`screen_layout` / `vlm_hierarchy` 走 **tool 参数** + 正文 JSON 合并 | `catalog/tool_schema.py`、`llm_client._parse_chat_json` |
| prompt **v12**（说明 function call 承载布局）+ revisions | `job_upgrades.upgrade_agent_decide_to_v12`、`bootstrap` |

### 0.6 复盘：`cr-83b57d410a49::case-c516fa0a`（跑批改 stored FSM 之后）

| 阶段 | 结果 |
|------|------|
| 解析 | `to` → `page.tab_我的`（0.95）；`from=潮玩悟空作品详情页` name=0 / screen=0 |
| localize | `chosen=""`，`band=recover`，`confidence=0`（详情栈，无底栏） |
| 执行（修前） | **declined 3ms**：`_skip_tab_fallback` 跳过直点 Tab，把 BACK 踢回模型 |
| 后续 | 模型 `press_key BACK` pass → `recover_restart` fail → `tap_element(我的)` pass |

**根因**：跑批 `use_live=False` 后 VLM 页名对不上 stored 节点；recover 跳过 Tab 直点是对的，但 **declined 而不是执行系统返回**，白烧一轮 decide。

**已落地**：`_skip_tab_fallback` 时本步 `press_key BACK`（最多 3 次），`step_pick=recover_press_back`。见 `loop/local_executors.py`。

### 0.7 复盘：localize 选不出当前页 → `fsm_navigate` 只会 BACK（2026-09-17）

0.6 把 declined 改成 BACK 之后，详情栈能退出，但 **Tab 根态仍然 `chosen=""`**：跑批读 published `v1`（只有 `page.tab_*` + 底栏文案），draft 里的 `page.sk*` / `state_wireframes` 没用上；底栏几个 Tab 同时可见时纯文案一律同分。

**已落地**（契约见 [NAVIGATION_ATLAS.md](NAVIGATION_ATLAS.md) §2.2 / §17.4）：

| 改动 | 文件 |
|------|------|
| 跑批 `load_fsm_doc(use_live=False)` 把 draft Atlas 叠进 `v1` | `nav_route.overlay_atlas_for_runtime` |
| 底栏纯文案折价 0.2；骨骼 Jaccard / layout_framework 辨页；通知栏 landmark 不 `required_miss` | `nav_localize` |
| `hierarchy_weak` 看可见文案节点数 | `hierarchy_slots.hierarchy_is_weak` |
| 无底栏才 BACK；底栏可见则直点目标 Tab | `local_executors._skip_tab_fallback` |

不在 nav 代码里写被测 App 文案。验收：有骨骼的 Tab 根态 `localized.chosen` 非空；详情栈仍 BACK，但 assist 能报出 `page.sk*`。

2. **`target_tab` 表述误导模型与编译器**：历史字段暗示「底栏选中态 / Tab 文案」，而 Atlas 边大量是 **内容区入口、返回、列表项**（详情→我的 甚至当前屏无底栏）。统一改为 **`target_page`**：表示 **跳转后或所点控件关联的「目标逻辑页」**（展示名 / 别名 / `page.sk*`），与 Tab selected 解耦。

3. **Hierarchy degraded 时骨骼 localize 可能虚高**（例：免责声明当页名仍 100%）。此时 **不能**单独信 assist；每轮 **agent-decide** 应输出 **`vlm_hierarchy`**（与 `accessibility_json` 同形），供 **localize 补强、tap 锚点、fsm 边校验**。

4. 展示名须按 **用例 + PRD + UI** 治理；「内容均由AI生成」宜为 **别名**，主名用业务名（潮玩详情 / 造物结果）。

---

## 1. 术语：`target_page` 与 `target_tab` 迁移

### 1.1 语义对比

| 旧 `target_tab` | 新 `target_page` |
|-----------------|------------------|
| 底栏 Tab 文案、`tab_bar.selected` | **逻辑页**指向：架构 `display_name` / `aliases` / 可解析口语 |
| effect_assert 常写 `tab_bar.selected` | 仍以 **屏态印证** 为主：`text_landmarks` / 骨骼 / localize chosen |
| 模型 param「Tab 切换」 | 工具描述改为「按路线图进入目标**页**」 |

**仍保留的结构能力（不改名）**：`tab_slot_index`、`anchor_between`（无文案底栏槽位）、`meta.action_type=tab`（仅表示交互类型是点底栏，执行载荷用 `target_page` 填展示名）。

### 1.2 落点范围（全局迁移）

| 层 | 文件/对象 | 改动 |
|----|-----------|------|
| 边 `execute` | `nav_screen_registry._hydrate_atlas_edge_execute`、`nav_synthesis`、`nav_fsm` 边 | `target_tab` → `target_page`；读路径 **兼容旧键 1 个版本** |
| 编译 tap | `nav_route.tap_params_for_edge`、`nav_compiler.edge_action_label` | 优先 `target_page` → `selector_text` |
| 展示 | `nav_live_graph.enrich_ui_logic_edges` | UI 文案「点击页 · {name}」 |
| 协议/文档 | `docs/NAVIGATION_ATLAS.md`、`docs/PROTOCOL.md`（边 execute 小节） | 字段表更新 |
| DB 已发布边 | 启动 migration 或 `prepare_atlas_doc_for_nav_runtime` 归一化 | 写入时双写可选，读时 `target_page \|\| target_tab` |

**验收**：全仓 `rg target_tab` 仅余 **兼容读取** 注释或迁移脚本；新采集边仅写 `target_page`。

### 1.3 与 `fsm_navigate` 参数

`tool_schema.fsm_navigate` 描述改为：`from_state` / `to_state` 为 **当前/目标逻辑页**（`page.sk*` 或展示名/别名），**不再写「Tab 文案」为主话术**。  
`to_state: "我的"` → 解析到展示名为「我的」的 `page.sk*`；边第一步 `execute.target_page: "我的"` → tap 编译。

---

## 2. 页面名称、别名与组件映射（保留并收紧）

### 2.1 真源

| 层级 | 写入 | 消费者 |
|------|------|--------|
| `display_name` | Studio / API，对齐 UI 标题 | assist、`resolve_state_fuzzy`、`target_page` |
| `aliases[]` | 用例口语、旧名、合规原文 | 解析、模型 from/to |
| `component_labels` / pin | 无文案控件 | tap、`target_page` 指向子入口时 |
| `page.sk*` | 聚类 | localize、边端点 |

工作流不变：盘点 session → 对照 PRD/UI → 改 Atlas → live 导航生效（重启 Nexus）。

### 2.2 无文案组件

边 meta 除 `target_page` 外必须能落 **selector_text / tab_slot_index / hotspot / 千分比坐标** 之一；否则 `nav_attempt` 标明 `missing_tap_compile`。

---

## 3. Hierarchy 质量与 VLM 补全

### 3.1 问题

| 状态 | Scout hierarchy | localize 行为 | 风险 |
|------|-----------------|---------------|------|
| 正常 | `nodes` 充足 | tab_bar + landmarks + 骨骼 | 低 |
| degraded | `hierarchy_text` 空或极短 | `degraded=true`，tab_bar **判不了** | 骨骼 Jaccard 仍可能 **高置信** |
| 无 selected | 有节点但无 selected 属性 | tab_bar 仅文案命中，折价 | 与真实「当前 Tab」不一致 |

**原则**：`band=recover` 或 `degraded=true` 时，**禁止**仅凭骨骼将 assist 置信展示为 100% 且不给脚注；若骨骼与 hierarchy/VLM 冲突，**降权并标注「待 VLM 印证」**。

### 3.2 双通道层级（运行时）

```text
observe 截图 + Scout hierarchy
        │
        ├─► hierarchy 可用 ──► nav_localize（规则）
        │
        └─► degraded / tab_bar 弱 ──► 同轮 agent-decide 必填 vlm_hierarchy
                    │
                    └─► 融合层（P0 规则，P1 学习权重）
                          → 补强 nodes / text_landmarks
                          → 可选覆盖 localize chosen（VLM 与骨骼差 > 阈值）
                          → 写入 turn meta / session_log（可审计）
```

**融合规则（P0 建议）**：

- `vlm_hierarchy.nodes` 与 Scout `nodes` **按 bounds IoU + class 合并**，冲突以 **Scout 坐标为准、VLM 补 text/desc**。  
- localize 增加信号 **`vlm_landmarks`**（权重介于 tab_bar 与 text_landmarks 之间），仅当 `degraded || tab_bar<0.3`。  
- **不**在 Nexus import 图像算法；VLM 只在 **agent-decide / assert-vision** 链路。

### 3.3 与方案二（多态）的边界

VLM 层级用于 **单帧可交互元素与页级语义**；feed 多态合并仍走 **方案二 morph**，不在此重复。

---

## 4. agent-decide prompt v10：`vlm_hierarchy`

### 4.1 目标

让大模型在 **每步决策 JSON** 中返回 **`vlm_hierarchy`**：基于截图的层级与元素信息，**与协议 `accessibility_json` 节点数组同形**，便于：

- 填入 `inspect_slots` / 与 Scout 合并；  
- `nav_localize` 在 degraded 时使用；  
- 模型自己 tap 时有 `selector_text` 候选；  
- 审计：对比 Scout dump 与 VLM 差异。

### 4.2 输出形状（与 `AgentDecision` 对齐）

在 `mino_nexus/ai/schemas.py` · `AgentDecision` 增加（v10）：

```json
{
  "thought": "...",
  "status": "continue",
  "action": { "capability_id": "tap_element", "params": { "x": 500, "y": 900, "selector_text": "我的" } },
  "expected_after": "...",
  "screen_layout": { /* 已有 v8 线框 */ },
  "vlm_hierarchy": {
    "hierarchy_format": "accessibility_json",
    "degraded_scout": true,
    "nodes": [
      {
        "resource_id": "",
        "text": "我的",
        "content_desc": "",
        "class": "android.widget.TextView",
        "clickable": true,
        "bounds": [800, 2200, 950, 2300],
        "center": [875, 2250]
      }
    ]
  }
}
```

| 字段 | 说明 |
|------|------|
| `hierarchy_format` | 固定 `accessibility_json`（`docs/PROTOCOL.md` §4.4.2） |
| `degraded_scout` | 模型自报：本轮是否认为 Scout hierarchy 不可用/不可信 |
| `nodes[]` | 与 Scout RESULT `nodes` **同字段**；`bounds`/`center` 为 **设备像素**（与协议一致，tap 仍用千分比 params） |
| 可选 `page_summary` | 一句业务页描述（供写入别名建议，**不进** identify 硬编码） |

**约束（v11 起写入 agent-decide / inspect-session 正文）**：

- **`screen_layout` 与 `vlm_hierarchy` 每轮必填**（与 assert-vision 对齐）；不再用「仅 weak hierarchy 才填」的 prompt 分支——程序侧按需消费，空 `nodes` 可忽略。  
- 只列 **可见且与当前步骤相关** 的节点（≤40 个），避免整屏涂鸦。  
- 不要编造 `resource_id`；无则空串。  
- 与 `screen_layout` 分工：layout 管区域框；hierarchy 管 **可点名控件文案**。  
- `thought` 与 `vlm_hierarchy` 一致；与 Scout 冲突时以 Scout 坐标为准。

### 4.3 程序升级（`job_upgrades`）

| 项 | 要求 |
|----|------|
| 标记 | `AGENT_DECIDE_V10_MARKER = "prompt_version >= 10（vlm_hierarchy）"` |
| 函数 | `upgrade_agent_decide_to_v10()`：`_patch_agent_decide_v10` + 可选槽说明块 |
| **版本历史** | 升级前 `revisions.append({ version: 9, note: "v9 before program upgrade to v10", ..._blocks_snapshot(row) })`，与 v7→v8→v9 **同一模式** |
| bootstrap | `core/bootstrap.py` 在 v9 之后调用 `upgrade_agent_decide_to_v10` |
| 解析 | `planner.decide_next_action` 保留 `vlm_hierarchy` 入 `AgentDecision`；`parse_warnings` 记录 schema 裁剪 |
| 落痕 | `session_log`：`llm/decision` 或 `context/slots` 增 `vlm_hierarchy` 摘要（nodes 数 + top texts） |

**v9 历史**：不得覆盖 v9 正文；Console `revisions` 中可 **activate_version** 回滚到 v9 快照。

### 4.4 prompt v10 正文要点（补丁内容纲要）

- 何时 **必须** 填 `vlm_hierarchy`：`==== hierarchy` 为空、或导航 assist 置信低、或准备 `fsm_navigate` / 底栏相关步骤。  
- 如何用：可与 `selector_text` 互证；**不要**用 Tab 硬编码表（仍遵守 no-app-keywords）。  
- 与 **导航 assist** 关系：assist 写「当前逻辑页」；`vlm_hierarchy` 写「看见的可点元素」；`target_page` 边指向 **页** 不是 selected 态。

---

## 5. 异常案例（更新解读）

**现象**：assist `当前屏：内容均由AI生成（page.sk…，置信 100%）`；`fsm_navigate(内容均由AI生成 → 我的)` 失败、无路线。

| 根因类 | v2 对策 |
|--------|---------|
| 展示名=噪声文案 | §2 名称治理 |
| 图无 nav 边 | 补采 / 手工边；assist 与 `nav_attempt` 同提示「无连通边」 |
| 详情无底栏仍尝试点目标 Tab | 无底栏槽位时本步 `press_key BACK`；底栏可见才直点 |
| 骨骼 100% 但 hierarchy degraded | v10 `vlm_hierarchy` + localize 降权；assist 脚注「结构定位，未印证 Tab」 |
| from 重复解析 | 优先 `localized.chosen` |

---

## 6. 实施路线图

### P0（可并行）

1. **`target_page` 迁移**：边读写、tap 编译、错误文案、tool_schema 描述；兼容 `target_tab` 只读。  
2. **名称治理**（造物相机关键 `page.sk*` 表 + 别名）。  
3. **assist ↔ `nav_attempt` 同源 plan_error**（含无 nav 边 hint）。  
4. **`upgrade_agent_decide_to_v10` + revisions 存 v9**；`AgentDecision.vlm_hierarchy`；planner 透传。  
5. **degraded 时 assist 置信封顶**（与方案二 `evidence_tier` 可对齐，先写死 band 规则）。  
6. **边索引解析 + 别名回填**（`nav_edge_resolve.py`）：`resolve_state_fuzzy` 在 name 不足时走 `nav_edge_to`；`atlas_doc_for_navigation` 链上 `enrich_state_aliases_from_nav_edges`。  
7. **fsm 单步语义**：`arrived_at_target` / `hops_remaining` / `correction_hint`；Tab 根 `pick_fsm_first_step`；弱 `probe` 达成提示（见 **0.4**）。

### P1

6. **VLM hierarchy 融合进 localize**（`vlm_landmarks` 信号）。  
7. **turn 持久化** `vlm_hierarchy` 到 capture meta（可选压缩）。  
8. 组件 pin + `target_page` 联调 `fsm_navigate` 第一步 tap。

### P2

9. Scout vs VLM 差异 UI（Studio 调试）。  
10. `tab_root_sk` 虚拟边（产品开关）。

---

## 7. 验收

1. 新边 JSON 仅含 `target_page`；`tap_params_for_edge` 对「我的」类目标页可编译。  
2. degraded 会话：agent-decide 输出含 `vlm_hierarchy.nodes`；session_log 可查；localize **不再**在仅骨骼命中时显示 100% 且无说明。  
3. `llm_jobs.agent-decide`：`prompt_version=10`，`revisions` 中存在 **version=9 完整快照**；`reset` / `activate_version` 可回到 v9。  
4. 潮玩详情→我的：改名 + 补边后，`fsm_navigate` 有路线或 declined 文案与 assist【路线】一致。

---

## 8. 非目标

- 不把 `target_page` 等同于 **Tab selected** 状态机。  
- 不用 VLM 替代 Scout 设备 dump 作为协议真源（VLM 为 **补强**）。  
- 不在 prompt 写死任何 App Tab 白名单。  
- 批跑强制冷启动仍不做。

---

## 9. 代码索引（实施）

| 主题 | 路径 |
|------|------|
| target_page / tap | `services/nav_route.py`、`nav_screen_registry.py`、`nav_synthesis.py` |
| 边 → 目标屏解析 | `services/nav_edge_resolve.py`、`services/nav_state_resolve.py` |
| fsm 本步边选取 | `services/nav_route.py`（`pick_fsm_first_step`） |
| 步骤 probe 提示 | `loop/step_effect.py`、`loop/local_executors.py` |
| assist | `services/nav_compiler.py`、`loop/nav_runtime.py` |
| localize 融合 | `services/nav_localize.py`、`loop/inspections.py` |
| agent-decide v10 | `ai/job_upgrades.py`、`ai/schemas.py`（`AgentDecision`）、`ai/planner.py`、`core/bootstrap.py` |
| hierarchy 协议 | `docs/PROTOCOL.md` §4.4.2、`docs/NAVIGATION_ATLAS.md` |
| prompt 铁律 | `docs/PROMPTS.md` |
