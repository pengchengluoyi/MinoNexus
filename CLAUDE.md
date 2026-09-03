# CLAUDE.md — MinoNexus

本文件是给 Claude Code / 任何在本仓库工作的人的**硬约束清单**。写代码前先读这里。

---

## 0. 本仓库的定位

MinoNexus 是服务端大脑。它决定"下一步做什么"，让 [MinoScout](../MinoScout) 去做。

上游来源是 [MiniOrangeServer](../MiniOrangeServer)（**已停止维护，只读参考，禁止改动**）。搬迁映射见 [docs/MIGRATION.md](docs/MIGRATION.md)。

---

## 1. 硬约束（违反即 CI 失败）

| # | 约束 | 守门脚本 | 为什么 |
|---|---|---|---|
| 1 | **不碰设备** —— 不 import `adbutils` / `uiautomator2` / `playwright` / `facebook-wda` / `Appium` / `pywinauto`，不 `subprocess` 调 `adb` | `scripts/verify_no_device_driver.py` | 设备操作全部经协议委托给 Scout。Nexus 一旦直连设备，就没法上云、没法多节点 |
| 2 | **不含图像算法依赖** —— 不 import `torch` / `open_clip` / `paddleocr` / `ultralytics` / `cv2` | 同上 | 拆分时已确认批次路径运行时零调用（见 `docs/MIGRATION.md` §0）。引回来等于把 45% 的旧代码拖回来 |
| 3 | **不 import Scout** | `scripts/verify_no_scout_import.py` | 依赖必须单向 |
| 4 | **协议契约一致** | `scripts/verify_protocol_contract.py` | 两仓 fixture 哈希必须相同 |

跑 `python scripts/verify_all.py` 一次性检查。

> 约束 2 的例外只有 `pillow`：缩略图压缩（`make_thumb`）需要它，且只做重采样，不做识别。

---

## 2. 三条铁律

### 2.1 能力目录是唯一真源，且只在本仓

`plugins/**.yaml` 声明"能做什么、用什么实现"，**不写"什么时候做、按什么顺序做"** —— 后者是 LLM 的事。

Scout 不读 YAML。所以**凡是 Scout 执行时需要知道的东西，必须由 Nexus 算完塞进 `EXECUTE` 载荷**：`executor_order`、`low_level`、`selected_impl`、`device_hint`。

漏塞一个字段的后果不是报错，而是 Scout 用默认值默默走了另一条路。

### 2.2 循环在本仓，但循环代码不该知道 Scout 存在

`agent_executor.py` / `recovery.py` / `orchestrator.py` 只依赖一个签名：

```python
result: EventResult = router.dispatch(event, ctx, ...)
```

本仓提供 `mino_nexus/loop/router_proxy.py`，**签名与上游 `CapabilityRouter.dispatch` 完全一致**，内部序列化成 `EXECUTE` 发给 Scout。

**不要在循环代码里出现 `if scout_connected` / `await send_to_scout(...)`。** 网络是 `RouterProxy` 的事。这条守住，上游那 3,533 行循环代码才能基本原样搬过来。

### 2.3 四个非设备 executor 留在本仓，不出网

| executor | 为什么在 Nexus |
|---|---|
| `hitl_executor` | 问人 —— 人在 UI 那一侧 |
| `vlm_executor` | 视觉断言 —— 是 LLM 调用 |
| `ai_persona_executor` | 拟人化编排 —— 展开成子事件再逐条派发 |
| `internal_executor` | `wait_ms` / `noop` —— 不需要设备 |

循环里决出的 capability 若属于这四类，**本地执行，不发 `EXECUTE`**。搞错会让 Scout 收到它 `supports()` 返回 False 的能力，白跑一圈 fallback。

---

## 3. 目录约定

**这一节由 `scripts/verify_layout.py` 守着** —— 往根目录加模块 / 加子包会让 CI 红。
不是洁癖：这份清单是新来的人（和模型）判断"东西该放哪"的唯一依据，它一过期，
文件就会照着错的结构落。

```
mino_nexus/
├── app.py                FastAPI 装配（请求模型必须模块级，见 §7）
├── cli.py                mino-nexus 入口
├── protocol.py           九条消息 + EventStatus（stdlib dataclasses，零依赖，见 §5）
├── schemas.py            跨界数据：PlanEvent / EventResult / CapturedScreen（与 Scout 同形）
├── log.py                SLog
├── paths.py              数据目录（data_dir() / APP_DATA_DIR）
├── json_store.py         带锁 + 原子写的小 JSON 文件；所有 *_store 的底座
├── http_util.py          bearer / ok / http_error
├── client_gate.py        客户端准入中间件
├── runtime_tokens.py     Scout 安装凭证
├── node_registry.py      node_id ↔ 连接 ↔ 设备；派单（见 docs/NODE_REGISTRY.md）
├── mdns.py               启动时注册 mino.local（无公网域名）
├── settings.py           **LLM provider 凭据**（给 llm_client 用）
├── settings_store.py     **UI 设置项读写**（给 rSettings 用）—— 与上者的分工见 §8
│
├── routers/              HTTP 入口，沿用上游 r* 命名 + deps.py
├── websocket/            observers.py（UI /ws）· node.py（Scout /node）
├── loop/                 agent_loop · case_runner · router_proxy · local_executors
│                         · agent_stream · persist_run_finish
├── ai/                   planner · prompts · llm_client · schemas · dispatch_log
│                         · roles_catalog · role_prompts · layer_stack · case_text
├── catalog/              能力目录 loader / registry / models / tool_schema / exec_classes
├── runtime/              run_context（从 Scout manifest 构造）· menu · session_gate · env_names
│
└── （业务服务，暂时平铺在根）
    app_automation.py  project_env.py  project_store.py  auth_store.py
    studio_nav.py  plugins_store.py  node_store.py  device_store.py
    device_secrets.py  knowledge_jobs_store.py
    run_store.py  task_store.py  qa_cover.py  qa_process_assist.py
    qa_process_jobs.py  figma_service.py  figma_logic.py  atlas_aliases.py
    ui_devices.py
```

命名沿用上游：router 用 `r*`，websocket handler 用 `w*`。

### 与上游 MiniOrangeServer 的结构差异（以及为什么）

| 上游有 | 本仓 | 原因 |
|---|---|---|
| `core/database.py` + `models/`（SQLAlchemy） | **没有。** `json_store.py` + `*_store.py` | 现阶段用 JSON 文件落盘（`~/.mino-nexus/*.json`），不引 ORM。全仓 grep `sqlalchemy` 应为零命中 —— 由 §1 的守门保证 |
| `core/database.APP_DATA_DIR` | `paths.py` | 上游把数据目录和 engine 塞在同一模块，任何要拼路径的模块都被迫 import ORM |
| `system_settings_service.py`（2,682 行） | `settings.py` + `settings_store.py` | 见 §8 |
| `services/`（80 文件大平层） | **平铺在根**（上表最后一组） | 搬迁时按"一个上游 service 一个根模块"落下来，没有重建这一层 |
| `runtime/run_context.py`（跑探测） | `runtime/run_context.py`（**重写**） | 拆分后连通性来自 Scout 上报，Nexus 不探测（§6） |

> **已知欠账**：根目录现在混了三类东西（基础设施 / 持久化 / 业务服务）。
> 5,600 行还能忍，继续长应该分成 `store/` `services/` `nodes/` 三个包。
> 那是一次纯 `git mv` + 改 import，**不要和功能改动混在一个 commit 里**。
> 真要动时，同步改本节与 `scripts/verify_layout.py` 的白名单。

---

## 4. 改动前必读

| 你要做什么 | 先读 |
|---|---|
| 加一条能力 / 改能力的实现路径 | [docs/CAPABILITY_CATALOG.md](docs/CAPABILITY_CATALOG.md)。多数情况只改 YAML，**Scout 侧零改动** |
| 改 prompt / 决策逻辑 | [docs/PROMPTS.md](docs/PROMPTS.md) |
| 改与 Scout 的通信 | [docs/PROTOCOL.md](docs/PROTOCOL.md) + §5。**协议改动必须两仓同步** |
| 派单 / 设备归属 / 多节点 | [docs/NODE_REGISTRY.md](docs/NODE_REGISTRY.md) |
| 动数据模型 | [docs/DATA_MODEL.md](docs/DATA_MODEL.md) |
| 打通 Console / Studio | [docs/HTTP.md](docs/HTTP.md) |
| 从上游搬代码 | [docs/MIGRATION.md](docs/MIGRATION.md) |

---

## 5. 协议同步规则

协议在两个仓库各有一份 `protocol.py`（本仓 `mino_nexus/protocol.py`，Scout `mino_scout/protocol.py`）。**没有共享包** —— 只有两个仓库，不引入第三个。

契约真源是 **golden fixtures**：`tests/fixtures/protocol/*.json`，两仓字节相同。

改协议的流程，**四步必须同一个 PR 周期内完成**：

1. 改 `docs/PROTOCOL.md`（两仓同一份文本）
2. 改/加 `tests/fixtures/protocol/*.json`
3. 两仓各自改 `protocol.py`，使其能 round-trip 全部 fixture
4. 两仓各自跑 `scripts/verify_protocol_contract.py`（用 `--write` 更新哈希）

哈希记录在 `docs/PROTOCOL.md` §8。两仓不一致 = 协议漂移，CI 失败。

---

## 6. 不要做的事

- 不要为了"快一点"在 Nexus 里直接 `subprocess adb ...` —— 那是拆分失败的第一步
- 不要把设备连通性当成本地可查的状态 —— 它只来自 Scout 的 `REGISTER` / `HEARTBEAT`
- 不要在循环代码里感知网络（见 §2.2）
- 不要把 CLIP / OCR / 组件检测搬回来 —— 拆分时已确认批次路径零调用
- 不要改 `../MiniOrangeServer` 的任何文件
- 不要假设至少有一个 Scout 在线 —— 无节点时必须能启动并在 UI 上说清楚
- 不要让 Console / Studio 去发现 Nexus —— origin 编译进前端，登录失败只提示「无法连接服务器」
- 不要在 `EXECUTE` 里省 `executor_order` —— Scout 不会自己算，会直接无路可走

---

## 7. FastAPI 装配的两个坑（都踩过，都能让服务静默失效）

### 7.1 请求模型 / WebSocket 注解必须在**模块级**

本仓的 `app.py` 与 `websocket/node.py` 都有 `from __future__ import annotations`，
于是函数注解是**字符串**，FastAPI 拿模块 globals 去 `eval` 它。定义在函数内部的
`BaseModel` 子类或 `WebSocket` import 拿不到 → `NameError` → **整个 app 起不来**。

```python
# ✗ 会炸
def create_app():
    class ObserveBody(BaseModel): ...
    @app.post("/x")
    async def x(body: ObserveBody): ...

# ✓
class ObserveBody(BaseModel): ...
def create_app(): ...
```

这个坑修完被一次重写带回来过，坏了一整天而 CI 全绿（静态守门不 import 代码）。
现在由 `scripts/verify_app_imports.py` 守着。

### 7.2 fastapi 版本上界是必须的

**fastapi 0.141.1 起，`include_router(APIRouter(prefix=...))` 会把路由路径解析成空串。**
后果：12 个 router 全部静默失效（没有 `/auth/status`、没有 `/case-runner/run`），
而 `app` 照常启动、`/health` 照常 200。最小复现：

```python
r = APIRouter(prefix="/x")
@r.get("/ping")
def ping(): ...
app.include_router(r)
# 0.124.0 → "/x/ping"      0.141.1 → ""
```

所以 `pyproject.toml` 钉了 `fastapi>=0.124,<0.130`，且 `verify_app_imports.py`
除了基础路由，**必须另验一条带 prefix 的路由**（基础路由全在不代表服务是好的）。

---

## 8. 两套 settings 的分工

| 模块 | 角色 | 落盘 |
|---|---|---|
| `settings_store.py` | **唯一真源。** UI（`rSettings`）读写；6 个 provider 预设、邮件、Figma、role prompt override、`case_execution_use` 等业务开关 | `~/.mino-nexus/settings.json` |
| `settings.py` | **只做 env 覆盖 + 转发。自己不存任何配置。** | 无 |

`llm_client` 与 `runtime/run_context` **只 import `settings.py`** —— "key 从哪来"收在一处。

### 铁律

**provider 字段的形状由 `settings_store.get_ai_provider_credentials` 定义，
`settings.py` 不得自行拼装。** env 覆盖必须叠加在它的返回值上。

踩过的坑（两层）：

1. `settings.py` 曾自己读 `ai_providers.json`，而 UI 写 `settings.json` —— 在设置页填了 key，
   `llm_client` 读不到，所有 LLM 调用报"未配置 provider"。
2. 更隐蔽：`llm_client` 会检查 `provider["case_execution_use"]`（"允许用于跑用例"），
   而当时 `settings.py` **根本不返回这个 key** → `.get()` 得到 `None` → 即使读对文件也被拒。

`tests/test_settings.py` 钉住了字段超集、UI 写入立刻可见、env 只覆盖指名的 provider、
以及 `summary()` 不泄明文 key。

### 环境变量（CI / 本地调试，不经 UI）

```
MINO_AI_PROVIDER   provider id，默认 openai
MINO_AI_API_KEY    有它才触发覆盖
MINO_AI_BASE_URL   缺省用预设
MINO_AI_MODEL      缺省用预设
```

配了 `MINO_AI_API_KEY` 就视为"我要用它跑"，`configured` / `enabled` /
`case_execution_use` 三个开关一并置真 —— 否则 `llm_client` 三道检查过不去。
