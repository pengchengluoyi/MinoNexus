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

## 12. `cr-a390541e5dd2` 复盘修复（2026-10-26）

33 条用例里有 23 条倒在四类**机械缺陷**上，都不是产品问题。逐条修，并把「程序该不该判死」
重新划了一遍：程序只判自己确定知道的事，不确定就交回模型看截图。

### 12.1 `run_flow_block` 派发用批次 run_id → `binding_run_mismatch` ×10

**证据**：`cr-a390541e5dd2` 的 213/217/219/220/221/222/223/224/227/234 全部
`contract/blocked reason_code=binding_run_mismatch`，一次设备动作都没发。

**根因**：`local_executors` 把 `run_id=str(params.get("run_id") or "")` 传下去，
`event_interpreter._rid_of` 拿不到就退回 `ctx.run_id`（**批次** id `cr-…`），
而 `RouterProxy` 比对的是 `self.run_id`（**会话** id `cr-…::case-…::sn`），永远不等。

**改法**：`_rid_of` 优先用 `ctx.session_id`（`agent_loop` 里等于 stream_id），批次 id 不再参与派发。

### 12.2 逻辑块版本梯子 → 协议弹窗「原子拒绝」×5

**证据**：228–232 各卡 10 秒后 `screen/recovery_rejected`，
`diagnostics=[{milestone_id: privacy_verified, reason_code: recovery_operation_not_bound,
missing_capabilities: [confirm_login_state]}]`。

**根因**：库里 `fb.global.privacy.accept` 的 steps 还是 v1（`privacy_verified.cap=confirm_login_state`），
代码里早改成 `screenshot`，但种子带版本号且只在「版本更高」时才覆盖 —— 版本阶梯让库和代码永久分叉。

**改法**：删掉 `_LOGIN_VERSION_ORDER` / `_VERSION_ORDER` / `_is_seed_upgrade`，
`SEED_VERSION = "code"`，启动时**无条件**把全局块的步骤同步成代码里的样子（只跳过非 `global_catalog` 行）。
块的内容以代码为准，库只是投影。

### 12.3 系统权限弹窗被算成「异应用」→ 扣图 + 不调模型就 `give_up` ×3

**证据**：214/226/233 的 `foreground_image_withheld` → `blocked_visual_decision` 直接 `give_up`，
`model_called=false`；前台混着 `com.mathmagic.magicam` + `com.android.permissioncontroller`。

**根因**：`nav_target_scope.infer_foreground` 只把 systemui / IME 当系统包，
`permissioncontroller` 落进 `app_pkgs` → `kind="foreign"` → `app_foreground="no"` →
送图策略扣图 → 视觉兜底无图可看 → 弃赛。

**改法**（三层）：

| 位置 | 改动 |
|------|------|
| `nav_target_scope` | 新增 `SYSTEM_DIALOG_PKGS` / `is_system_dialog_pkg()`；弹窗包存在时 `kind="system_dialog"`、`foreground_package` 仍是被测应用、`overlay_package` 记弹窗包 |
| `nav_capture_store.run_guard_foreground` | `system_dialog` 算 `app_foreground=yes` + `system_overlay=yes`（不再扣图） |
| `recovery_permission` | 新增 `system_dialog_present()` / `try_handle_system_dialog()`：结构确认是系统弹窗就派 `fb.global.system_dialog`（允许/拒绝由 `case_scene.system_dialog_block_id` 在编译期定），本回合 defer，下一回合重新观察 |

`agent_loop` 在 `select_observation` 之后、看图之前调用它，所以既不扣图也不让模型猜。

### 12.4 无导航图时回看太严 → 同一个点反复重做 ×5

**证据**：211/212/218/225/case-47530ac5：点「返回」→ 灵感首页 → 我的 → 个人中心 → swipe，
7 回合 34 次设备动作后 `action_result_unverified`。

**根因**：`settle_pending_review` 把 `unlisted`（落点不在里程碑目标面）和 `exclude`（点到了明确不该点的）
同等对待：都 `_rewind()` 里程碑、都连续 2 次就 stop。没有导航图时目标面本身就弱，
`unlisted` 只能说明「这次没落在预期文字上」，据此把里程碑打回重来，模型就会原地重复同一个点。

**改法**：`unlisted` 变成**软失败** —— 不回退里程碑、阈值 3 次、hint 明确写「换入口或换路径，不要重复同一点」，
事件改记 `review/soft_miss`；`exclude` 保持原样（2 次 stop）。

### 12.5 `studio_nav` 删除

`studio_nav` 不是导航图，是 **MinoStudio 侧栏入口白名单**（`testing/agent/knowledge/...`，
Console 决定、Nexus 落盘、Studio 只读）。Studio 已取消，这块逻辑整体删除：
`navigation/atlas/studio_nav.py`、`models/nav.py`、`/me/bootstrap` 里的 `studio_nav` 字段、
`GET/PUT /me/studio-nav`、`capabilities.write_studio_nav`。

### 12.6 恢复路径重复 `launch_app` / 前台探测过密

| 问题 | 改动 |
|------|------|
| 一次 prep 里 `bring_target_app_foreground` 连发 3 次启动（211） | `apply_rule` 遇到 `launch_app` 且 `app_launch_confirmed` 就跳过并记 `app_already_launched`；`close_app/kill_app/clear_app_cache` 通过或派发后把标记复位，保证重启块仍能起应用 |
| `get_foreground_app` 占全部设备动作 68%（683/1010） | `_fill_foreground_from_get_app` 加 2 秒同拍缓存；`RouterProxy.dispatch` 对任何界面写入动作清空缓存 |

### 12.7 程序块定位不到控件不该终止整条用例

**证据**：`cr-318e39386a3b` 的 213：`do_s1_1_hook` → `tap_element` `tap_target_unresolved`
（`execution_started=no`）→ `program_hook_failed` → `tool_blocked`；228：`intent_handoff_do`
→ `general.navigate` 锚点未命中 → 模型 `status=fail` → `milestone_plan_incomplete`；233：模型报
`report_screen_obstacle` 但漏了 `kind` → `screen_obstacle_invalid` → `tool_blocked`。

**改法**：

| 位置 | 改动 |
|------|------|
| `flow_block_executor` | 新增 `_target_unresolved()`：只认 `execution_started=no` + unresolved 证据。命中时把里程碑转成 `visual_tap`（去掉 `hook_cap`）、打开视觉兜底、写 `program/handoff`，本回合 defer，最多 2 次；第 3 次才按失败记账 |
| `vision_plan.handle_execution_obstacle` | 漏 `kind` 时先用结构（系统弹窗 / 协议控件）和模型自己的 summary 推断类别；实在没有则 `contract/reask` 让模型补一次（最多 2 次），不再一次就判死 |
| `do_program_plan` | 「在按钮界面不做任何操作」这类步骤不再生成意图交接，直接按 `do/noop_step` 通过 |
| `local_executors` | 意图因锚点未命中失败时，错误码给 `intent_target_unresolved` 并附「改由模型看图定位」，模型不会再据此 `give_up` |

### 12.8 复跑验证

`cr-318e39386a3b`（3 条）在修复 12.3/12.7 之前跑出的是**新的**失败点，说明 12.1/12.2 已生效：

| 用例 | 旧失败 | 新失败 |
|------|--------|--------|
| api-1-213 | `binding_run_mismatch` | `program_hook_failed`（已由 12.7 修复） |
| api-1-228 | `screen_obstacle_consent` | `milestone_plan_incomplete`（已由 12.7 修复） |
| api-1-233 | `foreground_image_withheld` | `screen_obstacle_invalid`（已由 12.7 修复） |

### 12.9 复跑继续暴露的三处「一次判死」

第 4 次复跑（`cr-9bbe7ff87da2`，211/213）已经跑到第 9 步（手机号、验证码、勾选协议都过了），
但仍在三个地方被一次判死：

| 现象 | 位置 | 改法 |
|------|------|------|
| `input_text` 的 `focus_ref` 指向已经被后续点击覆盖的聚焦动作 → `parameter_contract_mismatch` 直接终止 | `loop/execution/tap_params.py` | 先 `contract/reask` 让人重新聚焦再输入（最多 2 次），仍不行才阻断 |
| `run_flow_block` 在视觉模式下按意图展开时没有锚点，模型连试两次后判失败 | `loop/local_executors.py` | 视觉模式直接拒绝意图展开，回一句「用 tap_element 给坐标或给 block_id」 |
| do 步骤没有专门意图时落到 `general.navigate` 兜底，绕一圈必然 `intent_target_unresolved` | `loop/do_program_plan.py` | 没有专门意图（排除兜底）就直接生成模型视角里程碑，不再经意图 |

仍待处理（本轮未改）：

1. **`prep_lease_account` → `event_not_registered`**（`cr-9bbe7ff87da2` 的 api-1-213）：
   库里 `lease_account` 行是 `caller=system` / `visible_to=["system"]`，而前置按用例侧操作派发，
   能力快照里没有它。要么把租号明确成程序侧操作（`caller=program`），要么允许前置派发 system 能力。
2. **无导航图时坐标靠猜**：`login_entry` / `do_s1_0_nav` 的落点由模型按截图给坐标，
   回看只能判断「这一屏对不对」，判断不了「这一步该点哪」。需要把导航图补起来（见 §12.4），
   代码侧的放宽已经做完（软失败 + 换路重试），剩下的靠数据。
3. **开发操作队列自锁**（`api/development.py:239`）：Nexus 在跑批中途被重启后，
   `restart` 操作排在「等设备空闲」上，而设备又被这个操作占着，形成死锁；
   此时走 PyCharm 控制连接直接 stop/execute 运行配置可以解开。

### 12.10 坐标不该靠猜：能从节点算就别让人再给一次

`cr-f35d6cae59f2` 的 api-1-211 已经跑过前置启动、登录页、第 1–2 步，倒在
「当前视觉操作缺少完整坐标，未派发到节点」：模型手里有 hierarchy 节点（`target.bounds`），
但没写 x/y，于是要重来一次、再判失败。

| 改动 | 位置 |
|------|------|
| 缺 x/y 且带 `target.bounds` 时，用节点中心点按 0–1000 换算补上（`point_source=bounds`） | `action_space/assemble.py` |
| 系统弹窗挡屏时前台记 `unknown`（既不算异应用扣图，也不算「应用在前台」跳过启动核验） | `navigation/atlas/nav_target_scope.py`、`nav_capture_store.py` |
| 独立补答第一次判「未达成」不再直接停：先允许换入口重做一次，重做后仍不达成才停 | `loop/visual_review.py` |

`prepare_launch_unverified` 的根因就是第 12.3 条放松过头：只有系统弹窗、看不到应用自己的节点时
仍被算成 `app_foreground=yes`，前置启动核验于是被跳过。现在弹窗场景只记 `system_overlay=yes`，
前台归 `unknown`。

### 12.11 意图没落成动作 = 交回模型，不是用例失败

`cr-13cf1f327373` / `cr-2c396b2a7228` 的 api-1-211 在第 3 步「查看倒计时显示」卡住：
程序块这一步的意图是 `general.observe`，没有对应通道 → `intent/handoff` → `general.navigate`
→ 结构锚点没命中也没坐标 → `run_flow_block` 返回 fail → 里程碑记 fail → `milestone_plan_incomplete`。

| 改动 | 说明 |
|------|------|
| `loop/local_executors.py` | 意图返回 fail 时不再给 `FAIL`，改为 `SKIPPED` + 「本回合不推进，改由模型看图操作」；视觉模式下的意图展开同样只 skip，不判死 |
| `loop/flow_block_executor.py` | `_target_unresolved` 增加「缺少完整坐标 / 未派发到节点 / tap_target_unresolved」三种证据 |
| `action_space/assemble.py` | 模型给了节点 `bounds` 却没给坐标时，用节点中心点换算 0–1000 补上 |
| `loop/visual_review.py` | 独立补答第一次判「未达成」先允许换入口重做一次，重做后仍不达成才停 |

改完后 211 的失败点从「第 3 步 plan_incomplete」推进到最后一屏的 `model_parse`（模型输出不是合法 JSON），
属于模型侧问题，不再是程序判死。

### 12.12 仍然开着的三件事

1. `prep_lease_account` → `event_not_registered`：库里 `lease_account` 是 `caller=system` / `visible_to=["system"]`，
   前置按用例侧派发时能力快照里没有它。要么改 `caller=program`，要么允许前置派发 system 能力。
2. 「查看/确认」这类纯观察步骤：现在能交回模型了，但模型常常直接给一句自然语言（`model_parse`）。
   应该在只观察的里程碑里显式给一个「观察确认」动作，而不是让它从工具列表里挑。
3. 开发操作队列自锁（`api/development.py:239`）：跑批中途重启 Nexus 会让 `restart` 操作排在「等设备空闲」上，
   而设备又被这个操作占住。此时用 PyCharm 控制连接直接 stop/execute 运行配置可解开；
   代码侧应让 claim 阶段排除自己这条排队项。

## 13. `cr-9f7f478af02c` 复盘（2026-10-26 13:23–13:51）

33 条：pass 1 / fail 3 / blocked 21 / unverified 8。批次墙钟 1658 s，设备动作 986 次，
其中 `get_foreground_app` 409（41%）、screenshot 163、probe_device_state 135、hierarchy 128，
真正写界面的只有 `tap_element` 52 + `launch_app` 46。

### 13.1 `system_dialog_block_failed` ×21（本轮回归）

上一节 12.3 加的「结构确认是系统弹窗就派 `fb.global.system_dialog`」直接变成了 21 条一次判死：
块的允许步骤是 DOM 锚点，Scout 回「DOM 点击锚点未命中，不用坐标兜底」，
`try_handle_system_dialog` 于是 `_leave_execution_blocked`，每条用例 3.1–4.0 s 结束。

| 改动 | 位置 |
|------|------|
| 块失败后按节点 `bounds` 点一次（挑「允许」类文案、排除拒绝类） | `loop/recovery_permission.py::tap_dialog_button` |
| 这一拍强制走坐标系（DOM 方案会把 x/y 抹掉、改回 node 锚点） | 同上，临时切 `ctx.action_scheme="visual"` |
| 用真实画面尺寸换算，没有就**不点**（按 bounds 反推会把 y 算到 2514px） | 同上，由 `agent_loop` 传入本回合 `shot` |

### 13.2 `observation_missing` ×4 = 账号依赖没满足却继续跑

`api-1-215` 的 `execution/blocked`：`dependency_unknown`，`missing_fields: ["resource:account"]`；
`case-50b5e242` 是 `dependency_blocked`，`block:fb.global.login` 状态 blocked。
两种情况 `agent_loop.py:383` 都只写事件、随后照常 `execution_admitted = True` 进循环，
而这一轮没有 preflight 截图 → `capture_ok=no` → 被 `_infer_blocked_from_capture` 当成锁屏 →
`wake_screen`+`dismiss_keyguard` 各两轮 → 仍无观察 → `observation_missing`。

| 改动 | 位置 |
|------|------|
| `capture_ok=no` 不再等于锁屏（设备 awake 且未锁屏时不翻 `screen_blocked`） | `loop/device_display_guard.py` |

**待办**：`execution/blocked` 里 `responsibility=resource` 的依赖缺失应当**直接以该原因结束**
（`env_blocked` + `dependency_blocked`），而不是继续跑 4 个恢复动作再报一个假原因。

### 13.3 `resource_wait_deadline_exceeded` ×3

`case-5c5bf758` / `case-ed07139f` / `case-bc49fd61` 在 admission 阶段等资源组合超时，
103 ms 就结束（排队时间在此之前已经耗尽）。这三条要的是**新用户/未登录**账号，
批次里 `pool_accounts` 的租约被 210/214 等旧账号占用，可复用账号不够。
属于数据面（号池），不是代码。

### 13.4 `model_parse` ×2 = 模型 JSON 少写引号

`api-1-212` 的 `ds-2a5e5e66e64a` 原始输出：
`{"thought":当前do_s2_0_hook为查看吸底区域…,"milestones_append":[]}` —— `thought` 的值没加引号。

| 改动 | 位置 |
|------|------|
| 解析失败前先做两类无歧义修复：给缺引号的字符串值补引号、去掉尾逗号 | `contract/tool_calls.py::_repair_loose_json` |

用那条真实输出验证：修复后能解析出 `thought / milestones_append / screen_obstacle / exit_allowed`。

### 13.5 复跑验证

| 用例 | 修复前 | 修复后 |
|------|--------|--------|
| api-1-214 | 弹窗块 DOM 锚点未命中，3.1 s blocked | 弹窗被点掉，进 `do` 到 `do_s1_0_nav` 回看未通过（原有导航图问题） |
| api-1-216 | 同上 | 进 `do` 到 `intent_handoff_do` |
| api-1-212 | 同上 | 同 214（本轮未跑到模型 JSON 那一屏） |

## 14. 第二轮：`cr-9f7f478af02c` 七个问题的处理（2026-10-26 14:30–14:50）

### 14.1 号池 `resource_wait_deadline_exceeded` ×3 —— 不是并发，是 facet 不可满足

`case-5c5bf758` 的 `resource_allocation_logs` 每 16 秒一条 `lease_fail`，从 13:27:59 一直等
到自己的 deadline 13:32:44，说明等待窗口是真的用满了，不是并发抢占。

要求：`health=available`、`avatar_ready=custom`、`profile_data=filled`。
`pool_account_facets` 里这个项目 26 个账号全是 `lifecycle=unregistered`、`profile_data=none`，
没有 `avatar_ready` 这一项 —— 0 个满足。

补号逻辑按设计不会兜这种情况：`phone_pool_lease.should_provision_phone_seq` 要求
`_fresh_requirements_match`，新号只有「未注册/空资料」，给不出 `custom` 形象，
所以它正确拒绝，只是失败文案写成「可能都被占用或状态不符」，把人引到并发上。

**结论**：这三条要么先在 Workbench 预热「自定义形象 + 资料已填」的账号，要么把前置降级；
代码侧建议把 `lease_fail` 文案改成点名不满足的 facet（未做）。

### 14.2 账号依赖缺失 → 直接停（已改，已验证）

`agent_loop` 的 `BindingAdmissionError` 分支现在先认账号缺口（`missing_fields` 含
`resource:account`），命中就写 `execution/dependency_blocked` 并以 `env_blocked` 结束，
不再进循环。`api-1-215` 复跑：从 `observation_missing`（烧 4 个恢复动作）
变成 `env_blocked / dependency_unknown`，一句「账号资源未就绪（resource:account=unknown）」结束。

### 14.3 系统弹窗的 DOM 来源 —— DOM 是对的，锚点是空的

`observe/hierarchy` 在弹窗那一刻给的是 `com.android.permissioncontroller` 的 12 个节点，
`bounds`、文案都对；Scout 失败原因是 `execution_started: no / tap_target_unresolved`，
即块步骤只声明了 `match` 候选文案，没有 `selector_text`，DOM 没有可锚的东西。

三处改动：

| 改动 | 位置 |
|------|------|
| `match` / `target.name` 命中唯一节点时落成 `selector_text` | `loop/event_interpreter.py::events_from_steps` |
| 块失败后按 `bounds` 点一次，强制坐标系、用真实画面尺寸 | `loop/recovery_permission.py::tap_dialog_button` |
| 目录里没有这一行时不再拦派发（`hierarchy`/`screenshot` 无目录行） | `catalog/skill_channel.py::capability_enabled` |

第三条是复跑时暴露的新缺陷：`api-1-216` 的意图 `order.pay` 因为 caps 里带 `hierarchy`
直接被判「能力未启用或当前不可覆盖」，而 `hierarchy` 走 device/probe 通道、本来就没有目录行。

### 14.4 前台探测仍是 4 次/回合（未收敛）

`_fg_probe_cache` 改成 20 秒窗口、`RouterProxy.dispatch` 出口又加了一层结果去重，
启动核验轮询显式绕过去重；复跑 `cr-55bc51195f34` 仍测得 4 次/回合。
说明四个调用点拿到的 `ctx` 不是同一个对象（或不在同一次 dispatch 出口），
需要按调用链逐个确认；本条不算修好。

### 14.5 复跑结果

| 用例 | 结果 |
|------|------|
| api-1-215 | `env_blocked / dependency_unknown`（新增，真实原因） |
| api-1-216 | 弹窗与私有块均已通过，停在 `order.pay` → `intent_handoff_do` 锚点未命中 |
| api-1-214 | 走到 `do` 深处，最后 `execution_deadline_exceeded`（405 秒预算耗尽） |
| api-1-212 | 不再 `model_parse`，停在 `do_s2_0_hook` 缺完整坐标 |

## 15. 页面/控件语义收口（2026-10-27）

用例整句不再同时冒充页面和控件：

- 编译 `stamp_compiled_plans` 只从显式 `target.name` 建控件；`nav`、`check`、`flow_block`、`remark` 不建控件，检查步骤 `control_id` 置空。
- `page_semantics.analyze_clause` 把“进入/跳转”“查看/观察/状态”“放弃/返回流程”分开；页面短名与业务状态（`variant`）分离，`首条` 落为 `data.position=first`。
- `control_page_binding` 不再用整句 `nav_target` 建页面；同一 `page_key` 的多个状态保留为同一页面的页面状态。
- 旧 8 个 `case_flow` 页面经 `page_semantic_repair` 清理：5 rename / 1 keep / 2 reject；`商品详情页` 保留 `新人折扣`、`退款成功后` 两个状态；真实库生成 68 条 `nav_case_page_refs`。
- Workbench 应用逻辑左侧按 `page_key` 聚合；页面详情显示“页面状态 / 进入这个页面的用例引用 / 控件 / 断言”，控件只显示 `semantic_role=element`。
- 入口缺口仍是运行时阻断，不做编译期整案拒绝；本改动不改变 `case_preflight` 的 pass-through 行为。

## 16. 预编译产物与程序版本（2026-10-27）

复跑 `api-1-210` 时发现：用例编译任务记录的是 `nexus_version=0.1.69`，当前进程是
`0.1.70`，但旧产物仍被当成可执行计划。根因是 `contract/case_view.readiness` 只比较
`compiler_contract` 和 `resource_catalog_version`，漏了 `nexus_version`；而
`case_compilation.run_one` 明明会把 `nexus_version` 算进编译身份。两边口径不一致，
于是版本升级后旧计划继续被选中。

改动：

- `readiness` 同时比较 `nexus_version`、`compiler_contract`、`resource_catalog_version`；
- `execution_requirements.ordinary_compilation_diagnostics` 先做同一份 readiness 检查，
  旧产物在绑定前以 `requirements_need_compile / compile_contract_changed` 停住，
  不再拿旧的 `resource_key` / `case_scene` 去租号和执行；
- `run_comparison.frozen_identity` 记录当前 build identity，避免两次跑批把不同程序版本的
  同一计划误判成“没有变化”。

注意：`VERSION` 升级后已有编译产物会统一视为过期，需要重新编译。仅改运行时代码
（不影响编译产物的路径）不应触发整库重编；但如果改的是编译器语义、prompt 或资源目录，
仍应同步提升 `COMPILE_CONTRACT_REVISION` / `CATALOG_VERSION`，不能只靠版本号。

## 17. 账号事实 `registered_days`（2026-10-27）

「账号注册≤30天」不是注册流程状态，不能编译成 `register_flow=registered`。新增内置账号事实：

```text
facet: registered_days
label: 注册天数
value_kind: number
unit: 天
min/max: 0 / 36500
```

用例侧生成：

```text
账号注册≤30天          → registered_days lte 30
注册超过30天           → registered_days gt 30
注册未满7天            → registered_days lt 7
注册满7天              → registered_days gte 7
```

链路：

- `resources/accounts/account_facts.py` 定义字段和自然语言抽取；`compile_resource_key_from_precondition`
  会把它写进 `account.requirements.all` 与 `case_scene.lease_requirements`，不依赖模型也能得到确定事实。
- `case_semantic_integrity.validate_precondition_hits` 支持数值字段的 `lt/lte/gt/gte`，并把误选的
  `register_flow` 在同一条含注册天数的原文上纠正为 `registered_days`。
- `case_step_span_llm` 把 `value_kind/unit/min/max/help` 和 `op` 槽交给模型；prompt 明确示例
  「注册≤30天 → registered_days/lte/30」。模型输出仍受字段和值校验，不允许自造 facet。
- `resource_pool.match_requirements` 对数值事实做数值比较，而不是字符串比较；`unknown` 不满足。
- 号池账号的 `registered_days` 是事实值（例如 `7`），需要由真实账号资料、夹具或证据化写回填入；
  没有该值的账号不会被 `lte 30` 命中，不能靠模型猜一个值来冒充账号事实。
- `account-facet-commit` 的模型写回也接受数值事实，但只生成候选；仍需证据、规则和确认，
  不会因为模型输出一个天数就直接变成可分配事实。

`account-facet-schema` 的 AI 字段扩展也允许 `value_kind=number`，避免「注册天数」被拆成多个枚举。
编译合同升到 `COMPILE_CONTRACT_REVISION=17`，资源目录升到 `CATALOG_VERSION=14`。
