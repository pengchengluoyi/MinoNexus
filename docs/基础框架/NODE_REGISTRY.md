# NODE_REGISTRY — 节点、设备、派单

拆分引入的新概念：**`node_id`**。上游 MiniOrangeServer 只有 `sn`，它同时充当设备标识和连接端点 —— 单机单进程下够用，多节点下会立刻混乱。

## 1. 两个层级

```
node_id  = 一台跑着 MinoScout 的机器
   └── devices[]  = 这台机器能操作的设备
        ├── sn: R5CT30xxxx   (Android, adb + remote)
        ├── sn: claw-abc123  (Android, 只有 remote)
        ├── sn: 00008030-... (iPhone, ios_wda)
        └── sn: web{scout_id} (虚拟槽，不是真实设备)
```

| 概念 | 定义 | 稳定性 |
|---|---|---|
| `node_id` | Scout 节点标识，`[a-z0-9]{16}`（即 `scout_id`） | **重启后不变**。变了等于换了一台机器 |
| `sn` | 设备标识（adb serial / iOS UDID / `claw-*` / `web`+`node_id`） | 跟设备走 |
| `web{node_id}` | Playwright 虚拟槽 | 每个节点一个、跟节点走。旧名 `web-local` / `web_local` 仅作升级残留，无活节点上报时从登记表清掉 |

## 2. 归属

设备表（上游 `MDevice`）的 `channels` JSON 上**新增 `node_id` 字段**：

```json
{
  "node_id": "mac-studio-01",
  "adb":    {"state": "connected",    "transport": "usb", "serial": "R5CT30xxxx"},
  "remote": {"state": "connected",    "auth_state": "Authenticated"},
  "ios":    {"state": "not_applicable"}
}
```

归属由 Scout 的 `REGISTER` / `HEARTBEAT` 决定，**Nexus 不猜**：

| 事件 | Nexus 的动作 |
|---|---|
| `REGISTER` 里出现新 `sn` | 自动登记设备，`node_id` 设为该节点，`REGISTERED.warnings` 里告知 |
| `REGISTER` 里的 `sn` 已归属**另一个** `node_id` | 见 §3 冲突 |
| `HEARTBEAT.device_delta` | 更新该设备的 `channels` 状态。通道不是 `connected` 时 UI `status` 为 offline（节点还活着也不算在线） |
| 节点 45s 无心跳 | 该节点名下所有设备连通性置 `disconnected`，标注"节点离线"，**不清除归属** |
| `EXECUTE {capability_id: node.device_lost}` | 该设备连通性置 `disconnected`。残留 `web-local` / `web_local` 直接删掉，不当成有效槽 |
| `EXECUTE {capability_id: node.shutting_down}` | 立刻把该节点 `HEARTBEAT.active_runs` 里的在途 run 判失败。不断线干等 |
| 节点 WS 断开 | 同上：在途 run 立刻失败。专机为主，**本切片不重派**。节点行仍留在 SQLite `nodes`，UI 标离线 |

账号归属由登录会话签发的安装凭证确定，写入 SQLite `nodes.owner_user_id`。已归属节点的列表及所有节点操作均仅允许本账号归属；`studio_id` 是兼容元信息，不能扩大权限。管理员在 Workbench 的节点列表额外看见历史无归属节点，并可显式认领到当前账号；认领前不允许设备派单、节点命令或插件操作，不提供账号间迁移。

**不清除归属**很重要：节点重启期间设备不该从设备表里消失，否则 UI 上的设备会闪现闪灭。

节点管理页面的 `devices[]` 是当前连接清单，区别于设备表里的持久历史：只按 Registry 当前 `node_id` 筛选，移动设备需有连接通道，不追加数据库历史设备。当前节点上报的 Desktop/Web 槽保留阻塞原因。节点离线时实时清单为空、计数为零；全局任务选择器仍可显示本人历史设备。设备移到另一节点后，旧节点迟到的断开上报不更改当前位置。

## 3. 冲突：同一台设备出现在两个节点

物理上可能发生（一根 USB 线换机器、TCP adb 被两台机器同时连）。

**规定：同一 owner 内后到者胜；不同 owner 的同 SN 上报忽略，不能转移设备或继承原配置。**

```
同账号节点 B 的 REGISTER 里带了已归属节点 A 的 sn
  → 当前位置改为 B，账号 owner 不变
  → 向 UI 广播一条 warning："设备 R5CT30xxxx 归属从 mac-studio-01 变更为 win-box-02"
  → 若该设备上有在途 run，该 run 判为中断（不尝试迁移）
```

不做仲裁、不做抢锁 —— adb server 本身就是单机独占的，同一台设备挂两个节点的行为在驱动层就已经未定义。**运维上应避免**，见 `../MinoScout/docs/DEVICE_SETUP.md` §adb。

## 4. 派单

`POST /case-runner/run {app_id, sns[], cases[]}` 的解析顺序：

```
for sn in sns:
    1. 查设备表 → node_id
    2. node_id 有活连接？
         无 → 拒绝，原因："设备 {sn} 所属节点 {node_id} 离线"
    3. 该节点上报的 executors ∩ 能力目录 → 该设备的可用菜单
    4. 菜单为空？
         是 → 拒绝，原因："设备 {sn} 无可用执行通道"
    5. 建 run，循环起在 Nexus，EXECUTE 打向该节点
```

**不要静默排队。** 节点离线时立刻拒绝并说清是哪台设备、哪个节点 —— 排队会让人以为任务在跑。

多设备批次里部分设备不可用时：可用的照跑，不可用的在任务结果里标 `SKIPPED` + 原因，**不要整批失败**。

`coverage=once` 且 `sns[]` 长度 ≥ 2（Studio「加速拆分」）：用例按设备 **轮询分片**（case i → `sns[i % n]`），**每台设备一个后台线程** 并行执行各自分到的用例；`per_device` 仍为每台跑完整批、线程内串行。

## 5. 一个节点多个并发 run

允许，但要如实反映负载：

- `HEARTBEAT.busy` / `active_runs[]` 报当前在跑什么
- Nexus 侧对**同一台设备**不允许并发 run（设备是独占资源）
- 同一节点的**不同设备**可以并发
- 并发上限由节点自己在 manifest 里声明（未来字段），当前按"每设备串行"约束

## 6. 鉴权与配对

| 阶段 | 凭据 |
|---|---|
| 首次接入 | `REGISTER.token` = Workbench 签发的 15 分钟安装凭证，每台电脑独立生成 |
| 断线重连 | 首次注册换发的独立 `node_token`，绑定账号和 `node_id`，持久化到 Scout 配置 |
| 会话期 | `REGISTERED.session_token`，短期，重连需重新 `REGISTER` |
| 任务级 | `EXECUTE.device_hint` 里的设备密码等，带 `ttl_sec` |

Scout 必须声明 `REGISTER.account_isolation_version=1`，旧客户端在消费安装凭证前明确拒绝。`REGISTERED.owner_user_id` 用于本地插件/密钥命名空间确认。

安装凭证在 SQLite 写事务中原子绑定到一台电脑。响应丢失时，仅允许同一 `node_id` 在有效期内恢复同一长期凭证；不能登记第二个节点或恢复已吊销凭证。相同电脑重装保留身份及原长期凭证，其他账号绑定必须明确拒绝。`POST /runtime/nodes/{node_id}/revoke-token` 仅吊销这一节点并断开连接；不提供隐式跨账号转移；后续维护转移须先吊销旧凭证、隔离配置并重新绑定。任意非空 token 和共享环境 token 不再接受。

## 7. 怎么找到 Nexus

局域网部署时 Nexus **启动时**用 mDNS 注册稳定主机名 `mino.local`（服务类型 `_mino-nexus._tcp`）；跨网络使用部署域名或 VPN。

客户端写死这个名字，不扫 IP、不填地址、不发现「哪台是大脑」：

- Console / Studio：`http://mino.local:10104`（构建期 `VITE_NEXUS_URL` 缺省即此）
- Scout：安装命令写入 `nexus_url`，默认 `http://mino.local:10104`，dial `ws://mino.local:10104/node`

安装命令优先使用 `MINO_NEXUS_PUBLIC_URL`；未配置时使用请求的外部地址，反向代理须配置 uvicorn 的可信代理范围，应用不直接信任 `X-Forwarded-*`。请求地址为回环或未指定地址时使用既有 `mino.local` 部署地址，不能将浏览器的 localhost 带到远端电脑。发布清单沿用 `MINO_SCOUT_MANIFEST_URL`，命令使用同目录发布的 `bootstrap.sh` / `bootstrap.ps1`；安装脚本严格匹配目标电脑的系统和 CPU 架构。

局域网要能解析 `.local`（macOS 自带，Windows 10+ / Linux 需 mDNS）。Nexus 必须监听 `0.0.0.0`，不能只绑 `127.0.0.1`。

不要再给客户端做 IP 选择器，也不要把局域网 IPv4 写进 HTTP 响应。

## 8. 与上游的差异速查

| 上游 | 本仓 |
|---|---|
| `sn` 既是设备也是端点 | `node_id`（端点）+ `sn`（设备），两级 |
| ClawNode 直连 server `/ws` | ClawNode 连 **Scout**；Scout 连 Nexus `/node` |
| 连通性由 server 自己探（`connectivity_probe`） | 连通性由 **Scout 探并上报**，Nexus 只缓存 |
| `SecurityManager` 单 token | 每节点 token + 会话 token + 任务级凭据 |
| 设备不可用 → 跑中才发现 | 派单时就拒绝并说明 |


## PC Desktop 接入与调度（V4）

节点的 `host.os/arch/hostname/desktop` 是 Scout 上报的事实。每个节点只登记 `desktop:<node_id>:console` 一个交互桌面；窗口、显示器和前台应用都不是设备。`platform=desktop` 与 `os=macos|windows|linux` 分开，Web 槽保留。节点不能上报其他节点的 Desktop/Web 槽；宿主 OS 与桌面 OS 不符时拒绝该槽并返回 warning。

设备信息持久化在 SQLite `m_device.extra`，包含 `os`、`session_key`、`session_generation`、`desktop_state`、`blocked_reason`、`capabilities`、`readiness`。节点信息持久化在 SQLite `nodes`，不是旧 JSON 文件。离线保留原设备行与会话信息，但 `executable=false`，原因以所属节点离线为准。缺权限、锁屏、无 GUI、缺插件和未上报能力分别显示；在线不代表桌面就绪。心跳允许更新 Desktop executor 的可用性。

运行与探索提交通过同一服务重新核对账号归属、真实设备平台、应用渠道/OS、能力目录与当前占用。客户端 `platform` 不决定路由；不兼容或不就绪返回 409。他人资源返回 404，普通账号看不到未归属节点。用户选中的目标不可用时不自动换设备。本次 Desktop 接入提交中的任一选中目标不满足准入条件即拒绝整个提交，避免静默漏跑。

`device_reservations` 与新任务在同一个 `BEGIN IMMEDIATE` 事务中写入。Desktop 和同宿主 headed Web 共用 `desktop:<node_id>:console`；headless Web 保留 4 个 lane，移动设备按 sn 独占。历史运行中任务即使没有 reservation/device_targets 也参与占用判断，缺失宿主 ID 从设备登记恢复。任务终态释放对应 reservation，进程重启不凭空释放仍处运行态的任务。

任务保存冻结的 `device_targets`，含提交时的会话代际。RouterProxy 下发前核对代际和独占租约；Scout 仅走 Desktop 执行器，未知平台拒绝，不落到 ADB。截图捕获引用用于点击，动作尝试即消费引用；会话变化或取消失效。首版只开放 visual；目录缺少 Desktop 路径时禁止提交，管理员在 Workbench 显式预览/应用配置，启动不自动灌入这些能力。

历史无归属节点只由管理员通过 `POST /runtime/nodes/{node_id}/claim` 一次性认领到当前账号。认领更新节点、未归属设备和现有长期凭证 owner，断开当前连接后由 Scout 使用原凭证重新注册；已有 owner 的节点返回 409。此接口不提供账号间迁移。

当前 macOS Cua 适配已完成代码与隔离联调；真实权限、原生应用点击输入及实际发布包仍须在目标电脑验收。Windows/Linux 只上报宿主与阻塞状态，不宣称原生桌面执行已支持。
