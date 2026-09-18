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

### 0.8 复盘：`cr-5ecb8549ce72::case-c516fa0a` —— localize 已经准了，路线仍必然无路（2026-09-17）

**localize 不再是瓶颈**（`session_events` 实测，4 turn 全 `result=pass`、`degraded=false`）：

| turn | `localized.chosen` | confidence / band | 模型这一步做了什么 |
|------|--------------------|-------------------|--------------------|
| 1 | `page.sk3f5a31a92f9fs5` | 0.714 / explore（ambiguous） | 模型自己 `press_key` |
| 2 | `page.sk3f5a31a92f9fs4` | 0.962 / high | `fsm_navigate(→我的)` → **`step_pick=recover_press_back`** |
| 3 | `page.skd568c2708674s0` | 0.952 / high | 模型 `tap_element(我的)`，成功 |
| 4 | `page.sk9f40e317be03` | 0.823 / high | `assert-vision` pass |

整案 pass，但 turn 1–2 白烧。turn 2 的 `nav/attempt`：`resolved_from=page.sk3f5a31a92f9fs4`、
**`resolved_to=page.tab_我的`**、`plan_ok=false`、`plan_error=…两页分属不同连通分量`。

**根因（三条，都不在 localize 上）**

| # | 根因 | 证据 | 位置 |
|---|------|------|------|
| 1 | **跑批那份图里同时有两代节点，且互不连通。** published `v1`（`fsm_id=4`，`synthesis_mode=tab_bar_layered`）只有 5 个 legacy `page.tab_*` + 5 条 tab 边；draft（`fsm_id=6`）只有 5 个 `page.sk*` + 14 条 atlas 边。`overlay_atlas_for_runtime` 把 draft **叠加**上去而不删旧节点 → 运行时 10 states / 19 edges，**两个连通分量**。且 `meta.tab_bar.labels` 仍是 `page.tab_我的→"我的"`，`resolve_state_fuzzy` 走 `legacy_ref` 0.95 直接命中旧节点 → `shortest_nav_path` 恒空 | `load_fsm_doc(use_live=False)` 返回 `source=published+atlas`；`resolve_state_fuzzy(doc,"我的")` → `page.tab_我的`，`method=legacy_ref` | `nav_route.overlay_atlas_for_runtime`、`nav_route.resolve_state_ref`（`tab_bar.labels` / `entries` 子串两条分支都会命中） |
| 2 | **`nav_fsm_store` 落库时把 state 的 `meta` 整块丢掉。** `save()` 只写 `state_id/kind/identify/guards/wiki_ref/entry/role`，`_state_public()` 也不返回 `meta`；`nav_fsm_states` 表根本没有 meta 列。于是 `display_name` / `aliases` / `page_role` / `tab` / `visit_count` 在发布那一刻全部消失 —— §0.1 的 `enrich_state_aliases_from_nav_edges`（`nav_candidate_compiler` 第 1290 行刚写进 meta）紧接着被 `save_draft` 抹掉 | 运行时 `_state_labels(page.sk9f40e317be03)` 只剩 `['sk9f40e317be03']`；把 legacy 节点删掉后 `resolve_state_fuzzy("我的")` → `method=no_match`。补一个 `display_name="我的"` 后同一份图立刻 `ok=true`、2 hop | `nav_fsm_store.save` / `_state_public`、`models/nav_fsm.py` |
| 3 | **`page.sk*` id 不稳定，而短 id 参与模糊匹配。** live 重建出的是 `page.skd568c2708674` / `page.sk9f40e317be03s18`，已发布的是 `page.skd568c2708674s0` / `page.sk9f40e317be03` —— 同一屏两套 id。`_state_labels` 又把 `sid.replace("page.","")` 当可匹配标签，`_similarity("sk3f5a31a92f9fs4","sk3f5a31a92f9fs0")=0.94`，且 `looks_like_id=True` 会**跳过** `role=="from"` 的屏态印证 → 一个**不存在**的 `page.sk*` 被静默解析成另一屏（实测 live 图上 `page.sk3f5a31a92f9fs4` → `page.sk3f5a31a92f9fs0`，`name_score=0.81`） | 同上脚本 | `nav_state_resolve._state_labels` / `resolve_state_fuzzy` |

**为什么表现成「只会 BACK」**：`_skip_tab_fallback` 只在 **plan 失败** 分支里被问到，而它守的是**唯一的兜底策略**——`direct_tab_tap_params` 直点目标页展示名（默认这个名字就在底栏上）。底栏槽位 < 2 就认为直点必败 → `press_key BACK`。因为根因 1+2 让 plan **100% 失败**，兜底成了唯一路径，`fsm_navigate` 退化成 BACK 机。附带：`is_tab_shell_state` 读的 `tab_bar.entries/labels` 在运行时指向 legacy id、在 live 里是空的，所以 `pick_fsm_first_step` 的 `tab_shell_terminal` / `deep_page_direct_tab` 两条捷径同样永不触发。底栏本身不是导航前提，只是这条兜底的前提。

**已落地（2026-09-17，P0 四条按依赖顺序）**

| # | 改动 | 文件 |
|---|------|------|
| 1 | `nav_fsm_states` 加 `meta` 列（additive ALTER），`save` / `_state_public` 双向带上 `meta` —— 不做这条，任何命名治理在跑批路径上都是空转 | `models/nav_fsm.py`、`core/migration.py`、`nav_fsm_store.save` / `_state_public` |
| 2 | 运行时只认一代节点：`prune_superseded_tab_states` 剔除 legacy `page.tab_*` 节点与其边，并清掉 `tab_bar.entries/labels` / `recover.default_*` 里指向已删节点的引用；`_adopt_atlas_tab_bar` 把 `home/launch` 换到骨骼那一代。合成侧同样剔除，否则每次重发都会把旧节点带回来 | `nav_route.prune_superseded_tab_states` / `overlay_atlas_for_runtime`、`nav_candidate_compiler.merge_synthesized_doc` |
| 2b | `resolve_state_ref` 删掉 `tab_bar.entries` 的 **id 子串**分支（`"我的" in "page.tab_我的"` 等于给旧节点开后门）；并改成**分层扫**：id 后缀 → `display_name` → 别名 → identify。别名是采集噪声池（一页常十几条），与展示名同层按 states 顺序先到先得会让「我的发布」被另一页的同名别名截走 | `nav_route.resolve_state_ref` |
| 3 | `_state_labels` 不再把裸 `sk*` 短 id 当标签；`resolve_state_fuzzy` 对 id 形态的 ref（`page.*` / `tab_*` / 裸 `sk<hex>`）只允许**精确存在**，否则 `unknown_state_id`，由 `localized.chosen` 兜 | `nav_state_resolve.looks_like_state_id` / `_state_labels` / `resolve_state_fuzzy` |
| 4 | `recover_press_back` 不再伪造 `planned_hops`（原来 `max(2, n+1)` 会让摘要报「本步为路线图第 1/2 步」，而根本没有路线图）；`correction_hint` 换成 `plan_error` 原文 + `tab_root_entry_hint`（「退到并列入口「X」后 N 步可到目标」） | `local_executors._fsm_navigate`、`nav_route.tab_root_entry_hint` |

**数据侧**：hierarchy 采集不用清（`nav/capture/*` 与骨骼节点都是好的）。只把 published `v1` 从 Atlas 重发了一次
（`scripts/republish_nav_fsm_from_atlas.py <app_id> --write`，走 `promote_prepared`；库已备份 `mino.db.bak-fsm-tab-legacy-*`）：

| | 重发前 | 重发后 |
|---|---|---|
| `v1` | 5 states / 5 edges，**全是** `page.tab_*`，带名 0 个 | 8 states / 22 edges，**无** legacy，带名 8 个 |
| 跑批实际看到 | `published+atlas` 叠出 10 states，两个连通分量 | `published` 单一份，8 states |
| 潮玩详情 → 我的 | `plan_ok=false`（不连通） | 8 个页面**全部**有路径（1–2 hop） |

**验收（实测）**：`resolve_state_fuzzy("我的")` → `page.sk9f40e317be03s18`；失效 id `page.sk3f5a31a92f9fs4`
→ `unknown_state_id`（不再静默落到 `…fs0`），带 `localized.chosen` 时仍能规划出 2 hop。
回归见 `tests/test_fsm_nav_semantics.py`（新增 4 例）与 `tests/test_nav_localize.py`（叠图契约改为「只留骨骼那一代」）。

**仍开放**

1. **展示名还是噪声**：`穿搭信号塔` / `内容均由AI生成` / `用户信息区` —— 目前靠别名里的「我的」「灵感」才解析得动。§2 名称治理未做。
2. **没有并列入口标记**：Atlas 出来的 8 个节点 `entry` 全为 0、`tab_bar.entries/labels` 为空，于是 `is_tab_shell_state` 恒 False ——
   `pick_fsm_first_step` 的 `tab_shell_terminal` / `deep_page_direct_tab` 两条捷径和 `tab_root_entry_hint` 在真实数据上仍不触发（单测里能触发）。需要 Atlas 侧按底栏槽位把 Tab 根态标出来。
3. **`page.sk*` id 仍会随重新聚类换代**（`page.skd568c2708674s0` ↔ `page.skd568c2708674`）。第 3 条只是让它**报错而不是错配**；要根治得给骨骼 id 一个稳定键。
4. **`run_all.py` 按脚本跑，52 个 pytest 风格文件里的断言从不执行**（本次给 `test_fsm_nav_semantics.py` 补了 `main()`，其余未动）。

### 0.9 复盘：`cr-7566d0600a97::case-99ab0675` —— 目标口语被入口按钮别名吞回当前页（2026-09-17）

图已经连通（§0.8 重发后），localize 也认出了 `page.skd568c2708674`。6 次 `fsm_navigate(灵感/穿搭信号塔 → 开始造物拍照页)` 全部 `plan_ok=true`、**hops=0**、`step_pick=same_page_click_label`，再去点「开始造物拍照页」锚点落空。

| 现象 | 真因 |
|------|------|
| `resolved_from == resolved_to == page.skd568c2708674` | 入口页别名里有按钮文案「开始造物」；`_norm` 全局删「页」后「开始造物拍照页」⊃「开始造物」，平坦 0.92 包含分；拍照页 `page.sk3f5a31a92f9fs0` 同分但排在后面 |
| 图上其实有 `skd568… → sk3f5…fs0`（`selector_text=开始造物`） | 规划在 `plan_route` 前就当成「已在目标屏」 |
| 同页去 tap 整句口语 | `click_label_from_nav_ref` 把 ≤16 字的「…页」当控件文案 |

**已落地**（不写被测 App 文案）：

| 改动 | 文件 |
|------|------|
| `_norm` 只剥尾缀「页面/页」；包含分按长短比折价；剩余字被同页其它标签覆盖则加分 | `nav_state_resolve._norm` / `_similarity` / `_state_name_score` |
| 多页共享同一别名不再 `legacy_ref` 先到先得；同分时优先 leftover 覆盖，其次精确边 `to`（`name≥0.9`），再排除当前屏 | `nav_route.resolve_state_ref`、`resolve_state_fuzzy` |
| from/to 口语不同却解析到同一节点 → 排除当前屏再解析一次 | `plan_route_resolved._disambiguate_to_away_from_here` |
| 「…页/页面」不当可点文案 | `click_label_from_nav_ref` |

验收（本机 published 图）：`开始造物拍照页` → `page.sk3f5a31a92f9fs0`，`灵感 → 开始造物拍照页` 1 hop 走 `…_to_sk3f5a31a92f9fs0`。单测见 `test_nav_state_resolve.py` / `test_fsm_nav_semantics.py`。

### 0.10 复盘：`cr-d6d67b22eef9::case-c516fa0a` —— 去 Tab 页却点了「白色拍照按钮」（2026-09-17）

整案最后靠模型自己 `tap_element(我的)` pass，但 `fsm_navigate` 两轮都走歪了：

| turn | 当前 | 规划 | 本步实际 |
|------|------|------|----------|
| 1 | `page.sk3f5a31a92f9fs2` 商品展示区 | 2 hop 全是 BACK 链 | `press_key BACK`（图上说回到 fs5，真机落到了拍照页） |
| 2 | `page.sk3f5a31a92f9fs0` 拍照页 | `fs0 → fs1(白色拍照按钮) → BACK 到我的` | **去点白色拍照按钮**，锚点落空 declined |

图上其实有正确的 Tab 边 `skd568… → sk9f40…`（`target_page=我的`），底栏 `slots` 也有「灵感/我的」。但：

1. `tab_bar.entries/labels` 在剔 legacy 时被清空，`is_tab_shell_state` 恒 False，`pick_fsm_first_step` 的 Tab 捷径永不触发。
2. `tab_label_for_state` 对每个 `page.sk*` 都返回 `display_name`，于是「商品展示区」「确保物品完整入镜」都被当成 Tab 文案。
3. 最短路把「去我的」编成内容区边（拍照按钮、列表项）。规划成功就不走「无底栏则 BACK」的兜底。

**已落地**：用 `tab_bar.slots` 精确对齐别名来认 Tab 根；底栏**可见**时去 Tab 页本步直点槽位；**不可见**且本步将要点的不是该 Tab 文案时，改系统返回，禁止跟内容边。

### 0.11 复盘：`cr-4aedc34425b3::case-99ab0675` —— 拍完之后空等 26 轮（2026-09-17）

本轮 **没有调用 `fsm_navigate`**。步骤 1 用 `tap_element(开始造物)` 进了拍照页并 `assert_visual` 通过；步骤 2 点了白色快门后，模型从 turn 5 到 turn 30 **只 `wait_ms`**，直到「超过 30 步仍未完成」。

| 现象 | 真因 |
|------|------|
| `observe/hierarchy` `ok=true`、`text_len=0` | Flutter/无障碍不吐 `text`；节点还在，不是通道关掉 |
| localize `fs5` 置信 **2%**、`band=recover` | 结构分太弱仍选出一个页名；assist 写成「当前屏：管理」+「勿乱点」 |
| 同案历史 pass（`cr-8b961f17bb71`）也在快门后 wait | 生成需要等，但那次 8 次 wait 后 `signal_done` → 校验；这次 26 次从不收工 |
| `wait_ms` 不进 `ProgressGate` | `_FUSE_CAPS` 不含等待，同屏空等不计里程碑、不告警、不熔断 |

同案更早的 pass 里，生成结果页也曾被 localize 成 `fs5`（展示名「管理」），但置信够高、assist 没把模型冻住，最后 `assert_visual` 看到了达成信号。

**已落地**（不写被测 App 文案）：

| 改动 | 文件 |
|------|------|
| `wait_ms` 计入里程碑（do=10）；同屏空等不走困局/无进展，以免把合法加载等死 | `loop/action_fuse.py` |
| 第 4 次 wait 起注入「达成信号已在屏上则 signal_done」；第 10 次熔断并禁止继续空等 | 同上 |
| recover 档 assist 不再「勿乱点 / 先问人」，页名改成「最像…仅供参考」 | `nav_compiler._head` |
| recover 档业务流一律「参考意见」，不催「进入下一屏」 | `nav_flow_blocks.build_flow_context` |
| agent-decide **v14** 同步改系统提示里那句「勿乱点」 | `job_upgrades` / bootstrap |

验收：`tests/test_action_fuse.py`（10 次 wait 才里程碑）、`tests/test_nav_compiler.py`（低置信无「勿乱点」）。**需重启 Nexus** 加载代码并跑 bootstrap 升 v14。

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
