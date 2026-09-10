# UI 导航图 + LLM Wiki — 方案（v3.0）

> **v3.0**：**程序作用域键**（`project_id` / `app_id` / …）、**证据落 `data_dir()`**（与 `mino.db` 同根）、配置直接落库。  
> **状态：方案设计完成，暂不落代码**。设计文档仅此文件；校准证据不进 git，见 §0。

---

## 0. 程序作用域与键（实现必须对齐现有模型）

NavFSM 是 **按被测应用** 配置的子系统，键名与 `projects` / `apps` / `project_cases` / `runs` / `RunContext` 一致，**不引入**备份、试点、仓库路径等业务自创字段。

| 键 | 来源（本仓已有） | 用途 |
|----|------------------|------|
| **`project_id`** | `apps.project_id`；跑批时由 `resolve_knowledge_scope(ctx)` 同逻辑解析 | 列表/权限/与 `project_cases`、`knowledge_entries` 同 scope |
| **`app_id`** | `apps.id`；`RunContext.app_id`；`runs.app_id` | **`nav_fsm` 主键**；`nav_fsm_store.load(app_id)` |
| **`case_id`** | `project_cases.case_id` | 用例级 `nav_anchor`（`case` JSON 可选块）；walkthrough 记录追溯 |
| **`run_id`** | `runs.run_id` / `session_id` 前缀 | 校准 walkthrough、`nav_telemetry` 关联 |
| **`account_id`** | `ctx.picked_account` / `resource_lease` 租号 | 与 `meta.hierarchy_calibration.account_id` 绑定；跑批前校验 |
| **`target_package`** | `RunContext.target_package` / `apps.env` | hierarchy 里 `{package}:id/...` 的包名前缀 |
| **`run_type`** | `runs.run_type`（`manual` / `copilot` / 未来 `batch`） | GuardGate stop → `ask_human` vs `give_up` |

**被测 App 内容**（`state_id`、`text_landmarks`、`guards.detect`、Tab 文案）存在 **`nav_fsm*` 表**，按 `app_id` 隔离；**代码中不得硬编码**任何具体 App 文案或包名。

**三类存储归宿**（勿与 git 仓库目录混用）：

| 类型 | 归宿 |
|------|------|
| 运行时配置 | `mino.db` → `nav_fsm` / `nav_fsm_states` / `nav_fsm_edges` |
| 校准证据（hierarchy 片段、walkthrough 记录） | **`data_dir()/nav/calibration/{app_id}/{calibration_id}/`** |
| 方案 / 协议 | **`docs/NAVIGATION_ATLAS.md`**（本文件）、**`docs/PROTOCOL.md`** |

`data_dir()` 定义见 `mino_nexus/core/paths.py`（默认 `~/.mino-nexus`，与 `mino.db` 同级）。

---

## 0.1 设计原则


| 原则                         | 含义                                              |
| -------------------------- | ----------------------------------------------- |
| Localize 先于 Pathfind       | 先规则信号 + 置信度；VLM 仅低置信可选补充                        |
| 边是可执行契约                    | wait / scroll / retry / effect_assert / on_fail |
| **state_id 稳定，facets 运行时** | 不为每个列表项建节点                                      |
| 规划是条件转移                    | 在线选边 + `success_rate` 权重                        |
| guards 单一真源                | 不另建 Rule Cards 文件                               |
| NavFSM 配置落库                 | states/edges/guards 进 `nav_fsm*` 表；按 `app_id` + `project_id`  scope |
| 图谱不自动膨胀                   | 不开 session→`atlas_patch` 自动合并（与配置落库无关）                    |
| RouteAssist 有允许动作集         | 限制 LLM 偏离，不只事后熔断                                |
| 可评估才可迭代                    | 指标 + 人工抽检 localize                              |


---

## 0.2 存储：表结构、磁盘布局、API

### 0.2.1 表结构

```sql
CREATE TABLE nav_fsm (
  id              INTEGER PRIMARY KEY,
  app_id          TEXT NOT NULL,
  project_id      TEXT NOT NULL,       -- 冗余自 apps.project_id，便于按项目查询
  version         TEXT NOT NULL DEFAULT 'v1',
  meta            JSON NOT NULL,       -- hierarchy_calibration、guard_catalog 等
  test_data       JSON NOT NULL DEFAULT '{}',  -- lease_tags 等，与 Console 号池对齐
  updated_by      TEXT,
  updated_at      TIMESTAMP,
  UNIQUE(app_id, version)
);

CREATE TABLE nav_fsm_states (
  id            INTEGER PRIMARY KEY,
  fsm_id        INTEGER NOT NULL REFERENCES nav_fsm(id),
  state_id      TEXT NOT NULL,
  kind          TEXT NOT NULL,      -- page / dialog / state
  identify      JSON NOT NULL,
  guards        JSON NOT NULL,
  wiki_ref      TEXT,
  UNIQUE(fsm_id, state_id)
);

CREATE TABLE nav_fsm_edges (
  id            INTEGER PRIMARY KEY,
  fsm_id        INTEGER NOT NULL REFERENCES nav_fsm(id),
  edge_id       TEXT NOT NULL,
  kind          TEXT NOT NULL,      -- nav / recover
  from_state    TEXT NOT NULL,
  to_state      TEXT NOT NULL,
  guard         JSON,
  execute       JSON,
  effect_assert JSON,
  on_fail       JSON,
  UNIQUE(fsm_id, edge_id)
);
```

实现落点：`models/nav_fsm*.py`、`core/migration.py`、`services/nav_fsm_store.py`、`services/nav_calibration_store.py`（读写在 `data_dir()`）。

`meta.hierarchy_calibration` 示例：

```json
{
  "calibration_id": "20260910T120000Z",
  "evidence_rel_path": "nav/calibration/{app_id}/{calibration_id}",
  "account_id": "<picked_account.id>",
  "project_id": "<apps.project_id>",
  "hierarchy_format": "flat_text",
  "hierarchy_result_key": "hierarchy_text",
  "follow_filled_detectable": "id",
  "guard_recheck_on_anchor_hit": true
}
```

`success_rate` / `p95_ms` 放 telemetry，不放 `nav_fsm` 表。

### 0.2.2 校准证据磁盘布局（`data_dir()`，非 git）

路径助手（实现建议）：

```python
# mino_nexus/core/paths.py 或 services/nav_calibration_store.py
def nav_calibration_dir(app_id: str, calibration_id: str) -> Path:
    return data_dir() / "nav" / "calibration" / app_id / calibration_id
```

```
{data_dir}/nav/calibration/{app_id}/{calibration_id}/
  manifest.json       # project_id, app_id, account_id, hierarchy_format, ...
  walkthrough.json      # 有序步骤：run_id, case_id, turn_id, step_key
  hierarchy/            # 每步子树片段
    step_001.txt
```

Studio / API 通过 `GET /api/nav-fsm/{app_id}/calibration/{calibration_id}` 读磁盘（或 manifest 摘要进 DB JSON 指针）。**不把** `docs/` 仓库路径写进 `meta`。

### 0.2.3 运行时读写

| 角色 | 方式 |
|------|------|
| **Nexus 循环** | `nav_fsm_store.load(app_id)`；`project_id` 校验与 `apps` 一致 |
| **Studio / Console** | `GET/PUT /api/nav-fsm/{app_id}` |
| **校准导入** | walkthrough 完成 → 写 `data_dir` 证据 + `PUT` 更新 `nav_fsm*` |
| **校验** | `validate_nav_fsm`：无 `__CALIBRATE__`、边引用、`project_id`/`app_id` 必填 |

配置与证据均在 **部署机的 `data_dir()`**；与 `mino.db` 备份策略一致（运维备份整个数据目录，而非 git）。

---

## 1. 三层分工

```
NavFSM     → 在满足守卫下，下一步转移是什么（+ 允许动作集）
Wiki       → 为什么、注意什么（knowledge_entries，wiki_ref 绑定）
guards     → State.guards 投影进 prompt + GuardGate 计数
ProgressGate → 无进展 / 熔断空转（与 GuardGate 联动）
```

---



## 2. Localize



### 2.1 输出形状

```json
{
  "candidates": [{ "state_id": "page.feed.list", "confidence": 0.82, "signals": { "session": 1.0, "tab_bar": 0.9, "text": 0.7 } }],
  "chosen": "page.feed.list",
  "confidence": 0.82,
  "ambiguous": false
}
```



### 2.2 主路径（默认不用 VLM 做每步 localize）


| 信号               | 来源（本仓已有）                           | 默认权重 |
| ---------------- | ---------------------------------- | ----- |
| `session`        | `inspect-session` / `session_gate` | 高     |
| `tab_bar`        | Scout `hierarchy` 结构               | 高     |
| `text_landmarks` | hierarchy 文本 + `none_of`           | 中     |
| `history`        | 上一条边 `effect_assert` 是否通过          | 中     |
| `pHash_delta`    | pillow 感知哈希（**仅进展分**）              | 低     |
| `vlm_*`          | **接口预留，默认不调用**                     | 见下    |


**VLM 克制**

- **不**在每步 localize 调 VLM；延迟/成本/误判拖慢迭代。
- 仅当 `confidence < 0.45` 且 hierarchy 为空或冲突时，**可选**触发：
  - 异步 job（不阻塞本 turn 决策 → 下 turn 用缓存结果），或
  - 专用轻量 job：`nav/widget_state`（只判 follow 空心/实心），**非**整屏分类。
- 主链稳定后，再逐步提高 VLM 权重；telemetry 记录 `vlm_invoked` 占比。

**三档决策**

- `≥ 0.75`：推荐边 + 允许动作集
- `0.45 – 0.75`：探索（仅 recover + 允许动作集内探索 cap）
- `< 0.45`：恢复边优先（`go_home` / `close_dialog` / `press_back`）



### 2.3 恢复边（全局）

`recover.press_back` · `recover.close_dialog` · `recover.go_home` · `recover.restart_target_app`（已有）· `recover.dismiss_overlay`（advise）

---



## 3. NavFSM：状态与合并策略



### 3.1 state_id vs 运行时 facets（防爆炸）


| 进入 `state_id`                     | 仅运行时 facets（不进 id）           |
| --------------------------------- | ---------------------------- |
| 页面模板：`page.feed.list`              | `scroll_bucket`、`data`、列表项内容 |
| 弹窗模板：`dialog.unfollow_confirm`    | 弹窗文案变体                       |
| 会话：`logged_in` 作 guard，不作独立 state | `role`、`experiments`         |


**列表项**：不为「已关注项 1/2/3」建节点。用 **目标锚点**（用例 / 租号 / 知识库）：`anchor.author_name`、`anchor.post_title`，边 guard 匹配：

```json
{
  "from": "page.feed.list",
  "guard": {
    "widget.follow_button": "filled",
    "anchor": { "author_name": "{{case.nav_anchor.author_name}}" }
  }
}
```



### 3.2 状态示例（列表页）

```json
{
  "id": "page.feed.list",
  "identify": {
    "required": [
      { "signal": "tab_bar", "match": { "selected": "<tab_label>" } },
      { "signal": "text_landmarks", "any": ["<tab_label>"], "none_of": ["<profile_landmark>"] }
    ]
  },
  "guards": {
    "widget.follow_button": {
      "states": {
        "outline": { "tap_allowed": ["tap_follow"] },
        "filled": {
          "tap_forbidden": [{ "match": { "selector_text": "<follow_label>" }, "reason": "列表已关注不可取消" }],
          "tap_recommended": ["tap_author_avatar"],
          "wiki_ref": "knowledge:<entry_id>"
        }
      },
      "detect": { "priority": ["hierarchy_attr"], "vlm_fallback": "nav/widget_state" }
    }
  }
}
```



### 3.3 图完整性：列表 guard 不足以覆盖「进个人页再操作」类任务

仅配置列表页 guard，**无法**覆盖「列表已关注 → 须进个人页再取消」类流程。典型需补全的 **屏类型**（`state_id` 由该 `app_id` 自定）：


| state_id（示例） | 说明 |
|------------------|------|
| `page.home` | 主导航根 |
| `page.feed.list` | 信息流 / 列表 Tab |
| `page.profile.other` | 他人个人页 |
| `dialog.confirm_action` | 二次确认弹窗 |
| `page.profile.other_idle` | 操作完成后的个人页态 |



| edge_id（示例） | from → to |
|-----------------|-----------|
| `edge.home_to_feed` | home → 列表 Tab |
| `edge.open_profile` | 列表 → 个人页（**锚点** `case.nav_anchor`） |
| `edge.profile_primary_action` | 个人页 → 弹窗 |
| `edge.confirm_dialog` | 弹窗 → 后续屏 |
| `recover.*` | 全局恢复边 |


`page.profile.other` guards：

```json
"guards": {
  "widget.unfollow_button": {
    "states": {
      "visible": { "tap_allowed": ["tap_unfollow"], "scroll_maybe": true },
      "hidden": { "tap_recommended": ["swipe_up"], "wiki_ref": "knowledge:profile-scroll" }
    }
  }
}
```

`edge.profile_unfollow.effect_assert`：`dialog.unfollow_confirm` 的 `text_landmarks` 含「确定」/「取消关注」。

---



## 4. 边：可执行契约

定位优先级写入 Scout `EXECUTE` 载荷（Nexus 不直连设备）：

`resource_id` → `accessibility_id` → `selector_text`+父约束 → `coords_milli`（兜底）

每条边含：`execute.steps`、`wait_for`、`scroll_into_view`、`retry`、`effect_assert`（**强断言**，见 §7）、`on_fail`。

---



## 5. RouteAssist + 允许动作集

默认 **只**做 RouteAssist：每轮注入一段，**不**灌整条路径。

```
【导航 assist】loc=page.feed.list (0.82)
【建议边】edge.home_to_feed → tap Tab「<tab_label>」

【本步允许】
- 执行建议边：tap_element selector=<tab_label>
- recover.close_dialog / recover.go_home / recover.press_back
- signal_ask_human（仅 `run_type` ∈ `manual` / `copilot`，见 §11.5）
- signal_give_up

【禁止】
- tap selector=关注 当 widget.follow_button=filled
- 任意未列出的 cap（guard 将 block）

【守卫】列表已关注：请走个人页路径（edge.open_profile）
```

**菜单裁剪**：首期不做；仅在 prompt 注入【本步允许/禁止】。后续可由 `nav_compiler` 过滤 `available_menu_brief`。

偏离处理：

1. 第 1 次非法 cap → **GuardGate steer**（写入 history，不计无进展）
2. 第 2 次同一 `guard_id` → **block**
3. 第 3 次 → **stop** → `give_up`（批跑）

与 `ProgressGate` 独立计数，见 §8。

---



## 6. Wiki（本仓实现，不引入外部 llm-wiki 运行时）


| 能力    | 本仓真源                                              |
| ----- | ------------------------------------------------- |
| 条目存储  | `knowledge_entries`（`knowledge_store`）            |
| 情境分面  | `knowledge_situation`（facet / need / screen_role） |
| 执行检索  | `knowledge_match` + `match_step_knowledge`        |
| 路径类识别 | `is_path_item()` in `knowledge_hint.py`           |


**做法**：不部署独立 [llm-wiki](https://github.com/) 服务；采纳其 **wiki_ref 链接 + 编译式摘要** 思路：

- FSM 上 `wiki_ref: "knowledge:<entry_id>"`（与 `knowledge_entries` + `project_id`/`app_id` scope 一致）
- 注入时按 `knowledge_situation.need` **分档截断**（`nav_compiler` 读条目元数据，不一刀切）：
  - `howto`：≤ **800** 字（路径类，如个人页取消关注 + 确认弹窗）
  - `judge`：≤ **300** 字（断言/判别提示）
  - `exception`：≤ **500** 字（异常/恢复说明）
  - 默认：≤ **400** 字
- 超长部分链到 Console 知识详情，prompt 只注入摘要
- `propose_atlas` / QA 流程可提议新 knowledge 条目，人审后挂到 guard

每个 `app_id` 在 `knowledge_entries` 中配置与 guards 绑定的 Wiki（按 `project_id` / `app_id` scope），例如：列表 assert 参考、列表已关注须进个人页、个人页二次确认流程。

---



## 7. 数据回流（图谱不自动膨胀）

**NavFSM 配置已在 DB**（§0）；本节指 **导航图谱候选边** 不从失败 session 自动合并。

**循环依赖**：`effect_assert` 依赖 localize → localize 不准则无法自动判成功 → **不开 `atlas_patch` 自动入库**。

边 `effect_assert` 使用 **强断言**（不依赖 localize 高置信）：

- `resource_id` / `accessibility_id` 在 hierarchy 中出现
- `text_landmarks`：校准时实测的 Tab / 按钮 / 弹窗文案
- `effect_assert.state_delta`：如 `dialog` 从 null → `unfollow_confirm`

人工：QA 在 Console 标注 10–20 条 localize 抽检；session `nav_telemetry` 导出 CSV。

后续再开候选管道（confidence + 回归 + `atlas_patches` 回滚）。

---



## 8. 双熔断：ProgressGate + GuardGate



### 8.1 ProgressGate（已有 `loop/action_fuse.py`）

无进展 / 状态困局 / 状态循环 / 里程碑 / fuse block → stop。

### 8.2 GuardGate（独立计数）


| 次数  | 同一 `guard_id` 违反 | 行为                                          |
| --- | ---------------- | ------------------------------------------- |
| 1   | steer            | 注入【禁止】+ history；**不计** `no_progress_streak` |
| 2   | block            | skip cap                                    |
| 3   | stop             | `give_up`（`run_type` 无人值守）或 `ask_human`（`manual`/`copilot`，§11.5） |


`guard_id` 示例：`list.follow_filled_no_tap`。

与 ProgressGate：**并行计数**；任一先到 stop 即终止。Guard 违反 **不** 用低置信 VLM 触发，仅 hierarchy + 规则。

**硬约束**：`detect.strength` 决定能否进入 block/stop 计数（§8.3 三档）；未命中检测 → **只 steer + Wiki**，不 hard block（否则 `guard_fp` 超标）。

**按 strength 的 GuardGate 行为**（`guard_gate.py` 读 DB 加载的 guards，不硬编码）：

| `strength` | 含义 | 与标准阶梯的关系 |
|------------|------|------------------|
| `strong_id` | 稳定 `resource_id` / `content-desc` 命中 | **直接**走标准阶梯：违反 1 → steer，2 → block，3 → stop |
| `strong_text` | 仅 hierarchy 文案命中（如「已关注」） | 先过 **连续命中门槛**，再映射到同一阶梯（见下） |
| `weak` | 反证 / 隐藏态 | **永不** block/stop；仅 steer |
| `medium` | outline 等推荐态 | 仅推荐 cap；**禁止**用于 block |

**`strong_text` 与标准阶梯的统一规则**（避免「第 2 turn 是 steer 还是 block」歧义）：

| 事件 | `strong_text_consecutive_hits`（每 `guard_id` 独立） | 标准阶梯位 |
|------|------------------------------------------------------|------------|
| 本 turn **命中** detect，LLM **违反** guard | +1 | 连续命中 **1** → **steer**（阶梯第 1 档） |
| 连续命中 **2** 且仍违反 | — | **block**（阶梯第 2 档） |
| 连续命中 **3** 且仍违反 | — | **stop**（阶梯第 3 档） |
| 本 turn **未命中** detect 或 hierarchy 为空 | **归零** | 下次命中从 steer 重新计 |

`strong_id` **无**连续命中门槛；`strong_text` 只是在进入 block/stop 前多待 1 turn，阶梯档位名称与 §8.2 表一致。

---



### 8.3 守卫检测信号表示例（写入 `nav_fsm_states.guards`）

> 列表「已关注不可在列表取消」类 guard 的核心：`widget.follow_button.filled`。检测规则 **必须在 DB 配置里写死**（由 `data_dir` 校准证据录入后 Studio/PUT 写入），`guard_gate.py` 只读 store。

信号来源：Scout 回传的 **hierarchy 文本 / 节点属性**（`observe("hierarchy")` 或 screenshot 附带 hierarchy）。实现前需在真机采 1 份 hierarchy 样本，核对下表 resource-id 是否成立；**不成立则改 DB 配置（对照证据重录），不改代码猜**。


| guard 状态                  | `guard_id`                 | 检测信号（`match_any` 满足其一）                                                                                   | `strength`（DB） | 不满足时 GuardGate 行为                                              |
| ------------------------- | -------------------------- | ----------------------------------------------------------------------------------------------------------- | ------------------- | -------------------------------------------------------------- |
| `follow_button.filled`    | `list.follow_filled`       | `resource-id` / `content-desc` 匹配实测 id（校准表）                                                                  | `strong_id`         | block/stop 按 §8.2；未命中 → steer only                            |
| `follow_button.filled`    | `list.follow_filled`       | hierarchy 含 **「已关注」**（无稳定 id 时）                                                                             | `strong_text`       | 连续命中 1→steer，2→block，3→stop（§8.2）；断连归零               |
| `follow_button.outline`   | `list.follow_outline`      | 含 **「关注」** 且 **不含**「已关注」；或 `resource-id` 匹配 `*follow_btn`*                                                | `medium`            | 仅 **推荐** `tap_follow`；**禁止**用于 block                          |
| `unfollow_button.visible` | `profile.unfollow_visible` | 个人页含 **「取消关注」** 或实测 `*unfollow*` id                                                                        | `strong_id` 或 `strong_text` | 强命中 → 允许 `tap_unfollow`                                     |
| `unfollow_button.hidden`  | `profile.unfollow_hidden`  | 在个人页 state 下 **未**命中 `unfollow_visible`                                                                     | `weak`              | 仅 steer「向上滑动」                                                 |
| `dialog.unfollow_confirm` | `dialog.unfollow_confirm`  | 弹窗文案 + 容器特征（校准表）                                                                                          | `strong_id` 或 `strong_text` | 用于 `effect_assert`；允许 `tap 确定`                              |


**`guards.detect` 字段形状**（写入 `nav_fsm_states.guards` JSON）：

```json
"detect": {
  "strength": "strong_id | strong_text | medium | weak",
  "match_any": [
    { "hierarchy_contains": "已关注" },
    { "resource_id_regex": ".*followed.*" }
  ],
  "match_none": ["未关注"],
  "on_miss": "steer_only | allow | deny_block"
}
```

`strong_text` 连续命中计数由 `guard_gate` 运行时维护（`strong_text_consecutive_hits`），**不写进 DB**。

**`follow_filled_detectable` 三态**（`meta.hierarchy_calibration`，由校准表决定，非简单 true/false）：

| 校准结果 | `follow_filled_detectable` | filled `strength` | 验收 §12#1 |
|----------|---------------------------|-------------------|---------------|
| 有稳定 id | `id` | `strong_id` | 考核 block/stop |
| 仅有文案 | `text` | `strong_text` | 考核 block（连续命中规则） |
| 皆无 | `false` | —（全局 steer_only） | 降级为「不误点 + 个人页链」 |

**若 App hierarchy 无「已关注」等态属性**：`follow_filled_detectable: false`；GuardGate **全局降级**为 steer_only。

### 8.4 真机 hierarchy 校准（§10 步骤 0.5，**有序 walkthrough**）

> **必须先完成 §10 步骤 0.4（协议）与 0.6（loop 通道）**：校准用的 hierarchy **必须与跑批同一通道、同一 RESULT key、同一格式**。  
> **0.5 不是「采 5 份独立样本」**，而是一次有人值守的 **walkthrough**：步骤有顺序，中途改账号状态；跳步或乱序会导致后续屏采不到（例如先采弹窗会把账号变成未关注，样本 3「已关注个人页」永远消失）。

#### 8.4.0 前置条件

- 租号处于 **「已关注目标作者」初态**（`nav_anchor` + §11.4 `account_id`）；否则从步骤 1 重头来。
- 每一步：截 hierarchy **子树** + 记录 §8.4.2 六字段 + 记 `session_id` / `turn_id`。
- **任一步失败 → 整段 walkthrough 重跑**（恢复租号初态后再采），不得跳步补采。

#### 8.4.1 Walkthrough 顺序（按该 `app_id` 的图定义，**有状态依赖的步骤必须有序**）

产物落在 `{data_dir}/nav/calibration/{app_id}/{calibration_id}/`：

1. `manifest.json` — `project_id`、`app_id`、`account_id`、`hierarchy_format`、各 `guards.detect` 结论
2. `walkthrough.json` — 有序步骤：`step_key`、`run_id`、`case_id`、`turn_id`
3. `hierarchy/step_*.txt` — 每步子树片段

**示例**（列表关注态 + 个人页取消类 App；其他 App 自定步骤，但须满足「改状态的操作放在后面」）：

| 步 | 屏态（示例） | 用途 |
|----|--------------|------|
| W1 | 列表 · 目标 guard 态 A | 录 `guards.detect` 强信号 |
| W2 | 列表 · 对照态 B | 录 outline / 反例 |
| W3+ | 后续屏（个人页、弹窗…） | 录边 `effect_assert` 依赖的 landmark |

**W1 额外**：确认 anchor 是否首屏 → `anchor_on_first_screen` / `scroll_into_view`（§10.4）。

**W2 说明**：未关注行可与 W1 同屏相邻行，**不必**改目标作者关注态；勿在 W3 之前对目标作者取消关注。

采集方式（**与步骤 0.6 一致**，禁止旁路）：

- `agent_loop` 内 `RouterProxy.observe("hierarchy")` 写入 `inspect_slots["hierarchy_text"]` 后，从 session log / telemetry 截取
- 同一 `observe("hierarchy")` 路径的 **`run_type=manual` walkthrough run**（§11.5）

**账号**：walkthrough 必须用 **跑批将使用的同一 `lease_account` / `account_id`**（§11.4）。开发机个人账号 **不得**录入 `nav_fsm` 配置。



#### 8.4.2 每份样本记录 6 个字段

写入 `{data_dir}/nav/calibration/{app_id}/{calibration_id}/manifest.json`（与 DB 配置对照，证据不进库）：


| 字段                  | 说明            | 示例                                    |
| ------------------- | ------------- | ------------------------------------- |
| `account_id`        | **`picked_account.id`**（与 `meta.hierarchy_calibration.account_id` 一致） | `<lease_account_id>` |
| `walkthrough_step` | W1–W5 | `W3` |
| `run_id` / `case_id` / `turn_id` | 来源跑批 | `cr-xxxx` / `case-xxxx` / `12` |
| 目标节点路径              | 根 → 目标        | `.../RecyclerView/ItemView[2]/Button` |
| `resource_id`       | 完整 id（**被测 App 包名**，校准时实测） | `{package}:id/btn_followed`           |
| `content-desc`      | 无障碍描述         | `已关注`                                 |
| `text`              | 可见文本          | `已关注`                                 |
| 容器特征                | 列表行 / 弹窗容器 id | `{package}:id/dialog_container`       |


另记 `hierarchy_format`：`flat_text` | `xml_dump` | `accessibility_json`（**须与** `docs/PROTOCOL.md` **§4.4.2 一致**）。若 Scout 短期无法结构化导出，首期 **只支持 `flat_text`**，`detect.match_any` 仅用 `hierarchy_contains`。

#### 8.4.3 校准判定规则


| 样本 1 结果                             | `follow_filled_detectable` | DB `guards.detect`                                     | GuardGate                      |
| ----------------------------------- | -------------------------- | ------------------------------------------------------ | ------------------------------ |
| **同时有**「已关注」文本 + 稳定 `*followed`* id | `id`                       | `strength: strong_id`；`resource_id_regex` + 可选 `hierarchy_contains` | 标准 block/stop                  |
| **仅有**文本、无稳定 id                     | `text`                     | `strength: strong_text`；只写 `hierarchy_contains` + `match_none` | 连续 2 turn 命中后 block           |
| **两者皆无**（Flutter/Canvas 自绘）         | `false`                    | 无 filled detect；`on_miss: steer_only`                  | **全局 steer_only**；§12#1 降级    |
| 弹窗为**独立 window**                    | —                          | `dialog.unfollow_confirm` 加 `window_type` / `layer` 条件 | —                              |
| 弹窗为**应用内 View**                     | —                          | 用容器 `resource_id` + 文案组合                               | —                              |


校准完成后，把结论一次性写入 `meta.hierarchy_calibration`（含 `account_id`、`samples[]` 摘要、`dialog_layer`、`anchor_on_first_screen`、`hierarchy_result_key`）。

**跑批前校验**：`ctx.picked_account` 的 id == `meta.hierarchy_calibration.account_id`；不一致 → **`nav_fsm_store.load` 拒绝**，须重新校准（§11.4）。

### 8.5 配置录入：可先写 DB vs 必须等校准

| 可先 `PUT` 进 DB（与 hierarchy 无关） | 必须等 walkthrough 后录入（禁止 `__CALIBRATE__`） |
|--------------------------------------|--------------------------------------------------|
| `app_id` / `version` / `test_data` 骨架 | `guards.*.detect.match_any` 内 `resource_id_regex` |
| 5 个 `state_id` + `identify` 骨架 | `guards.*.detect.strength` |
| 边的 `from` / `to`、`guard` 逻辑、`effect_assert` **结构** | `effect_assert.require_any` 实测 id |
| `recover` 边模板 | `meta.hierarchy_calibration.*` 实测字段 |
| `scroll_into_view` 字段存在 | 需滚动的边的 `scroll_into_view` 参数 |

**做法**：可先 migration + 空壳行；walkthrough 后 **一次性 seed / Studio 录入** 实测值。`validate_nav_fsm` 在 **load 与 PUT** 时拒绝残留 `__CALIBRATE__`。

---



## 9. 评估指标


| 指标 | 建议目标 |
|------|----------|
| `localize_acc`（人工抽检） | ≥ 90% 骨干屏 |
| `edge_success_rate`（强断言） | 关键边 ≥ 85% |
| `guard_fp` / `guard_fn` | < 10% / < 5%（见下） |
| `path_reach_rate` | 按业务定目标屏 |
| `task_success`（端到端） | pass 或明确 blocked 原因 |
| `vlm_invoked_rate` | 记录基线 |

**guard_fp**（误拦）：人工抽检 telemetry 中 `nav/guard_hit` 且 `action=block` 的 turn，标是否误拦（30 次）。

**guard_fn**（漏拦）：本该 block 却没 block。首期 **不做自动判定**；telemetry 记录候选，人工抽检：

```json
{
  "event": "nav/guard_miss_candidate",
  "guard_id": "list.follow_filled",
  "filled_detected_by_human": true,
  "llm_tapped_follow": true,
  "hierarchy_snippet": "..."
}
```

QA 对 `nav/guard_miss_candidate` 事件标 `confirmed_fn: true/false`，再算 guard_fn。完整 key 见 §10.6。

---



## 10. 实施顺序

> **原则**：**阻塞项先通** → 校准证据落 `data_dir()` → **DB migration + 配置录入** → 运行时模块。  
> **执行顺序**：**0.4 → 0.6 → 0.5 → 1 → 2–8**。

### 10.0 阻塞项（运行时模块的入场条件）

未完成以下两项，**禁止**录入 NavFSM 实测配置 / 写 `guard_gate` 业务逻辑——否则 guard / localize / `effect_assert` 全是空中楼阁。

#### 10.0.1 步骤 0.4 — 协议对齐（Scout + `docs/PROTOCOL.md`）

**Nexus owner（单点）**：开仓内 issue `nav/hierarchy-protocol`，由 **`loop/router_proxy` + `agent_loop` 接入 PR 的 assignee** 担任；负责催 Scout 对齐、在截止日触发 fallback、把决议写入 PROTOCOL。

| 项 | 责任 | 交付 |
|----|------|------|
| hierarchy 在 `RESULT` 中的 **key** | Scout 实现 + Nexus owner 确认 | 写入 `docs/PROTOCOL.md` §4.4.2（两仓同步） |
| **格式** | 同上 | `flat_text` / `xml_dump` / `accessibility_json` 选一 |
| **决策窗口** | Nexus owner | **3 个工作日**（从 issue 开工日计） |

**硬规则 — 逾期未对齐的 fallback**（Nexus owner 在 issue 到期日 **主动触发**，不等 Scout「再等等」）：

1. 按默认开工：`hierarchy_format: flat_text`；`detect.match_any` **仅** `hierarchy_contains`（substring）。
2. `resource_id_regex` / `node_query` 推到后续版本；不阻塞首期主链。
3. 在 `docs/PROTOCOL.md` §4.4.2 记 **provisional** 约定；Scout 后续对齐时走协议同步 PR。

**不要**只在 `NAVIGATION_ATLAS.md` 里记口头约定；协议（含 provisional）是真源。

#### 10.0.2 步骤 0.6 — hierarchy 通道打通（`agent_loop`）

**Feature flag（回滚开关，0.6 第一提交物之一）**：

| 配置 | 默认 | 说明 |
|------|------|------|
| `MINO_OBSERVE_HIERARCHY=1` 或 `playbook.observe_hierarchy` | `false` | 启用后每 turn 拉 hierarchy |
| 读入点 | `agent_loop` turn 开头 | 出问题一键关 flag，无需回滚核心循环逻辑 |

| 改动点 | 说明 |
|--------|------|
| 每 turn | `screenshot` 成功后 **同逻辑 turn** 调 `observe("hierarchy")`（多一次 RPC，接受延迟） |
| 写入 | `inspect_slots["hierarchy_text"]`、`hierarchy_turn_id`、`hierarchy_stale` |
| 绑定 | `hierarchy_turn_id == screenshot_turn_id` 时两信号同属一轮 |

**失败 / 漂移语义**（实现必须遵守）：

| 场景 | 行为 |
|------|------|
| screenshot **与** hierarchy **都成功**，`turn_id` 一致 | 写入 `inspect_slots`；供 localize / guard / `effect_assert` |
| hierarchy **失败**（超时 / Scout error） | **本 turn 继续**（不 fail turn）；`hierarchy_text = ""`；`localize` 降级为 session + history；**guard_gate 本 turn 不 block**（无输入不误判） |
| screenshot **失败** | 走现有失败路径，**不**特殊处理 hierarchy |
| `hierarchy_turn_id != screenshot_turn_id` | `hierarchy_stale: true`；**本 turn 不评估** `effect_assert`；guard 按「无 hierarchy」处理 |

**校准（0.5）必须走此通道**（`observe_hierarchy=true`），不得用 Scout 旁路导出。

依赖关系：

```
0.4 协议 key/format
    ↓
0.6 agent_loop 每 turn hierarchy  ← 阻塞 nav_fsm / guard_gate / localize
    ↓
0.5 walkthrough（证据文件）
    ↓
1 migration + seed → nav_fsm* 表
    ↓
2–5 NavFSM 运行时模块
```

| 步骤      | 交付物                            | 说明                                                                                                   |
| ------- | ------------------------------ | ---------------------------------------------------------------------------------------------------- |
| **0**   | 测试数据规范（§11）                    | `project_id` / `app_id` / `lease_account` / `case.nav_anchor` 与校准 manifest 一致                               |
| **0.4** | **协议对齐**（阻塞）                   | Scout 确认 RESULT key/format → `docs/PROTOCOL.md` §4.4.2；默认 `flat_text`                                |
| **0.6** | **hierarchy 通道**（阻塞）           | `agent_loop` 每 turn `observe("hierarchy")` → `inspect_slots["hierarchy_text"]`                      |
| **0.5** | **真机 walkthrough**（§8.4）       | W1–W5 → `{data_dir}/nav/calibration/{app_id}/{calibration_id}/manifest.json` + `walkthrough.json`                               |
| **1**   | **DB + 配置录入**                  | migration；`nav_fsm_store` + `validate_nav_fsm`；seed 或 Studio **直接写库**（§0.2）                         |
| **2**   | `nav_fsm.py`                   | `load(app_id)` 从 DB；match guard；推荐边；允许动作集                                                      |
| **3**   | `nav_localize.py`              | 读 `hierarchy_text`：session + tab_bar + text_landmarks + history                                      |
| **4**   | `nav_compiler.py`              | 仅文本 RouteAssist；wiki 摘要按 need 分档（§6）                                                                |
| **5**   | `guard_gate.py`                | 读 `detect.strength` 三档；`strong_text` 连续命中规则                                                        |
| **6**   | `nav_telemetry` + loop 挂接       | §10.6 最小 schema；hierarchy 通道已在 0.6                                                                 |
| **7**   | 端到端（**有人值守**）                  | §12；非无人批跑                                                                                            |
| **8**   | 评估                             | `session_harness` + guard_fp/fn 人工抽检                                                                   |


**存储**：运行时 **`nav_fsm_store.load(app_id)`** 读 `mino.db`；Studio `PUT /api/nav-fsm/{app_id}` 写同一表。证据 Markdown **不**进 `apps.env`。

### 10.1 `effect_assert` 契约（写入 `nav_fsm_edges.effect_assert`）

统一形状：

```json
"effect_assert": {
  "within_ms": 8000,
  "require_any": [
    { "resource_id_regex": "..." },
    { "text_landmarks": ["..."] },
    { "tab_bar": { "selected": "<tab_label>" } }
  ],
  "require_none": [],
  "state_delta": { "dialog": null }
}
```

**不依赖 localize 高置信**；只依赖 hierarchy 稳定元素。边级定义：


| edge 类型 | `require_any` 示例 | `state_delta` |
|-----------|-------------------|---------------|
| Tab 切换 | `tab_bar.selected` 或 `text_landmarks` 含目标 Tab 文案 | `tab: <label>` |
| 进个人页 | profile 头 `resource_id` 或 landmark 文案 | `state: page.profile.*` |
| 打开确认弹窗 | 弹窗 landmark 文案 | `dialog: dialog.confirm_*` |
| 确认后 | 弹窗消失 + 目标按钮态重现 | `dialog: null` |
| `recover.*` | 目标 landmark 消失或回到 home | 按边定义 |


`nav_fsm` 在边执行后（下一 turn observe）评估 `effect_assert`；失败记入 `nav_telemetry`，**不**自动改图。

`resource_id_regex` / profile 头 id 在 **步骤 1** walkthrough 后录入 DB；录入前可只写 `text_landmarks`。

### 10.2 `GET /api/nav-fsm/{app_id}` 响应形状（设计稿）

与 §0.2 表结构一致；`guard_catalog` 可放 `meta.guard_catalog`。

```json
{
  "app_id": "<apps.id>",
  "project_id": "<apps.project_id>",
  "version": "v1",
  "meta": {
    "hierarchy_calibration": {
      "calibration_id": "20260910T120000Z",
      "evidence_rel_path": "nav/calibration/<apps.id>/20260910T120000Z",
      "account_id": "<picked_account.id>",
      "follow_filled_detectable": "id",
      "hierarchy_format": "flat_text",
      "hierarchy_result_key": "hierarchy_text",
      "guard_recheck_on_anchor_hit": true
    },
    "guard_catalog": []
  },
  "test_data": {
    "lease_tags": ["<console_tags>"],
    "anchor_author_field": "case.nav_anchor.author_name",
    "precondition_follow": true
  },
  "states": [ /* nav_fsm_states 行 */ ],
  "edges": [ /* nav_fsm_edges 行，含 recover */ ]
}
```

### 10.3 `detect` 录入前占位（仅 seed 草稿，不得 `load`）

```json
"detect": {
  "strength": "__CALIBRATE__",
  "match_any": [
    { "hierarchy_contains": "已关注" },
    { "resource_id_regex": "__CALIBRATE__" }
  ],
  "match_none": ["未关注"],
  "on_miss": "steer_only"
}
```



### 10.4 `edge.open_author_profile` 与 anchor / 滚动

`guard.anchor.author_name` 隐含：**列表里目标作者那一行的 follow 按钮为 filled**。若 lazy-load 导致目标不在首屏，边不会被推荐，GuardGate 也帮不上忙。

`nav_fsm_edges` 边级字段（walkthrough 后录入 DB）：

```json
{
  "id": "edge.open_author_profile",
  "from": "page.feed.list",
  "to": "page.profile.other",
  "guard": {
    "widget.follow_button": "filled",
    "anchor": { "author_name": "{{case.nav_anchor.author_name}}" }
  },
  "scroll_into_view": {
    "required": "__CALIBRATE__",
    "anchor_match": {
      "text_landmarks": ["{{case.nav_anchor.author_name}}"],
      "parent_resource_id_regex": "__CALIBRATE__"
    },
    "max_swipes": 8,
    "direction": "up"
  },
  "execute": { "steps": ["tap_author_avatar_or_name"] },
  "effect_assert": { "within_ms": 8000, "require_any": [{ "text_landmarks": ["个人主页", "主页"] }] }
}
```


| `anchor_on_first_screen`（校准） | `scroll_into_view.required` | 行为                                                      |
| ---------------------------- | --------------------------- | ------------------------------------------------------- |
| `true`                       | `false`                     | 直接点头像/作者名                                               |
| `false`                      | `true`                      | 先 `swipe_up` 直到 anchor 文本命中，再执行 `execute.steps`         |
| 不稳定                          | —                           | 调整 `case.nav_anchor` 或后续加 `scroll_into_view` 子状态机 |

**anchor 未命中 → 滚动 → guard 重检**（`meta.hierarchy_calibration.guard_recheck_on_anchor_hit: true`）：

1. anchor 未命中 → **steer** `swipe_up`（**不**对 filled 做 block；anchor 与 filled 独立）
2. 滚动后同轮或下轮 `observe("hierarchy")` 刷新
3. anchor 命中 → `nav_fsm` **强制重跑** `guards.detect`（含 filled），再生成允许动作集

禁止：anchor 未命中时跳过 filled 检查，滚动到位后不再重检（会漏拦）。

### 10.5 `validate_nav_fsm`（PUT / load 门禁）

`nav_fsm_store.load()` 与 `PUT /api/nav-fsm/{app_id}` **之前**递归检查：任意字段值含 `__CALIBRATE__` → **422 / raise**，不进入 runtime。

```python
def validate_nav_fsm(obj, path=""):
    if isinstance(obj, str) and "__CALIBRATE__" in obj:
        raise ValueError(f"nav_fsm not calibrated: {path}")
    ...
```

seed 草稿可含占位符；**对 dev DB 执行 seed 前**必须通过 `validate_nav_fsm`。

### 10.6 `nav_telemetry` 最小事件 schema（步骤 6 前钉死）

写入 `session_log`（`writer.append`），`event` 字段名 **定型后不再改名**（字段值可空）：

```json
{
  "event": "nav/localize | nav/edge_attempt | nav/guard_hit | nav/guard_miss_candidate",
  "turn_id": 12,
  "run_id": "cr-xxxx",
  "case_id": "<case_id>",
  "state_id": "page.feed.list",
  "confidence": 0.82,
  "edge_id": "edge.open_author_profile",
  "guard_id": "list.follow_filled",
  "strength": "strong_text",
  "action": "steer | block | stop",
  "strong_text_consecutive_hits": 1,
  "cap_id": "tap_element",
  "result": "pass | fail | skip",
  "hierarchy_stale": false,
  "observe_hierarchy": true
}
```

| `event` | 何时写 |
|---------|--------|
| `nav/localize` | 每 turn localize 后 |
| `nav/edge_attempt` | 边执行后 / `effect_assert` 评估后 |
| `nav/guard_hit` | guard 触发 steer/block/stop |
| `nav/guard_miss_candidate` | LLM 点了 `tap` 关注类 cap 且人可标 fn（§9） |

`session_harness` 与 §9 指标 **只读** 上述 key；新增字段用 optional，不改已有 key。

---



## 11. 测试数据与校准绑定

会 **改变账号状态** 的用例（如取消关注），跑完后 `guards.detect` 依赖的屏态变化 → 下一轮需恢复状态或换租号，否则 `guard_fn` / `task_success` 不可复现。

### 11.1 规范


| 项        | 要求                                                                                              |
| -------- | ----------------------------------------------------------------------------------------------- |
| **租号** | `lease_account` / `ctx.picked_account`；`test_data.lease_tags` 与 Console 号池一致                        |
| **锚点**   | 用例 `nav_anchor.author_name` 与 `nav_fsm.test_data` / 证据 `manifest.json` 一致                          |
| **跑前**   | prep：`check_follow_state`（读 hierarchy）；状态不符 → 先恢复或换租号（首期可人工；后续写 prep 边进 DB）                    |
| **跑后**   | 默认 **不自动回滚账号状态**；下一轮人工恢复或换租号；telemetry 记 `account_id`                                            |
| **隔离**   | 状态敏感用例与注册/清空类用例 **不同 run** 或不同 `lease_tags`                                                          |




### 11.2 与现有能力衔接

- `lease_account` + `ctx.accounts_brief` / `picked_account`（已有）
- `case` JSON 增加可选块：

```json
"nav_anchor": {
  "author_name": "小青",
  "require_followed": true
}
```

- `nav_fsm.test_data.lease_tags` 与 Console 号池配置 **字节一致**（DB 为运维真源；证据 md 可抄一份备查）



### 11.3 有人值守 vs 批跑

- 状态敏感流程：**每次跑前**确认账号/屏态；telemetry 记录 `follow_state_at_start`；
- `run_type=manual` / `copilot`：GuardGate stop → `ask_human`（§11.5）；
- 无人值守 `batch`（未实现）：stop → `give_up`；须配合自动 prep/回滚账号。

### 11.4 证据 · 租号 · DB 配置三元绑定

| 绑定项 | 规则 |
|--------|------|
| `account_id` | `data_dir` manifest、`meta.hierarchy_calibration.account_id`、跑批 `picked_account` **一致** |
| `nav_anchor.author_name` | walkthrough W1–W5 的目标作者与用例字段一致 |
| 不一致 | `nav_fsm_store.load()` 拒绝 → **重新 walkthrough + 重录 DB** |

开发机个人账号采的 hierarchy **不得**录入 `nav_fsm`（关注态、resource-id 可能与租号不同）。

### 11.5 `run_type` 映射（`runs.run_type`，已有字段）

| `run_type` | 含义 | NavFSM / GuardGate |
|------------|------|---------------------|
| `manual` | Console 单用例跑，默认 | stop → `ask_human` |
| `copilot` | 带 `instruction` 时自动升格 | 同 `manual` |
| `batch`（未实现） | 无人值守批跑 | stop → `give_up` |

校准时 walkthrough 与 §12 验收 run 使用 **`run_type=manual`**（或 `copilot`）。若后续加 `batch`，须在 `guard_gate` / `nav_compiler` 显式分支，**不得**默认当 manual。

---

## 12. 验收标准（按 `app_id` 配置完成后）

| # | 验收项 | 判定方式 |
|---|--------|----------|
| 1 | Guard 不误拦 / 不漏拦（在已校准屏态下） | `guard_fp` / `guard_fn` 达 §9；telemetry 有 `nav/guard_hit` |
| 2 | 关键边 `effect_assert` 可判定 | session 中有对应 `nav/edge_attempt` pass |
| 3 | localize 可用 | `nav/localize` 命中预期 `state_id` |
| 4 | hierarchy 通道 | `observe_hierarchy=true` 时 `hierarchy_text` 非空（或按 §10.0.2 降级语义） |
| 5 | 配置真源 | `nav_fsm*` 在 DB；证据在 `data_dir`；代码无 App 文案硬编码 |
| 6 | 图谱不自动膨胀 | 无 session→`atlas_patch` 自动合并 |

**不是验收项**：全 App 一次建完、菜单硬裁剪、VLM 主路径 localize。

---



## 13. 用什么建、谁来看（适配 MinoNexus，非照搬外链方案）

本仓库是 **Nexus 服务端**；UI 在 **MinoStudio / MinoConsole**（独立仓）。下表是**本生态**选型，不是要求引入下列项目为 Nexus 依赖。

### 第一层 NavFSM — 建图与运行时


| 方案                                                          | 是否采用       | 说明                                                                          |
| ----------------------------------------------------------- | ---------- | --------------------------------------------------------------------------- |
| **Nexus** `nav_fsm*` **表 + telemetry**                     | **主路径** | 配置在 DB；证据在 `data_dir()/nav/calibration/`                                      |
| [kaeawc/auto-mobile](https://github.com/kaeawc/auto-mobile) | **可选、离线**  | 在 **Scout/真机** 侧做探索建图，导出 JSON → `atlas_patch` 人工审核；**不能**进 Nexus 循环（违反不碰设备） |
| compose-nav-graph                                           | 不采用        | 仅 Compose 应用；本被测 App 未必适用                                                   |
| 从 session 自动提议边                                             | **后续**   | 禁止失败 session 自动入库                                                           |




### 第二层 Wiki


| 方案                                              | 是否采用    | 说明                      |
| ----------------------------------------------- | ------- | ----------------------- |
| `knowledge_entries` **+** `knowledge_situation` | **采用**  | 已在生产路径；`wiki_ref` 挂 FSM |
| 开源 llm-wiki 项目                                  | **不部署** | 借鉴链接/摘要模式；内容留在 sqlite   |




### 第三层 守卫


| 方案                           | 是否采用    | 说明    |
| ---------------------------- | ------- | ----- |
| **State.guards + GuardGate** | **采用**  | 单一真源  |
| Drools 等规则引擎                 | **不采用** | 双维护风险 |




### 第四层 可视化


| 方案                          | 归属             | 说明                                                         |
| --------------------------- | -------------- | ---------------------------------------------------------- |
| **JSON + Studio 表单**        | 首期           | Nexus `GET/PUT .../nav-fsm/{app_id}`；校准证据只读 API 读 `data_dir` |
| relation-graph / react-flow | **MinoStudio** | 运营看图谱、边成功率（读 Nexus telemetry API）                          |
| FSM Engine 等 Web 编辑器        | **草图阶段**       | 产品/QA 画状态机 → Studio `PUT` 进 `nav_fsm` 表                         |
| 现有 `app_atlas.modules` 树    | **保留**         | 模块树 ≠ 导航图；`module_id` 关联 NavFSM 节点                         |


---



## 14. 与现有代码映射


| 模块         | 路径                                                                                                |
| ---------- | ------------------------------------------------------------------------------------------------- |
| 熔断         | `mino_nexus/loop/action_fuse.py`                                                                  |
| 会话/登录信号    | `runtime/session_gate.py`、`loop/inspections.py`                                                   |
| 知识         | `services/knowledge_store.py`、`knowledge_situation.py`                                            |
| 图谱补丁       | `services/qa_cover.apply_atlas_patch`                                                             |
| 别名         | `services/atlas_aliases.py`、`m_atlas_alias`                                                       |
| Playbook   | `services/app_automation.get_playbook`                                                            |
| Session 评估 | `loop/session_harness.py`                                                                         |
| **0.6 阻塞** | `loop/agent_loop.py`：每 turn `observe("hierarchy")` → `inspect_slots["hierarchy_text"]`          |
| **步骤 1**   | `models/nav_fsm*.py`、`services/nav_fsm_store.py`、`routers/rNavFsm.py`（或 `rAppAutomation` 子路由） |
| **待建**     | `services/nav_fsm.py`（运行时）、`nav_localize.py`、`nav_compiler.py`、`nav_telemetry.py`、`loop/guard_gate.py` |


---



## 15. 里程碑摘要


| 阶段 | 内容 |
|------|------|
| 基线 | ProgressGate；GuardGate + 允许动作集 |
| 一期 | hierarchy 通道；`nav_fsm*` 落库；`data_dir` 校准；telemetry |
| 二期 | EdgeAssist；VLM 低置信可选；菜单硬裁剪 |
| 三期 | 候选管道 + `atlas_patches` + Studio 图谱 UI |
| 四期 | 模块子图 + 覆盖率报告 |


---



## 16. 结论

**v3.0**：NavFSM 是通用子系统，按 **`project_id` + `app_id`** scope；配置在 **`mino.db`**，校准证据在 **`data_dir()`**（与库同根）；本文件仅方案，不存被测 App 数据。

实施入口：**0.4 协议 → 0.6 hierarchy 通道 → 0.5 校准落盘 → 1 migration/写库 → 运行时模块**。

---

## 17. 实现层补充（非方向问题）

阻塞项已升入 §10.0；本节为校准时一并确认的边角。

### 17.1 `tab_bar.selected` 可能无结构化字段

§10.1 中 `tab_bar.selected` 取决于 hierarchy 是否暴露 `selected` 属性。校准时应对每个 Tab 采样本；若无 `selected`，`effect_assert` 退化为 `text_landmarks` + `require_none`。

### 17.2 `CapturedScreen` 需承载 hierarchy 正文

步骤 0.6 实现时，从 PROTOCOL 钉死的 key 解析 hierarchy，写入 `inspect_slots["hierarchy_text"]`（必要时扩展 `CapturedScreen` 或当轮 sidecar，**不猜 key**）。

### 17.3 当前代码态（为何 0.6 必须先做）

`agent_loop` **现状**仅 `observe("screenshot")`，`hierarchy_text` 恒空——这是 **债务**，不是可选项。§10.0.2 为还债的明确工单。