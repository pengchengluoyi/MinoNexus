# CASE_RESOURCE_KEY — 用例密钥（ResourceClaim）与租约（ResourceLease）

真源词汇表：`mino_nexus/services/case_resource_key_catalog.py`  
Console 对照页：`GET /settings/case-resource-key`

## 1. 锁与钥匙分别是什么

| 概念 | 谁写 | 存哪 | 作用 |
|------|------|------|------|
| **Claim（锁）** | 用例作者 / 导入 Job | `precondition` 原文 + `case.meta.resource_key` + `case_scene` | 跑批前对照测试资源登记簿，生成 Prep、选号、过滤设备 |
| **Lease（钥匙）** | Nexus 跑批 | `RunContext.resource_lease`、`sns[]`、`package_id` | 本次 run 占用的具体账号与设备；状态转移写回登记簿时带 `run_id` |

用例里**只写 Claim**；Lease 由 `ensure_case_account_lease`、批次 `sns[]`、项目 App 包名解析产生。

## 2. 前置怎么写（编号行）

推荐格式（与飞书/用例库 `precondition` 一致）：

```text
1. 登录态：未登录
2. 账号与数据：已配置形象
3. 环境与权限：清除应用缓存
```

| 行类别 | 映射 Claim | 映射资源 |
|--------|------------|----------|
| 登录态 | `device_app.required_session` + 号池选号 `session`/`login_status` | 机态 + `pool_account_facets` |
| 账号与数据 | `account.requirements` | `pool_account_facets`（含号池模板扩展字段） |
| 环境与权限 | `device_app.prep` / `case_scene.prep_items` | Prep → `clear_app_cache` 等 |

扩展类别见 Console「用例密钥」表（端、设备形态、号池模板 ID 等）。

机器副本（可选，由 Job 从编号行编译）：

```json
case.meta.resource_key
```

示例见 catalog `example_claim`。

## 3. CaseScene 与 resource_key 分工

- **`case_scene`**：循环已消费（`required_session`、`session_prep`、`platform`、`device_need`、`prep_items`、`lease_requirements`）。见 `runtime/session_gate.py`。
- **`resource_key`**：完整 Claim v1（含 `account`、`device_app`、`target_app`）。编译时 **合并进 case_scene**，同名字段以已存在的 `case_scene` 为准。

## 4. Lease 字段（勿写入用例）

| 字段 | 含义 |
|------|------|
| `resource_lease.account_id` | 本 case 租到的号 |
| `resource_lease.run_id` | 占用键 |
| `ctx.sn` | 本 case 使用的设备 |
| `package_id` | 被测 App 包名（项目 App 配置） |

## 5. 实现阶段

1. 文本前置 → `compile_requirements_from_precondition` + `clamp_case_scene`（已部分存在）
2. `resource_key` 入库与 Preflight（对照 `device_app_sessions`）
3. Capability / 流块 → `resource_transition` 写号池与机态
4. **prep 硬门槛**：`prep_resource_gate_issues` — `signal_done` 前须 `prep_clear_done` + 机态满足 Claim（`resource_claim_gate`）
5. **登录流块**：`fb.global.login` 必做步骤完成且 `logged_in` / `get_otp` 成功 → `emit_login_complete`
6. **用例保存**：`case_store` 自动 `sync_case_resource_metadata` → `extra.resource_key` + `case_scene`
7. **HTTP**：`GET /project/{id}/device-app-sessions` · `PATCH …/{sn}/{package}` · `POST /project/{id}/resource-trial`

词汇表增删改只改 `case_resource_key_catalog.py`，Console 自动同步。

## 6. 转移规则与日志（P2）

| 落点 | 作用 |
|------|------|
| `resource_transition_rules` | 配置化 trigger → effects（启动时 seed，Console 可读 `GET /settings/resource-transition-rules`） |
| `resource_transition_audit` | 结构化审计行（run / case / sn / trigger） |
| `session_events` type=`resource/transition` | **与跑批同一条 session 轨迹**，Console「会话日志」可检索；有 `SessionWriter` 时走 `append`，否则按 `report_run_id` 直写 |

原则：**机态/号池真源仍是登记簿与号池表**；`SLog` 只作运维旁路，产品侧以 session log + 审计表为准。
