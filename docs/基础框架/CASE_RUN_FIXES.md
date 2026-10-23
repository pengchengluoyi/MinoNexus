# 批跑故障排查与修复记录

本文记录 `cr-01b0dfd9c925`、`cr-efc1af08698d` 等任务的根因、代码修复与复跑结论。  
**修复落盘后需重启 Nexus 进程**，否则内存中的 catalog / skill guards 仍是旧版。

---

## 1. `cr-01b0dfd9c925`（一键登录 / SIM 前置）

### 1.1 现象

| 用例 | 失败原因 |
|------|----------|
| `case-057f1b5a` 一键登录 | 超过 30 步；30 次 `read_device_data`，summary 始终 `model=25102RKBEC` |
| `case-d34f0b7e` 等登录用例 | `tap_element` 返回 `model=25102RKBEC`，界面不变，`assert_visual` 失败 |

### 1.2 根因

**A. Scout 幂等缓存串台（点击「不生效」）**

- Scout 缓存 key：`(run_id, step_idx)`
- Nexus 传参：批任务 `run_id=cr-xxx` + 每用例从 1 起的 `step_idx`
- 第二个用例第 1 步 `tap_element` 命中第一个用例第 1 步 `read_device_data` 的缓存 → **未真正点击**，却 `pass`

**B. `read_device_data` 不返回 SIM 信息（eSIM/SIM 拿不到）**

- Scout `adb_executor._read_device_data` 默认 `key=model`，只 `getprop ro.product.model`
- 能力目录描述 SIM，实现未覆盖 → agent 前置永远完不成

### 1.3 修复

| 仓库 | 改动 |
|------|------|
| **MinoNexus** | `scout_run_id` 改为 per-case `report_run_id`；agent 步骤 `step_idx` 用 `agent_step_idx(case_seq, turn)`（`web_env.py`，slot≥11 与框架 0/1/9 错开） |
| **MinoScout 0.1.17** | `read_device_data` 默认读 SIM bundle；支持 `esim`/`euicc`；单字段 `sim_state`/`phone_number` 等 |

### 1.4 验证要点

- 不同 case 的 `tool/result` 不应再出现跨能力 summary（如 tap 返回 model=）
- 前置 `read_device_data` 应返回 `sim=READY` / `sim=ABSENT` 等

---

## 2. `cr-efc1af08698d`（造好物相机 / 开始造物）

### 2.1 现象

- 用例：`case-99ab0675` — 点击底部「开始造物」→ 白色拍照按钮
- 失败：**超过 30 步仍未完成**
- 工具统计：`recover_system_permission_dialog_while_using_deterministic` ×27，仅 1 次 `launch_app` / `wait_screen_ready` / `tap_element`

### 2.2 根因

**A. 系统权限 recovery 文案与 MIUI 不一致**

- 点击「开始造物」触发**相机权限**弹窗
- 真机文案：**「仅在使用中允许」**
- 规则 actions 仅有「仅在使用**该应用**时允许」等 → 16 次 tap 均未命中 → verify `app_foreground=yes` 失败

**B. deterministic recovery 无熔断 → 死循环**

- `limit_advise_recovery` 只限制 `mode=advise`
- deterministic 失败后可无限重试同一 `recover_*`，27 步耗尽上限

**C. 次要因素（非主因）**

- `observe/hierarchy` 日志字段为 `text_len`/`node_count`，并非空层级（约 50–78 节点）
- Replay 时应用可能已在拍照页，步骤 1「点开始造物 tab」与当前界面不一致

### 2.3 修复（本仓）

| 文件 | 改动 |
|------|------|
| `catalog/recovery_seed.py` | `upgrade_system_permission_recovery_rules()`：actions 优先「仅在使用中允许」「使用时允许」；`match.screen_text_any` 补 MIUI 文案；`top_window_pkg_prefix` 补 `com.lbe.security.miui` |
| `core/bootstrap.py` | 启动时调用上述 upgrade |
| `loop/step_pointer.py` | `recovery_fail_counts` + `bump_recovery_fail()` |
| `loop/registry.py` | `limit_recovery_retry` 守卫：advise / deterministic 同类 recovery 最多 2 次失败后拒绝 |
| `ai/skill_defs.py` | do 阶段 guards 增加 `limit_recovery_retry` |
| `loop/agent_loop.py` | recovery 未恢复时 `bump_recovery_fail`；NavFSM 未启用时从菜单剔除 `fsm_navigate` / `recover_fsm_navigate` |

### 2.4 复跑记录

在**未重启 Nexus**（仅 DB upgrade）下 HTTP replay 父会话 `cr-efc1af08698d::case-99ab0675`：

| 子 session | 结果 | 说明 |
|------------|------|------|
| `cr-542133e2ec7e::case-99ab0675` | fail | recovery ×2 仍失败；第 4 步 **agent 直调 `tap_element`「仅在使用中允许」成功**；后因已在拍照页、无「开始造物」tab，`fsm_navigate` 无实现，give_up |
| `cr-91371dbc3d86::case-99ab0675` | fail | 同类：已在相机页，找不到「开始造物」 |
| `cr-4f9d3c22c49e::case-99ab0675` | blocked | **重启 Nexus 后** replay；无权限 recovery 死循环；`fsm_navigate` 误路由 Scout（无 adb 实现）×3；误点 `?`/`取消`；`action_fuse` 熔断后 HITL `human_choice_single` 未接线 |
| `cr-711c13bf7acd::case-99ab0675` | fail | 修 `fsm_navigate` 本地路由后再 replay；第 1 轮 agent 直接 `give_up`（已在拍照页、无「开始造物」tab），**无 recovery 死循环、无误调 fsm** |

**结论**

- 权限问题：**直点「仅在使用中允许」可行**；DB 规则升级 + 服务重启后，deterministic recovery 应能一次命中
- 死循环：**recovery 次数从 27 降至 2**（新守卫需重启 Nexus 后稳定生效）
- 复跑脏状态：应用未冷启动，步骤 1 与界面不一致 → 建议复跑前 `recover_restart_target_app` 或手动清数据

### 2.5 `fsm_navigate` 误路由 Scout（复跑新发现）

**现象**：`cap=fsm_navigate 对设备 … 无可用实现（该设备通道=['adb']）`

**根因**：`local_executors._fsm_navigate` 已实现 NavFSM 最短路，但 `router_proxy.LOCAL_CAPS` 未包含 `fsm_navigate`；`is_local_cap` 只认 `recover_` 前缀，而目录 id 是 `fsm_navigate`（无 `recover_`）→ 请求发到 Scout。

**修复**：`mino_nexus/loop/router_proxy.py` 将 `fsm_navigate` 加入 `LOCAL_CAPS`。

---

## 3. 操作清单

### 3.1 部署修复

```bash
# 1. Scout ≥ 0.1.17（SIM read_device_data）
# 2. 拉取 MinoNexus 代码后重启 Nexus（必须，否则 catalog/guards 不刷新）
# 3. 启动时会自动执行 upgrade_system_permission_recovery_rules + upgrade_run_case_sop_guards
```

### 3.2 复跑单案

```bash
curl -X POST "http://<nexus>/case-runner/sessions/<session_id>/replay" \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"sn":"<device_sn>"}'
```

### 3.3 日志排查 SQL（`~/.mino-nexus/mino.db`）

```sql
-- 会话摘要
SELECT session_id, status, summary, event_count FROM session_meta
WHERE run_id LIKE '%<run_id>%';

-- 工具调用分布
SELECT json_extract(payload,'$.capability_id'), COUNT(*)
FROM session_events WHERE session_id='<session_id>' AND type='tool/call'
GROUP BY 1;

-- recovery / guard
SELECT type, json_extract(payload,'$.summary'), json_extract(payload,'$.reason')
FROM session_events WHERE session_id='<session_id>'
  AND type IN ('recovery/match','guard/block');

-- guard 运维字段（2026-09）：step、导航、熔断、探针
SELECT type,
  json_extract(payload,'$.guard_id'),
  json_extract(payload,'$.step_n'),
  json_extract(payload,'$.instruction'),
  json_extract(payload,'$.probe'),
  json_extract(payload,'$.fuse_state_tail')
FROM session_events WHERE session_id='<session_id>' AND type='guard/block';

-- 会话结束汇总（仅当本 run 发生过 guard 拦截）
SELECT type, payload FROM session_events
WHERE session_id='<session_id>' AND type='ops/guard_summary';
```

---

## 4. 导航图谱合成修正（2026-09-11 晚）

### 4.1 用户反馈与纠正

- **错判**：`cr-eba445e87a93` 第 3 步确实在灵感页点击底栏（采集帧含 `灵感`/`我的` Tab，非「已在拍照页 give_up」）。
- **根因**：权限弹窗竖排按钮（`仅在使用中允许`/`拒绝`/`本次使用允许`）被 `extract_tab_bar_labels` 误当底栏 Tab；合成后错误 Tab 被 `merge_synthesized_doc` 锁死，新真实 Tab 无法展示。

### 4.2 代码改动

| 文件 | 改动 |
|------|------|
| `nav_capture_store.py` | `is_overlay_screen`（permissioncontroller / dialog_root_view / 全宽竖排按钮）；`synthesis_turns` |
| `nav_synthesis.py` | Tab 识别改为**最底水平行聚类**（`infer_tab_bar_band` / `content_bottom_px`），无屏幕百分比阈值 |
| `nav_screen_layout.py` | `find_bottom_horizontal_tab_row`、`is_modal_button_stack` 几何判定 |
| `nav_candidate_compiler.py` | 去掉 `_MAX_MAIN_SCREENS`；`_MIN_CLUSTER_TURNS=1`；`merge_synthesized_doc` 可替换错误根 Tab、累积追加节点、清理陈旧边 |
| `nav_live_graph.py` | `sync_on_new_capture`；线框优先用**新鲜 Tab 识别** |
| `nav_runtime.py` | 每帧 `append_turn` 后调用 `sync_on_new_capture` |

### 4.3 目标作用域（包名 / 网址）

同一 `project_id` 下可有多个 `app_id`；导航按 **app** 隔离，并在每个 app 内用 `target_scope` 区分：

| 平台 | `target_id` | `kind` |
|------|-------------|--------|
| android | `com.foo.bar` | `package` |
| ios | `com.foo.Bar` | `bundle` |
| web | `https://host`（origin） | `url` |

采集帧写入 `platform` + `target_scope`；合成 / 图谱只保留**匹配本 app 作用域**的帧（其它包名、站点、桌面、权限弹窗一律丢弃）。实现：`services/nav_target_scope.py`。

### 4.4 行为变化

- **每帧采集即合成发布**（不再等打开架构页或手点发布）。
- **图谱无页数上限**，新 session 持续追加；证据足够时可整体替换错误 Tab 入口。
- 权限弹窗不进 App 图谱 Tab 层（仍可通过 `augment_overlay_states` 记 dialog）。

---

## 5. 待办 / 后续

| 项 | 说明 |
|----|------|
| 重启 Nexus | 使 `limit_recovery_retry`、新 recovery actions 进入运行态 |
| 复跑前冷启动 | `recover_restart_target_app` 或清应用数据，避免停在拍照页 |
| Scout recovery tap | 可选：权限按钮 **包含匹配**（`仅在使用` 前缀），减少 ROM 文案漂移 |
| 前置 SIM absent 快速 fail | `read_device_data` 返回 `sim=ABSENT` 时 prep 直接失败，避免空转 30 步 |

---

## 6. 变更文件索引

```
mino_nexus/loop/web_env.py          agent_step_idx / FRAME_AGENT_SLOT_BASE
mino_nexus/loop/agent_loop.py       scout_run_id、recovery fail 计数、fsm 菜单过滤
mino_nexus/loop/registry.py         limit_recovery_retry
mino_nexus/loop/step_pointer.py     recovery_fail_counts
mino_nexus/loop/recovery.py         scout_run_id（前序修复）
mino_nexus/loop/router_proxy.py     fsm_navigate 本地路由
mino_nexus/catalog/recovery_seed.py  permission 规则 upgrade
mino_nexus/ai/skill_defs.py         do guards
mino_nexus/core/bootstrap.py        启动 upgrade 钩子

MinoScout/mino_scout/executors/adb_executor.py  SIM/eSIM read_device_data（0.1.17）
```

---

## 7. localize 选不出当前页（2026-09-17）

跑批 `fsm_navigate` 只会 BACK：运行时图没有 draft 骨骼。修复见 [NAVIGATION_ATLAS.md](NAVIGATION_ATLAS.md) §17.4、[`9月16日-用例执行调用fsmnavigate能力异常.md`](9月16日-用例执行调用fsmnavigate能力异常.md) §0.7。

| 文件 | 改动 |
|------|------|
| `services/nav_route.py` | `overlay_atlas_for_runtime`；`load_fsm_doc(use_live=False)` 叠 draft |
| `services/nav_localize.py` | 底栏纯文案 0.2；wireframe Jaccard；噪声 landmark 跳过 |
| `loop/hierarchy_slots.py` | `hierarchy_is_weak` 看可见文案节点 |
| `loop/local_executors.py` | 无底栏 BACK；有底栏直点 Tab |

---

## 8. 重启被自己拒绝 / 前置三连动作 / 节点永久 busy（2026-10-23）

三条都出自 `cr-8a22b16487ed`、`cr-dc5a828b343e`、`cr-b7abfa648a63`、`cr-7cdcfaa93757` 的同一个复跑序列。

### 8.1 Scout 重启「目标配置运行着当前执行节点」

`IDEError('目标配置运行着当前执行节点…', reason_code='ide_controller_restart')`。

- **停配置是按 `sessionId` 定位的**（`stop_run_configuration` 传 `sessionId` + `configurationName`），与 pid 无关；
- 那个 `pid` 只是 `inspect_running()` 的观测值，Scout 作为独立进程（实测 `ppid=1`）与目标配置同目录时会被误认成同一实例，把正常重启也拒掉。

| 仓库 | 文件 | 改动 |
|------|------|------|
| MinoScout | `mino_scout/ide_run_control.py` | 删除 `ide_controller_restart` 自我拒绝 |
| MinoScout | `mino_scout/ide_control.py` | 同上（debugger_mcp 分支） |

验证：`development_prepare action=restart` 三次连续成功，`state=ready`。

### 8.2 启动后核验读不到前台包名

`_verify_launch_foreground` 只读 `EventResult.data`，而 Scout 把包名放在 `raw_response` 里 →
永远拿到空串 → 把「起的明明是目标应用」判成「起不来」，连发 3 次 `launch_app` 后报
`env_blocked`。另外冷启首屏要 2~4 秒，立刻探测必空。

| 仓库 | 文件 | 改动 |
|------|------|------|
| MinoNexus | `loop/flow_block_executor.py` | 同时读 `data` / `raw_response`；每轮最多 5 次 ×1s 等首屏；`foreground_unreadable` 与 `foreground_mismatch` 分开报 |

### 8.3 已结束的 run 把节点钉在 busy

`cancel_run` 在「仍有底层请求在途」时直接 `return 0`，`_active_runs` 从不摘除；
UI 截图轮询又带同一个 `run_id`，把 `_run_seen` 一直刷新 → `_IDEMPOTENT_TTL_SEC(600s)` 也救不回来。
结果：节点 `busy=True`，后续所有 `restart`/`execute` 都被「等待当前节点的测试和设备操作结束」挡住（实测 20 分钟以上）。

| 仓库 | 文件 | 改动 |
|------|------|------|
| MinoScout | `core.py` | `cancel_run` 保留资源告警但同时摘除在途标记；新增 `_IDLE_RUN_SEC=90`：无在途请求且静默 90s 的 run 不再算活动 |

### 8.4 复跑结论（`cr-7cdcfaa93757`，ZN32233HKS）

| 检查 | 结果 |
|------|------|
| `close_app` | **0**（此前 1 次，无前置依据） |
| 首个设备写动作 | `clear_app_cache`（seq50），**在任何 launch 之前** |
| `clear_app_cache` | 1 次 |
| `launch_app` | 1 次（见 §8.5；此前是 `recovery_action` + `plan_action` 各一次） |
| 前置 | 通过，不再 `foreground_image_withheld` / `env_blocked` |
| 终止点 | `tool_blocked`：`当前不在登录页，但用例要求未登录且尚未取证` —— 领域门，非基础设施缺陷 |

### 8.5 恢复动作拉起后 block 又启动一次

`cr-a0a9e3aff26e`：seq71 `recovery_action launch_app` 成功，seq74 `plan_action launch_app` 又来一次。
`prep_launch_app` 是 block 的 hook 步骤，程序按里程碑照发，不再看「应用其实已经起来了」。
而且 `recovery.py` 的 `_dispatch` 成功启动后没落 `app_launch_confirmed`，守卫也看不到事实。

| 文件 | 改动 |
|------|------|
| `loop/recovery.py` | `_dispatch` 里 `launch_app/open_app/open_url` 成功即 `ctx.app_launch_confirmed = True` |
| `loop/flow_block_executor.py` | block hook 是启动类且应用已启动时，标 `pass` + `app_already_launched`，写 `milestone/already_satisfied`，不再派发 |

复跑 `cr-2e906a0c1368`：`clear_app_cache` 1 次（seq47）→ `launch_app` **1 次**（seq71，恢复动作），block 的启动步骤记 satisfied。

---

## 9. 协议弹窗交给逻辑块（2026-10-23）

以前 `screen_obstacle.kind == "consent"` 走一条通用 `interrupt_accept_consent` 点击，和系统权限弹窗的处理不对齐；
`fb.global.privacy.accept/.decline` 两个块虽然已经 seed，但没有任何派发入口。

| 环节 | 改动 |
|------|------|
| 编译期 | `casework/case_scene.py::_consent_block_id`：用例原文同时出现「协议/隐私/条款」主题词与「同意/接受」或「拒绝/不同意」时，把 `consent_block_id` 写进 `case_scene`；判不出留空，不在运行期猜意图 |
| 执行期 | `loop/interrupt_recovery.py::builtin_obstacle_steps("consent", app_id, consent_block)`：优先按用例给的块 id 展开，缺省 `fb.global.privacy.accept`，解析不到才退回通用点击 |

验证（dev 库）：

```
consent   -> privacy_choice(tap_element) → privacy_confirm(tap_element) → privacy_verified(confirm_login_state)
permission-> system_dialog_allow → system_dialog_confirm → system_dialog_verified
```

注意：块里的 `match` 词表目前只是数据，执行期点击目标仍由 hierarchy 现场解析（与系统弹窗同一套机制）；
真正带隐私弹窗的用例还没跑过，属于待验证项。

---

## 10. 未登录取证过度严格 / 恢复动作进块（2026-10-23）

### 10.1 `confirm_login_state` 只回答「当前是不是登录页」

`cr-d9c1b4ce4799` 的两条用例都停在同一句：`当前不在登录页，但用例要求未登录且尚未取证`。
查下来是两层问题叠在一起：

1. `prep_clear_done` 只在 `agent_loop` 里写，程序派发的 clear 走 `milestones.note_tool_pass`，漏了标记；
2. 标记为假时，`local_executors.confirm_login_state` 对 guest/logged_out 用例判 FAIL，
   要求「先按配置导航取证」——但 clear 已经把应用数据清了，未登录本来就是既定事实。

| 文件 | 改动 |
|------|------|
| `loop/milestones.py` | 程序派发的 `clear_app_cache` pass 也写 `ctx.prep_clear_done = True` |
| `loop/local_executors.py` | 去掉 guest/logged_out 的 FAIL 分支：只报告「在登录页 / 不在登录页」，不承担账号举证 |

复跑 `cr-efedd6d878ba`（api-1-227）：`confirm_login_state` 于 seq126 **pass**，
摘要为「应用数据已重置（pm clear），未登录由重置事实保证」，随后进入 do 阶段；
新的阻断是 `do_s1_1_hook` 在 dev 目录里没有对应能力，与本条无关。

### 10.2 L0 恢复规则的动作改由逻辑块提供

规则里原来自己写一份 actions，块里又写一份 steps，两边各说各话。现在：

| 文件 | 改动 |
|------|------|
| `catalog/flow_block_seed.py` | 新增 `fb.global.device.wake` / `fb.global.app.foreground` / `fb.global.app.restart`，并给出 `RECOVERY_RULE_BLOCKS` 映射 |
| `loop/recovery.py` | `_rule_actions()` 按映射从块取步骤，块缺失才退回规则自带动作并告警 |
| `startup.py` + `flow_block_seed.verify_required_blocks()` | 启动自检 9 个全局块，缺任何一个直接报错，不再静默降级 |

映射：`screen_asleep_or_locked → device.wake`、`bring_target_app_foreground → app.foreground`、
`restart_target_app → app.restart`（`screen_secure_or_no_capture` 是诊断规则，无动作，不进映射）。

---

## 11. 恢复能力收口成 run_flow_block / 调度改为焦点优先（2026-10-23）

### 11.1 `recover_*` 虚拟能力删除

以前模型侧有一条 `recover_<rule_id>` 的虚拟能力，`_run_action` 按前缀截出规则再 `apply_rule`，
动作在规则 payload 里又写一遍。现在统一成：

| 文件 | 改动 |
|------|------|
| `loop/block_dispatch.py`（新） | `run_block(block_id)`：按块步骤顺序派发，遇失败即停，返回聚合结果 |
| `loop/local_executors.py` | 新增 `run_flow_block` 本地执行；只允许恢复类块（`RECOVERY_RULE_BLOCKS` 的值），其余拒绝并回列可执行块 |
| `catalog/recovery_seed.py` | 注册 `run_flow_block`（kind=recovery，caller=case，prep/do，四平台）；提示语改成块 id |
| `runtime/menu.py` | 恢复菜单不再按规则发虚拟能力，只发一条 `run_flow_block` |
| `loop/execution/actions.py` | 删掉 `recover_` 前缀分支 |
| `casework/execution_requirements.py` | 契约里 `recover_<rule>` 换成 `run_flow_block` |
| `loop/registry.py` 等 | 删掉两个 `recover_*` 守卫及其注册；`run_flow_block` 按恢复类处理（不算进展、不清步骤指针） |

自动恢复链路不变：`run_recovery → apply_rule → _rule_actions(块)` 仍然自己执行，不依赖模型。

验证（stub router，不碰设备）：

```
fb.global.app.foreground -> launch_app(package=com.mathmagic.magicam)
fb.global.app.restart    -> close_app → launch_app
fb.global.device.wake    -> wake_screen → dismiss_keyguard
run_flow_block(fb.global.login) -> fail（不在可执行白名单）
run_flow_block(无 block_id)     -> fail
```

### 11.2 调度从「列表第一个」改为「当前焦点」

`first_pending_hook_milestone` 原来只按里程碑列表顺序取第一个未完成的 hook。
中断栈插入、回看推进、程序跳步之后，列表第一个不代表正在做的那一步：
`cr-efedd6d878ba` 的焦点已经在 `privacy_confirm`，调度却回头又派了一次 `prep_launch_app`。

现在先找 `status=in_progress` 的里程碑：是 hook 就派它，不是 hook（且必填）就交回模型，
只有没有焦点时才退回按顺序取第一个。可选行不因 in_progress 卡住后面的 hook。

### 11.3 删 `RECOVER_PREFIX` 引发的 ImportError

`recover_*` 收口时把 `execution/actions.py` 里的 `RECOVER_PREFIX` 一起删了，
但 `agent_loop.py` / `auto_finish.py` / `turn_outcome.py` 还在 `from ... import RECOVER_PREFIX`。
静态门看不出来（它只检查名字有没有定义，不检查 import 的东西还在不在），
要等某个用例真的 ImportError 才暴露：`cr-997250726855`。

| 改动 | 说明 |
|------|------|
| 三个调用方 | 去掉对 `RECOVER_PREFIX` 的依赖，改成 `cap_id == "run_flow_block"` 的显式判断 |
| `tools/check_imports.py`（新） | 逐个模块真实 import，抓「定义删了、调用方还在 import」这类错误；`[OK] 所有模块可导入` 才算过 |
