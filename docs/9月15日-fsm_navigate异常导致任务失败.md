# 9月15日 — FSM navigate 异常导致批跑任务失败

分析对象：`cr-664c2ef9877d`（造物相机 / 移动端，同一设备连续 3 条用例）

- 应用：`app_id = 3d2b9799-0027-4c7d-bfe7-8c5b88f4087d`，包名 `com.mathmagic.magicam`
- 任务结果：`failed`，`passed = 1`，`failed = 2`
- 背景：`9月15日-大模型单步骤判断是否达成异常.md` 相关 P0（do 达成信号 / step_effect）已落地；本任务用于验证批跑，暴露出 **跨用例屏面延续 + 自然语言与架构页名不对齐 + FSM 导航硬失败 + 冷启动恢复可观测性不足** 等问题
- 优先级：**P0**（解析/导航/降级）、**P1**（可观测性）

---

## 0. 结论

**第二条用例开环仍停在第一条结束时的 3D 造物详情页（无底栏）。模型用 `fsm_navigate` 想赶到「我的」Tab，但 `from_state` 用的是自然语言（「手办详情页」「拍照页」），与 NavFSM 里存的 `state_id` / 展示名 **无法对齐** → 路线图报「当前页无出边」；fallback 直接点 Tab「我的」时屏上无底栏 → tap 失败 → 整条 `fsm_navigate` 记为 FAIL。**

**第三条用例在同一栈上继续失败；模型调用的 `recover_restart_target_app` 报「执行 3 个动作，未恢复」，trace 又看不出是哪一步没过。**

**产品取向（本稿后续修复以此为准，不再做「为这一条用例定制的菜单/批跑策略」）：**

1. **不在批跑层强制每条用例冷启动** —— `fsm_navigate` + 必要时由模型主动 `recover_restart_target_app`，才是跨用例回到可导航态的主路径。
2. **去掉「仅登录模块才允许开环冷启动」的程序门禁** —— `recover_restart_target_app` / 等价能力交给大模型：任意阶段认为需要即可调用，不由 Nexus 按模块类型拦死。
3. **架构页支持改名、别名，并与当前屏做相似度解析** —— 模型说的「手办详情页」在「名称/别名匹配度」与「当前屏对该 state 的 localize 匹配度」双高时，应解析为图中同一页，再走路线图。
4. **FSM 仍失败时降级给大模型手动操作**，并记录尝试路线；**不**在 recover 带隐藏 `fsm_navigate`（避免为用例打补丁式菜单规则）。

与单步达成文档的共性：

> **程序把「当前无法证明」当成「必须失败」** —— 这里体现为：`fsm_navigate` 规划/点击失败即 `EventStatus.FAIL`，缺少降级与结构化 `nav_attempt`；页名解析则把「模型说法」和「图里节点」当成关键字相等，而不是相似度 + 屏态印证。

---

## 1. 任务与用例结果（真源：`app_regression_runs.payload`）

| 序 | case_id | 名称 | 结果 | 摘要（截断） |
|---|---|---|---|---|
| 1 | `case-99ab0675` | 点击开始造物跳转到开始造物 | **pass** | 底部「先炫一下」「做成真的 ¥0.01起」可见 |
| 2 | `case-c516fa0a` | 点击我的进入我的页面 | **fail** | 步骤 1 预期未成立：仍在拍摄/预览界面，无「我的」页或登录弹窗 |
| 3 | `case-1e835d27` | 点击灵感切换到灵感页面 | **fail** | 拍照识别页，找不到底部「灵感」tab；重启后仍无入口 |

任务 ID：`cr-664c2ef9877d`。各用例采集 session：`cr-664c2ef9877d::case-<case_id>`（`~/.mino-nexus/nav/capture/...`）。

---

## 2. 跨用例屏面延续（事实，不作为「必须批跑冷启动」的依据）

### 2.1 第一条用例正常结束在什么屏上

`case-99ab0675` 最后几步（`engine_steps`）：

- 多轮 `wait_ms` 等待生成
- `signal_done`：认定底部已有「先炫一下」「做成真的¥0.01起」
- `assert_visual` **pass**

采集最后一帧（`turn_0015`） hierarchy 摘要：

```
… 3D空间 / 结果1 / 结果2 / 参数信息 / 返回 / 先炫一下 / 做成真的 ¥0.01起
```

`localized.chosen = ""`，`band = recover`，**无底栏 Tab 节点** —— 与架构图里的 `page.tab_*` 入口不在同一层。

### 2.2 第二条用例第一帧 = 第一条的最后一帧

`case-c516fa0a` `turn_0001` 的 `hierarchy_text` 与 `case-99ab0675` `turn_0015` **同型**（3D 详情 / 造物结果页），时间戳连续（`1789456614` → `1789456634`）。

说明：**第二条用例开环时设备仍在上一条的深层页** —— 这是本任务失败的**背景条件**，期望由 **`fsm_navigate` 赶到目标 Tab/页**，或由模型在合适时机 **`recover_restart_target_app`** 重置，而不是在 `case_runner` 里对每条用例自动 `close_app → launch_app`。

### 2.3 当前代码与目标差异：`reset_native_app_before_case`

现状（`mino_nexus/loop/app_env.py`）：`reset_native_app_before_case` **仅在登录模块用例**（`is_login_module_case`）时由 `run_case` 自动执行；本任务三条均为普通功能用例 → **不会**自动冷启动。

**目标改动：**

- **删除「非登录模块不执行」的限制** —— 不再用 `login_module` / `is_login_module_case` 作为是否允许冷启动的门禁。
- **自动开环冷启动不作为批跑默认行为**（不做 P0「每条用例强制 restart」）；冷启动统一走恢复能力 **`recover_restart_target_app`**（及 catalog 里等价动作），由 **大模型在 prep / do / recover 任意阶段自行决定** 是否调用。
- Nexus 只保证：能力在菜单里可用、调用链正确、失败可观测；**不**替模型判断「这条用例该不该重启」。

---

## 3. `fsm_navigate` 异常链（第二条用例为主）

### 3.1 模型行为（`engine_steps` 摘录）

`case-c516fa0a` 关键事件序：

| # | capability | status | summary（要点） |
|---|---|---|---|
| 0 | `fsm_navigate` | fail | `路线图无路径：手办详情页 → page.tab_我的`。当前页无出边；尝试直接点击 Tab「我的」；**tap 无坐标** |
| 1 | `tap_element` | pass | 点击「返回」 |
| 2–3 | `fsm_navigate` | fail | `拍照页 → page.tab_我的`，同样无路径 + Tab 点击失败 |
| 4 | `recover_restart_target_app` | fail | 执行 3 个动作，**未恢复** |
| 5 | `fsm_navigate` | fail | 仍在拍照页，同上 |
| 6 | `assert_visual` | fail | 最终用例失败摘要 |

第三条 `case-1e835d27` 更短：

| # | capability | status | summary |
|---|---|---|---|
| 0 | `fsm_navigate` | fail | `拍照识别页面 → 灵感页面`；目标无入边；尝试 Tab「灵感页面」→ tap 失败 |
| 1 | `recover_restart_target_app` | fail | 未恢复 |

### 3.2 执行器逻辑（`local_executors._fsm_navigate`）

1. 用 `from_state` / `to_state` 调 `nav_route.plan_route_for_app`（内部 `resolve_state_ref`）。
2. 规划失败时 fallback：`direct_tab_tap_params` + `tap_element`。
3. 任一步 tap 失败 → 整个 `fsm_navigate` 返回 **`EventStatus.FAIL`**。

本例直接原因：

- **`resolve_state_ref` 近似关键字/相等匹配**（`state_id`、Tab 文案、后缀片段），**不支持**「手办详情页」↔ 图中某一 `page.*` 的模糊对应。
- **`to_state` 混用** `page.tab_我的` 与「灵感页面」等非注册说法 → fallback selector 错误。
- 深层页 **无底栏** 时，仅靠「点 Tab 名」的 fallback 必然失败（图连通性 / 边配置是另一层问题，见探索丢失文档）。

### 3.3 自然语言 vs 架构页名（核心产品需求）

**冲突本质：** 大模型按业务口语描述屏面（「手办详情页」「拍照识别页面」）；架构图节点有 `state_id`、`display_name`、identify 信号，二者不是同一套字符串。

**目标行为（P0）：**

| 能力 | 说明 |
|---|---|
| **页可改名** | Console / 架构页可编辑节点的展示名（真源在 NavFSM / atlas `meta.display_name`，不硬编码 App 文案）。 |
| **页可设别名** | 每个 state 支持 `aliases[]`（或等价字段），供解析与 prompt 展示共用。 |
| **双通道相似度解析** | 调用 `fsm_navigate` 时，对 `from_state` / `to_state` 原文：在「名称 + 别名 + Tab 文案」上做 **匹配度**（非简单 `==` / 子串），选出候选 `state_id`。 |
| **屏态印证** | 对 `from_state`：当前屏 `localize`（或实时 hierarchy 对 identify 的得分）与候选 state **匹配度也较高** 时，才认定「模型说的就是这一页」；二者缺一则不强行绑到图上节点，走降级或仅用手动 tap。 |
| **规划** | 解析出 `from_id` / `to_id` 后再 `plan_route`；仍无路径或 tap 失败 → **降级**（§5.1），不记为用例级致命失败。 |

实现落点（规划，不写死算法）：`nav_route.resolve_state_ref` 升级为 `resolve_state_ref_fuzzy(...)`；别名与改名走 `nav_fsm` / `rNavFsm` 已有编辑链路；相似度可与现有 `nav_localize` 分数复用或共用特征。

### 3.4 循环层：FSM fail 不会立刻结束用例

`agent_loop` 对 `fsm_navigate` 的 FAIL **不会像 `assert_visual` 那样立即 `_leave`**，会出现 **连续多次 fsm_navigate fail** 占满步数。降级后应减少无效 FAIL 堆积，并把每次尝试写入 `nav_attempt`（§5.2）。

---

## 4. `recover_restart_target_app` 失败（`case-1e835d27` 与 case2 step 4）

### 4.1 规则定义（`recovery_seed._RESTART_TARGET_APP`）

动作链：`close_app` → `wait_ms 1500` → `launch_app`  
验证：`app_foreground=yes` 且 `capture_ok=yes`。

### 4.2 本任务表象

```
restart_target_app: 执行 3 个动作，未恢复
```

**未落盘** `out.actions[]` → 无法区分 close / launch / verify 哪一步失败。

### 4.3 与后续行为的关联

restart 失败后模型仍尝试 `fsm_navigate`；case3 开环仍在拍照识别页。在 **不强制批跑冷启动** 的前提下，更需要：**恢复能力随时可调**（§2.3）+ **restart 分项日志**（P0-4）+ 必要时 FSM/手动导航离开深层页。

可能原因（实现时加日志确认）：

1. 冷启动后 App **恢复上次 Activity 栈**（包名在前台但仍在拍照/3D 页）。
2. `close_app` / `launch_app` 任一步失败。
3. **verify 过严**（`capture_ok`）在动画或黑帧上失败。

---

## 5. 需求记录（产品 / 下一轮开发）

### 5.1 FSM 无法导航时应降级给大模型，不要「硬失败」

1. `plan_route` 失败 **或** fallback tap 失败时，`fsm_navigate` 返回 **非致命** 结果（例如 `DECLINED`/`SKIPPED` + `local_reason=fsm_degraded`），`correction_hint` 说明可继续 `tap_element` / `press_back` / 视情况 `recover_restart_target_app`。
2. 同轮或下一轮 LLM 继续操作，由 step_effect / check 收尾。
3. **不**根据 `localized.band == recover` 从菜单移除 `fsm_navigate`（避免单用例定制策略）。

### 5.2 记录大模型的「尝试路线」

| 字段 | 含义 |
|---|---|
| `nav_attempt.from` / `to` | 模型传入原文 |
| `nav_attempt.resolved_from` / `resolved_to` | 相似度解析后的 `state_id` |
| `nav_attempt.name_score` / `screen_score` | 名称/别名匹配分、屏态印证分（可选） |
| `nav_attempt.plan_ok` / `plan_error` | 路线图结果 |
| `nav_attempt.fallback_tab` / `tap_status` | Tab 兜底与 tap |
| `nav_attempt.degraded` | 是否已降级 |

### 5.3 冷启动：模型决策，程序放权

- 移除 `reset_native_app_before_case` 的**登录模块限定**；不在批跑入口对 `case_seq > 0` 强制冷启动。
- `recover_restart_target_app`：**prep / do / check 任一阶段**均可出现在能力菜单并由模型调用（与 recovery_seed 文案一致）。

---

## 6. 修复清单（建议）

### P0

| ID | 项 | 改法要点 |
|---|---|---|
| P0-1 | FSM 失败降级 | `_fsm_navigate`：规划 + tap 均失败 → 非致命 + `fsm_degraded`；`agent_loop` 非致命列表 + `correction_hint` |
| P0-2 | 页名 / 别名 + 相似度解析 | NavFSM/atlas：**展示名可改、`aliases` 可配**；`fsm_navigate` 解析 from/to 用 **名称相似度 + 当前屏 localize 印证**，替代纯 `resolve_state_ref` 关键字逻辑 |
| P0-3 | `from_state` 缺省补全 | 模型未传 `from_state` 时优先 `ctx.nav_localized_state`（高置信）；低置信时仍走 P0-2 模糊解析，解析失败则降级而非瞎绑节点 |
| P0-4 | restart 可观测 | `apply_rule` / `engine_steps` 写入 `actions[]`；verify 失败附带 `evidence.brief()` |
| P0-5 | 去掉登录模块冷启动门禁 | `app_env.reset_native_app_before_case`：删除 `is_login_module_case` 门闩对**自动**执行的绑定；自动开环仍默认关，**主动** `recover_restart_target_app` 全阶段可用 |

### P1

| ID | 项 |
|---|---|
| P1-1 | `to_state` 与 Tab 文案、别名统一表；解析结果写入 `nav_attempt` 供 Studio 展示 |
| P1-2 | restart verify 分项与「栈顶 Activity」可观测（是否回到 Tab 壳层） |
| P1-3 | 图连通性：深层页入图 / 边补采（与探索丢失文档衔接），减少「解析对了仍无路径」 |

### 明确不做（避免为用例打补丁）

| 原草案 | 原因 |
|---|---|
| ~~recover 带禁止 `fsm_navigate`~~ | 菜单按 band 藏能力是用例特化，不采纳 |
| ~~批跑每条用例强制冷启动~~ | 与「FSM + 模型按需 restart」主路径冲突 |

---

## 7. 复现与核对命令

```bash
sqlite3 ~/.mino-nexus/mino.db \
  "SELECT status, passed, failed FROM app_regression_runs WHERE run_id='cr-664c2ef9877d';"

python3 <<'PY'
import json, sqlite3
con = sqlite3.connect("/Users/changpengcheng/.mino-nexus/mino.db")
row = con.execute(
    "SELECT payload FROM app_regression_runs WHERE run_id=?", ("cr-664c2ef9877d",)
).fetchone()
doc = json.loads(row[0])
for c in doc["cases"]:
    print(c["case_id"], c["status"], (c.get("summary") or "")[:120])
    for s in c.get("engine_steps") or []:
        cap = s.get("capability_id") or ""
        if "fsm" in cap or "restart" in cap:
            print(" ", cap, s.get("status"), (s.get("summary") or "")[:160])
PY
```

---

## 8. 与其它 9月15日文档的关系

| 文档 | 关系 |
|---|---|
| `9月15日-大模型单步骤判断是否达成异常.md` | case1 pass 依赖 do 达成信号；本任务说明 pass 后屏面可留在深层页，需 FSM/恢复导航 |
| `9月15日-探索新页面在架构图中丢失.md` | 深层页未入图时，即使 P0-2 解析正确仍可能无路径 → 补图 + 降级 |
| `9月15日-导航架构页与探索循环问题核对.md` | 登录模块自动冷启动的**历史行为**见该文；本任务改为 **模型按需 restart + 去掉模块门禁** |

---

## 9. 验收（修完后）

1. 重跑同三条批跑：case2 开环可在深层页，但 **`fsm_navigate` 能把「手办详情页」解析到正确 state**（或明确降级并留下 `nav_attempt`），且模型可在任意阶段调用 `recover_restart_target_app`（无登录模块拦截）。
2. 架构页修改展示名 / 增加别名后，模型用口语调用 `fsm_navigate`，在屏态印证通过时规划成功或给出可理解的「无路径」降级。
3. `recover_restart_target_app` fail 时 trace 有 `actions[]` 与 verify 证据，而非仅「未恢复」。
4. **不**验收「每条用例自动冷启动」或「recover 带隐藏 fsm_navigate」。
