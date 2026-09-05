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
| 节点 WS 断开 | 同上：在途 run 立刻失败。专机为主，**本切片不重派**。节点行仍留在 `nodes.json`，UI 标离线 |

账号归属（Studio「Scout 节点」页）由 `REGISTER.studio_id` 与安装凭证上的 `owner_user_id` 写入 `nodes.json`。列表过滤：当前登录用户的 `owner_user_id`，或查询参数里的本工作台 `studio_id`。没有这两项的历史节点只给管理员看，归属列写「未归属」。管理员可见全部节点。

**不清除归属**很重要：节点重启期间设备不该从设备表里消失，否则 UI 上的设备会闪现闪灭。

## 3. 冲突：同一台设备出现在两个节点

物理上可能发生（一根 USB 线换机器、TCP adb 被两台机器同时连）。

**规定：后到者胜，并告警。**

```
节点 B 的 REGISTER 里带了已归属节点 A 的 sn
  → 归属改为 B
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

## 5. 一个节点多个并发 run

允许，但要如实反映负载：

- `HEARTBEAT.busy` / `active_runs[]` 报当前在跑什么
- Nexus 侧对**同一台设备**不允许并发 run（设备是独占资源）
- 同一节点的**不同设备**可以并发
- 并发上限由节点自己在 manifest 里声明（未来字段），当前按"每设备串行"约束

## 6. 鉴权与配对

| 阶段 | 凭据 |
|---|---|
| 首次接入 | `REGISTER.token` = 配对 token，由人在 UI 上生成并配置到 Scout |
| 会话期 | `REGISTERED.session_token`，短期，重连需重新 `REGISTER` |
| 任务级 | `EXECUTE.device_hint` 里的设备密码等，带 `ttl_sec` |

上游 `SecurityManager` 是**本地单 token**模型（单机够用）。多节点需要：每节点独立 token、可吊销、token 与 `node_id` 绑定。**这是拆分的待办项，不要沿用单 token。**

## 7. 怎么找到 Nexus

没有公网域名。Nexus **启动时**用 mDNS 注册稳定主机名 `mino.local`（服务类型 `_mino-nexus._tcp`）。

客户端写死这个名字，不扫 IP、不填地址、不发现「哪台是大脑」：

- Console / Studio：`http://mino.local:10104`（构建期 `VITE_NEXUS_URL` 缺省即此）
- Scout：Studio 安装时写入 `nexus_url`，默认 `http://mino.local:10104`，dial `ws://mino.local:10104/node`

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
