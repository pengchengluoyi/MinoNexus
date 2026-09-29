# FSM 逻辑块（Flow Blocks）

> 程序编排登录/系统弹窗等固定顺序；**里程碑**为单步进度真源（见 `docs/架构/V2/执行改造方案-记忆与FSM逻辑块.md`）。

## 标识与存储

| 项 | 说明 |
|---|---|
| 表 | `nav_flow_block_catalog`（`app_id=__global__` 为全局块） |
| 种子 | `mino_nexus/catalog/flow_block_seed.py` |
| HTTP | `GET/PUT /flow-blocks/catalog`，`GET ?channel=web` 筛渠道 |
| 应用覆盖 | `GET/PUT /flow-blocks/overrides/{app_id}` |

内置 Web 邮箱登录：`fb.global.login.email_web`（发码 → 取码 hook → 点验证码框 → 按该次点击输入 → 协议 → 提交）。

## steps_json v2 字段

| 字段 | 含义 |
|---|---|
| `id` | 步 id，对应 milestone.id |
| `kind` | `visual_tap` / `hook` / … |
| `cap` | Scout 能力（visual） |
| `hook_cap` | 本地 hook（`lease_account`、`get_otp`） |
| `cap`（与 `hook_cap` 并存） | hook 步的 **设备义务**（如 `input_text`）。hook 成功只记 ready，设备输入在下一回合，用新的执行序号 |
| `optional` | 可跳过 |
| `skip_policy` | `llm`（须 evidence）/ `never` |
| `guards` | `[{ "type": "block_repeat", "key": "email_tab", "remediate": "hook_chain" }]` |
| `fuse` | `{ "same_target_repeat": 2, "action": "fuse_block" }` |

## 运行时

1. **do 进入**：`ensure_milestones_before_decide` 将块展开为 `success_criteria.milestones`（`decision/milestones` `source=program_seed`）。
2. **每 turn**：`try_dispatch_block_hook` 仅在前序必填里程碑完成后 dispatch 下一 hook。
3. **守卫**：`login_flow_under_milestones` 时由 `flow_block_guards.py` 接管原 registry 四条登录 guard；`guard/block` 的 `dispatch_gate_code` 可为 `flow_block:…`。
4. **菜单**：hook 独占能力从 function tools 剔除（`get_otp`、`lease_account` 等）。
5. **hook 与设备输入分回合**：`get_otp` / `lease_account` 成功后本回合结束，不在同一回合再派设备输入。账号或验证码已经在上下文里时，这一回合只做设备输入。填框用下一回合自己的 `step_idx`。填框失败不推进 `otp_fill` / `email_fill`。
6. **准出（exit）**：先观测、默认不拦截 phase。步级 `milestone/exit_eval`（如 `otp_fill` 须 `input_text:sms_code` 且设备摘要无「焦点未确认」）；`do_to_check` 前 `block/exit_eval` 记录块级影子结论（`would_block` 供后续收紧）。
7. **otp 链**：先 `otp_fetch`（只取码），再 `otp_field` 看图点击。点击千分比写入 `web_sms_code_tap_milli`，**下一回合** `otp_fill` 只消费该落点。邮箱同理：`email_field` 的点击写入 `web_email_tap_milli`，下一回合填写消费它。DOM 查找不能覆盖已有点击。`otp_ready` 后菜单隐藏 `get_otp`。
8. **登录态**：Web 邮箱登录块最后一步 `login_state` 先采层级、再截屏，卡片用这次截屏。邮箱框和验证码框都还在，这一步保持未完成，下一回合再看，最多 `max_turns`（默认 3）次后记「登录未成功」。表单已经不在，这一步通过，随后才进入用例预期的校验。登录是否成功不放进通用校验。校验看图时，画面还在变化就换一帧再看，同一张画面不重复叫模型，最多 3 次。

## check 扩展包（P5）

`expected` 可嵌 JSON：

```json
{
  "schema": "mino.checkpoints.v1",
  "checkpoints": [
    { "id": "c1", "type": "element_exists", "selector": "弹窗标题", "required": true },
    { "id": "c2", "type": "counter_delta", "selector": "收藏", "delta": 1, "baseline_key": "fav" },
    { "id": "c3", "type": "selected_or_lit", "selector": "我的", "required": true },
    { "id": "c4", "type": "visual_assert", "selector": "弹窗居中", "required": true }
  ]
}
```

编译为 `kind: checkpoint` 里程碑；`run_programmatic_checks` 写回 `apply_check_evidences_to_milestones`。

`visual_assert` / 自然语言歧义点：程序侧标 `inconclusive`，check 轮次自动注入 **`assert_visual`**（里程碑路径与旧 `scripted_check` 一致），无需模型先自由决策。

## 逻辑块独占能力

| 能力 | 何时仅块内 hook |
|---|---|
| `lease_account` | 登录块 `email_fill` 未 pass |
| `get_otp` | 登录块 `otp_fill` 未 pass 且已发码 |
| `request_sms_code` | 安卓通用登录块的发码步。邮箱 Web 的 `send_code` 是看图 `tap_element`，点中即记为已发码 |

通用 `tap_element` / `input_text` 仍由 LLM 决策视觉步。
