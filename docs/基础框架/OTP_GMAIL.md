# 一次性口令（OTP）与 Gmail 收信 — 方案真源

与 [CASE_RESOURCE_KEY.md](CASE_RESOURCE_KEY.md)、环境配置（`project_env`）配合使用。  
**v1**：仅 Gmail IMAP 收信；**全局移除**「解码平台 / adapter HTTP」；不支持公司统一取码服务。

---

## 1. 产品结论（已拍板）

| 项 | 结论 |
|----|------|
| 公司统一取码 HTTP | **不做**，UI / schema / 文案全部移除 `adapter` |
| 邮箱收信 | **v1 仅 Gmail**（`imap.gmail.com`）；其他后缀后续版本 |
| 项目收件箱 | **每个项目一个** Gmail 信箱（地址落在项目环境配置，非密钥） |
| 读信凭证 | **每用户**在 Studio 插件里配置 **Gmail 应用专用密码**（`user_plugin_secrets`），跑批时用**当前登录用户**的密钥读**该项目**配置的收件箱 |
| 同项目多环境 | **测试 / 预发 / 正式**各自可选不同口令方式 |
| **同项目多 App** | **每个 channel（被测应用）** 可单独配置接码方式 + 登录号类型；与跑批 **`env_surface`** 绑定 |
| **自由匹配** | **任意** `channel × env_profile` 组合独立配置（如同一 App：`test`=固定码、`pre`=Gmail；不同 App 同环境也可不同）；无全局强制 |
| 测试账号 | 号池里登录邮箱用 **Gmail 别名**（`+tag`）；**一个项目一个** IMAP 收件箱地址 |

---

## 2. 能力边界

- **Nexus**：`get_otp`（internal）、租号、`login_submit` 发码后等待与填码；**IMAP 只读**，不发邮件、不开浏览器登 Gmail。
- **Scout**：不变；仍 `tap` 发送验证码、`input_text` 填码。
- **Console**：插件策略（是否启用 Gmail 收信插件）；**不存** IMAP 密码。
- **Studio**：**每个被测应用（channel）** 配置接码/登录；环境级默认值；插件页填应用专用密码；号池维护 `email` / `phone`。

---

## 3. 配置分层（项目 → 环境默认 → App 覆盖）

接码策略是 **完全自由的二维表**：**channel.id × env_profile** 每一格可独立选 `fixed` / `gmail` / `hitl` / `auto` 与 `login.kind`（手机/邮箱）。  
同一 App 在 **test 固定码、pre Gmail** 是标准用例，不是特例。跑批必须带 **`env_surface`**，`get_otp` / 租号按该格解析。

```
project_env
├── gmail_inbox.address          # 项目唯一 IMAP 收件箱（根级，非密钥）
├── environments[].secrets       # 全项目「默认」接码/登录（未单独配置的 App 继承）
└── channel_secrets              # 按 App 覆盖（真源优先）
      [channelId][envKey] → { otp, login }
```

### 3.1 项目收件箱（根级，与 App 无关）

```json
"gmail_inbox": {
  "address": "qahi3d@gmail.com"
}
```

所有走 `otp.mode=gmail` 的 App/环境共用此信箱；**不按 App 拆多个 IMAP 信箱**。区分账号靠号池 `email` 别名 + 邮件 To 匹配。

### 3.2 环境默认 `environments[].secrets`

未在 `channel_secrets` 里声明的 App **继承**该环境默认，避免单 App 项目重复配置。

```json
{
  "key": "test",
  "label": "测试",
  "secrets": {
    "otp": { "mode": "fixed", "fixed": "123456" },
    "login": { "mode": "pool", "kind": "phone" }
  }
}
```

### 3.3 按 App 覆盖 `channel_secrets`（必选能力）

**channel.id** 与环境配置里「被测应用」一致（如 `web-hi3d`、`android.main`），与 `package_for_app(..., surface=env_surface)` 同一套 id。

```json
"channel_secrets": {
  "web-hi3d": {
    "test": {
      "otp": { "mode": "fixed", "fixed": "123456" },
      "login": { "mode": "pool", "kind": "phone" }
    },
    "pre": {
      "otp": { "mode": "fixed", "fixed": "123456" },
      "login": { "mode": "pool", "kind": "phone" }
    }
  },
  "web-saas": {
    "test": {
      "otp": { "mode": "fixed", "fixed": "888888" },
      "login": { "mode": "pool", "kind": "email" }
    },
    "pre": {
      "otp": {
        "mode": "gmail",
        "from_allowlist": ["noreply@saas.com"],
        "subject_contains": "验证码"
      },
      "login": { "mode": "pool", "kind": "email" }
    }
  }
}
```

| 字段 | 说明 |
|------|------|
| `otp.mode` | `auto` \| `fixed` \| `gmail` \| `hitl` |
| `otp.fixed` | 固定测码 |
| `otp.from_allowlist` / `subject_contains` | Gmail 收信过滤（**可按 App 不同发件人**） |
| `login.mode` | `auto` \| `pool` \| `hitl` |
| `login.kind` | `phone` \| `email` — 该 App 在本环境下默认用号池哪一列登录 |

### 3.4 解析顺序（跑批 / get_otp / 租号）

入参：`project_id`、`env_profile`（批次）、`env_surface`（channel id，来自新建执行选中的被测应用）。

```text
effective_secrets =
  channel_secrets[env_surface][env_profile]   # 有则整块使用 otp+login
  否则 environments[env_profile].secrets       # 继承环境默认
```

账号级 `otp` 字段仍在 **auto 链最前**（§4.2），用于单账号特例，不替代 App 级 mode。

**缺省行为**：`env_surface` 为空时不得静默用错 App — Preflight 应要求选对被测应用（与 URL/包名解析一致）。

### 3.2 用户插件 `gmail_otp`（Studio）

- **插件 id**（拟定）：`gmail_otp`
- **用户填写**：Gmail 应用专用密码（16 位，[Google 说明](https://support.google.com/accounts/answer/185833)）
- **不落项目 JSON**：密码只在 `user_plugin_secrets`，按 `user_id` + `plugin_id`
- **跑批**：`get_otp` 使用 `project_env.gmail_inbox.address` + **当前执行用户的**插件密钥登录 IMAP

未配置插件密钥时：`mode=gmail` 的 `get_otp` **失败**，提示「请在 Studio 插件中配置 Gmail 收信」；若环境为 `hitl` 则走问人。

### 3.3 号池账号

| 字段 | 说明 |
|------|------|
| `email` | 被测 App 登录框要填的地址；推荐 `qa+{账号id}@gmail.com` 等同收件箱别名 |
| `login_kind` | `email`（邮箱登录用例）；缺省兼容旧数据 `phone` |
| `otp` | 可选；账号级固定码，优先于环境 `otp.fixed` |

**不要求**每个账号单独 IMAP 配置；收信匹配靠 **To 含租号 email** + **发码时间** `otp_sent_at`。

---

## 4. `otp.mode` 与 `auto` 链

### 4.1 模式说明

| mode | 行为 |
|------|------|
| `fixed` | 仅环境/账号 fixed（与今天 Hi3D 短信固定码一致，邮箱登录也可在测试环境用固定码） |
| `gmail` | 发码后 IMAP 轮询项目收件箱取码 |
| `hitl` | 不自动取码，`signal_ask_human` |
| `auto` | 按优先级链（无 adapter） |

### 4.2 `auto` 优先级（口令）

1. 租号账号 `otp` 字段（非空）
2. `channel_secrets` / 环境 `otp.fixed`（若 mode 允许 fixed）
3. 若当前环境 `otp.mode` 为 `gmail` 或 auto 且已配 `gmail_inbox.address` + 用户插件密钥 → **Gmail 收信**
4. `hitl`

**App × 环境**决定链路：例如 `web-hi3d` + test → fixed；`web-saas` + pre → gmail。解析的是 **effective_secrets**（§3.4），不是「整个项目只有一个 mode」。

### 4.3 登录号 `login`

- `pool`：租号后使用 `phone` 或 `email`（由 `login.kind` + 账号字段）
- `hitl`：问人
- `auto`：pool → hitl

---

## 5. 执行时序（邮箱登录 + 接码）

1. 批次 `env_profile` + `env_surface` → `resolve_effective_secrets(project_env, env_profile, env_surface)`（§3.4）
2. `lease_account` → `picked_account.email` = 别名地址
3. Agent：`input_text` 填邮箱 → `tap` 发送验证码 → 记 `ctx.otp_sent_at`
4. `get_otp` → `_resolve_otp` 按 §4 取码；Gmail 分支：
   - IMAP `imap.gmail.com:993`
   - 登录：项目 `gmail_inbox.address` + 用户插件应用专用密码
   - 搜索：时间 ≥ `otp_sent_at - 30s`，可选 `To` 匹配租号 `email`
   - 正文正则提取 4–8 位码（可配置）
5. `input_text` 填验证码；后续与短信链相同

短信场景不变：test 环境 `otp.mode=fixed` + `login.kind=phone` 即可。

---

## 6. Gmail 实现（技术）

- **不依赖**公司 HTTP 取码；**不引入**非商用 OTP 第三方包。
- Nexus 内小模块（如 `services/gmail_otp.py`）：标准库 `imaplib` + 邮件解析 + 正则；行为对齐开源脚本（如 GitHub 上常见的 Gmail IMAP 取验证码示例）。
- **v1**：轮询；v2 可选 IMAP IDLE 降延迟。
- **v2+**：OAuth XOAUTH2、非 `@gmail.com` 后缀。

日志：`get_otp` 摘要可保留 `source=gmail`；验证码是否明文由环境 `redact_otp_in_log`（默认测试可明文，预发可打码）— 实现阶段再定。

---

## 7. 移除 adapter（迁移）

| 位置 | 动作 |
|------|------|
| Studio `ProjectEnvEditor` / `envProfiles.js` | 删除解码平台选项与 URL/Header |
| `project_env.OTP_MODES` / `PHONE_MODES` | 删除 `adapter`；删除 `adapter_url` / `adapter_header` 字段 |
| 存量数据 | `mode=adapter` → 迁移为 `hitl` 或 `auto`（并写 migration 日志） |

Nexus `_resolve_otp` 从未完整实现 adapter；移除后行为与现状一致或更明确。

---

## 8. UI 草案（Studio）

### 8.1 项目级

- **Gmail 收件箱地址**（`gmail_inbox.address`）：一处配置，说明「号池别名均进此信箱；密码在插件」。

### 8.2 环境默认（上线顺序里选中 test/pre…）

- 折叠区 **「默认登录凭证（未单独配置的 App 继承）」**：口令模式 + 登录号类型。
- 与现网「环境配置底部一整块登录凭证」对应，但文案标明是 **默认**，不是唯一。

### 8.3 每个被测应用（channel 卡片）

在已有 **应用与平台**（包名 / URL）下方增加 **「登录凭证」**（按当前选中的上线环境 test/pre 编辑，或 Tab 切换环境）：

- 开关：**继承环境默认** / **自定义**
- 自定义时：一次性口令（自动 / 固定码 / Gmail 收信 / 问人）、固定码、Gmail 发件人/主题过滤
- 登录号：手机 / 邮箱（号池）

保存写入 `channel_secrets[channel.id][env.key]`。

### 8.4 其它

- **插件页**：Gmail 收信 — 应用专用密码（每用户）。
- **号池**：`phone` / `email`；可用 `+别名` 导入（§11）。
- **新建执行**：已选被测应用 → `env_surface`；跑批用该 App 的 effective_secrets。

---

## 9. 实施分期

| 阶段 | 内容 |
|------|------|
| P0 | 移除 adapter；`gmail_inbox` + `channel_secrets` schema；**`resolve_effective_secrets(env_profile, env_surface)`**；`get_otp` / 租号读 effective；Studio channel 卡片「继承/自定义」 |
| P1 | `gmail_otp` 插件 + IMAP；号池 email；环境默认 + per-App UI 完整 |
| P2 | 发件人模板库；日志打码；OAuth；非 Gmail 后缀 |

---

## 10. 示例（同一项目，两个 App）

| App (`env_surface`) | 环境 | otp | login.kind | 说明 |
|---------------------|------|-----|------------|------|
| `web-hi3d` | test | fixed `123456` | phone | 短信登录，与今天 Hi3D 一致 |
| `web-hi3d` | pre | fixed `123456` | phone | 预发仍测码 |
| `web-saas` | test | fixed `888888` | email | 邮箱登录 + 测试固定码 |
| `web-saas` | pre | gmail | email | 预发真实邮件，IMAP + 发件人 `noreply@saas.com` |

项目收件箱：`qahi3d@gmail.com`。  
SaaS 账号：`qahi3d+saas001@gmail.com` … — `get_otp` 按 To + 该 App 的 gmail 过滤条件取码。

执行用户 Alice 在插件中配置应用专用密码；Bob 未配置则其触发的 `gmail` 跑批会失败并提示配置插件。

---

## 11. 账号管理：Gmail 别名怎么建、怎么导入

### 11.1 两个地址各管什么

| 配置位置 | 填什么 | 示例 |
|----------|--------|------|
| **项目 `gmail_inbox.address`** | 用来 **登录 IMAP** 的那个真实 Gmail（收件箱本体） | `qahi3d@gmail.com` |
| **号池每条账号 `email`** | 被测 App **登录框里要输入的邮箱**（可带 `+` 别名） | `qahi3d+acc001@gmail.com` |

规则（Gmail 官方行为）：

- `local+任意标签@gmail.com` 与 `local@gmail.com` **进同一个收件箱**。
- IMAP 用 **本体地址** + 应用专用密码登录即可读到所有别名来信。
- `get_otp` 用租号上的 **完整 `email`** 去匹配邮件头里的 **To/Cc**（或 Delivered-To），避免同信箱多账号串码。

**不是**在 `gmail_inbox` 里写带 `+` 的地址；`+` 只写在 **号池账号** 里，用于区分租号。

### 11.2 `gmail_inbox.address` 怎么配

1. 准备一个 **项目专用 Gmail**（建议团队共用的 QA 邮箱，开启两步验证）。
2. 在 Google 账号里生成 **应用专用密码**（给 IMAP 用，不进项目 JSON）。
3. 在 **环境配置**（实现后：项目根 `gmail_inbox.address` 或各环境展示同一字段）填写：  
   `qahi3d@gmail.com`（**不要**写 `+acc001`）。
4. 每位跑批同学在 Studio **插件 → Gmail 收信** 填自己的应用专用密码（读的是上面这个收件箱）。

各 **环境** 只区分「怎么取码」，不区分收件箱（除非你以后扩展「环境覆盖收件箱」）：

- `test`：`otp.mode=fixed`，号池可不配 `otp`、用环境固定码。
- `pre`：`otp.mode=gmail`，发真实邮件，走 IMAP。

### 11.3 账号管理里批量导入 `+` 别名

Studio **资源 → 账号管理 → 批量导入**（粘贴或 CSV），表头已支持 **邮箱 / email**（见 `account_pool_import.py`）。

**推荐 CSV 示例**（邮箱登录、预发走 Gmail 收信、测试走固定码靠环境，不在账号上写 otp）：

```csv
邮箱,展示名,登录态,环境,备注
qahi3d+web001@gmail.com,Web用例001,logged_out,test,别名001
qahi3d+web002@gmail.com,Web用例002,guest,test,别名002
qahi3d+pre001@gmail.com,预发001,logged_out,pre,预发收信
```

说明：

- **手机号可留空**；租号与 `account_ident` 会优先用 `email`（已有逻辑）。
- **`+` 后缀建议稳定且唯一**（如 `+web001`、与用例/账号 id 对应），方便对信、排错。
- **环境列**与号池筛选一致；同一别名不要重复导入两行（除非你们刻意 merge 策略）。
- 若某环境仍用 **固定码**，可在该账号行填 **验证码/otp** 列，或只靠环境 `otp.fixed`（实现后按 §4 优先级）。

单条新建：编辑账号表单里同样有 **邮箱** 字段，手动填 `qahi3d+xxx@gmail.com` 即可。

### 11.4 跑批时怎么用

1. 用例前置 / 租号条件需要账号 → `lease_account` 选中一条，例如 `email=qahi3d+web001@gmail.com`。
2. Agent 在登录框 **input_text 填该 email**（不是填 `gmail_inbox.address`）。
3. 点发送验证码 → `get_otp` 用 **项目收件箱 IMAP** 收信，并在邮件里找 **发给 `qahi3d+web001@gmail.com`** 的那封。

### 11.5 常见误区

| 误区 | 正确 |
|------|------|
| 在 `gmail_inbox` 写 `xxx+tag@gmail.com` | 收件箱配置写 **本体**；`+tag` 只在号池 |
| 每个账号配一个 IMAP 密码 | **一个项目一个信箱** + **每用户一个**插件密钥读同一信箱 |
| 导入只写手机号 | 邮箱登录用例必须填 **邮箱** 列 |
| 所有环境都配 gmail 收信 | 测试环境可用 **fixed**，仅预发/正式开 **gmail** |

### 11.6 与当前版本差距（实现前）

- `gmail_inbox`、环境 `otp.mode=gmail`、`get_otp` IMAP：**按 §9 分期尚未全部落地**。
- 账号 **邮箱导入 / 编辑** 已支持；**`login_kind`** 在环境 secrets 里为方案字段，落地后邮箱登录用例应设 `login.kind=email`。
