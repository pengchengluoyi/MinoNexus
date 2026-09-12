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
