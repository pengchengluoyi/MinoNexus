# HTTP — Console / Studio 怎么连本仓

UI **只**打 MinoNexus。没有公网域名时用内网名 `mino.local`：Nexus 进程启动后用 mDNS 注册，客户端不填 IP。

开发 / 内网默认：`http://mino.local:10104`。可用 `VITE_NEXUS_URL` 覆盖。

## 登录

1. `GET /sys/server_info` —— 登录页 ping。连不上提示「无法连接服务器」。
2. `GET /auth/status` —— `{ logged_in, needs_setup, mail_configured, ws_token, ... }`
3. `POST /auth/login` body `{ username, email, password }`
4. 成功 `{ code: 200, data: { token, ws_token, user_id, role, ... } }`
5. 之后 HTTP `Authorization: Bearer <token>`，WS `/ws?token=<ws_token>`（与 token 相同）

首次启动若还没有账号，会写入本地初始账号：

| | |
|---|---|
| 用户名 | `admin` |
| 密码 | 环境变量 `MINO_BOOTSTRAP_PASSWORD`，缺省 `Mino@local` |
| 角色 | `admin` |

账号角色只有两种：`admin`（管理员）与 `user`（用户）。旧角色名会映射：`platform_admin`/`org_admin` → `admin`，其余 → `user`。

- **Console**（`X-Mino-Client: console`）：仅 `admin` 可登录；登录用账号密码。非管理员返回 403「仅管理员可登录控制台」。
- **Studio**（`X-Mino-Client: studio`）：邮箱登录 / 邮箱注册。注册**不**校验验证码，只查邮箱格式与是否已注册。

数据目录：`MINO_NEXUS_DATA_DIR`，否则 macOS `~/Library/Application Support/MinoNexus`。

## 客户端门禁

请求头 `X-Mino-Client: console|studio`。

| | Console | Studio |
|---|---|---|
| `/auth/users` 写 | 允许 | 403 |
| `/settings/mail` 写 | 允许 | 403 |
| `/packs` 写（builtin） | 允许 | 403 |
| `/settings/ai/providers` 写 | 403 | 允许 |
| `/runtime/nodes/install-token` | 403 | 允许 |
| `/runtime/nodes/{id}/command` | 403 | 允许 |
| `/project` `/app-automation` `/task` 写 | 403 | 允许 |
| `/case-runner` 写 | 403 | 允许 |

## Scout 安装

安装包在 **Scout GitHub Release**，不进本仓数据目录，也不要在服务器上维护 zip / dmg / exe。

Studio **只**拉 GitHub 公开 manifest，不经过本仓：

```
https://github.com/<owner>/MinoScout/releases/latest/download/manifest.json
```

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/runtime/nodes` | 当前用户可见的节点。`?studio_id=` 为本工作台 id。未归属仅管理员可见；离线节点仍列出 |
| POST | `/runtime/nodes/install-token` | 短 TTL 节点凭证。Studio 写入 Scout 配置 |
| POST | `/runtime/nodes/{node_id}/command` | Studio 对已连接节点下发 `stop` / `restart` / `update`。离线 409。`start` 400 |
| GET | `/releases/scout/latest` | **客户端不用。** 兼容旧调用的可选代理；未设 `MINO_SCOUT_MANIFEST_URL` 时 404。不存文件 |

Console 不提供 Scout 安装或包配置。

## 还没搬的

HITL 问人界面、排期 cron、基线库、从设计稿/定位抽登录图标（CLIP/视觉）、完整 3500 行 Agent 恢复/拟人化路径。

已经可联：登录、概览、账号、发信、模型 Key、**技能**（`/settings/ai/skills`）与 **Jobs**（`/settings/ai/jobs`）prompt 编辑、角色 prompt / 对话、扩展包只读、节点列表、项目/应用/环境/号池、自动化配置与用例草稿、**Case Runner 下发与轮询**（无 Scout / 无 Key 时任务失败并带中文原因，不再 501）、qa-process tick/assist/脑图、**项目用例库导入 preview/commit**、Figma 同步（无 Token 时 400）。

## 项目用例库与导入

用例真源表 `project_cases`，唯一键 `(project_id, case_id)`。导入需先选 `requirement_id`，经 preview → commit；冲突逐条或批量选「覆盖 / 保留」。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/project/{project_id}/cases` | 项目用例列表 |
| GET | `/project/{project_id}/requirements` | 导入可选需求（项目内各 App 聚合） |
| POST | `/project/{project_id}/cases/import/preview` | 解析表格；body 含 `table`、`header_row`、`skip_rows`、`column_map` |
| POST | `/project/{project_id}/cases/import/commit` | 写入；body 含 `preview_token`、各行 `on_conflict` |
| DELETE | `/project/{project_id}/cases/{case_id}` | 删除单条用例 |
| POST | `/project/{project_id}/cases/delete` | 批量删除；body `{ case_ids: [] }` |

旧 `POST /app-automation/qa-process/import/{app_id}` 的 `kind=cases` 已停用，请走上述 preview/commit。

## Jobs（LLM prompt 真源）

Console **Jobs** 页读写下列 API。Studio 写入口经 `client_gate` 拦截。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/settings/ai/jobs` | 列表（含 `system_blocks` / `user_blocks`） |
| GET | `/settings/ai/jobs/health` | 启动烟测：每条 enabled job 能否 render |
| GET | `/settings/ai/jobs/{id}` | 单条 |
| PUT | `/settings/ai/jobs/{id}` | 保存块或 `{ reset: true }` 恢复上一版；保留最近 5 次修订 |
| POST | `/settings/ai/jobs/{id}/preview` | dry-render（假槽 + 可选 flags） |

## 技能

技能是可编辑产品对象（角色 + SOP + prompt + 结果视图），与 `/packs` 分开。引擎 / 指针 / 视图只能选自已注册枚举。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/settings/ai/skills` | 列表 + `enums`（engine / pointer / view） |
| GET | `/settings/ai/skills/{id}` | 单条。空 prompt / 坏 SOP 回退 builtin |
| POST | `/settings/ai/skills` | 新建。`engine` 必须是 `agent_loop` / `qa_job` / `chat` |
| PUT | `/settings/ai/skills/{id}` | 改 prompt / SOP / view。`{ reset: true }` 恢复 builtin prompt |
| GET | `/settings/ai/roles` | 仍返回产品角色；`skills` 字段已是技能表 |
| PUT | `/settings/ai/roles/{id}/prompt` | 兼容旧入口；技能 id 会写进 `skills` 表 |

## 知识库

条目落 `knowledge_entries`。Console「能力 → 知识库」与 Studio 测试页共用 `/settings/knowledge*`。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/settings/knowledge` | 列表。可按 `app_id` / `account_id` / `account_ident` 过滤 |
| PUT | `/settings/knowledge` | 整表替换 `items` |
| PUT | `/settings/knowledge/{id}` | 新建或更新一条 |
| DELETE | `/settings/knowledge/{id}` | 删除 |
| POST | `/settings/knowledge/{id}/review` | `approve` / `reject` |
| GET | `/settings/knowledge/app/{app_id}` | 某应用的条目 |
| POST | `/settings/knowledge/append` | 给某应用追加一条 |

机审开关仍在 `settings.payload.knowledge_jobs`（按被测账号），**不要**写进知识表：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/settings/knowledge/jobs?account_id=&project_id=` | 读该应用登录账号的开关；不传账号返回旧缺省 + `accounts[]` |
| PUT / POST / PATCH | `/settings/knowledge/jobs` | body 必须带 `account_id` 或 `account_ident`，且能在号池里解析到 |
| POST | `/settings/knowledge/auto-review` | 机审待审。关开关时 `skipped`，否则先留人工 |
| POST | `/settings/knowledge/analyze-failure` | 失败分析入口，避免再 405 |

## 自检

```bash
pip install -e .    # 或 uv sync
```
