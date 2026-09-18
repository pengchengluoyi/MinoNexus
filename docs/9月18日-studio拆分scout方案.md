# Studio 拆分 Scout — 安装链接、归属与升级（2026-09-18）

**落地主线：** 用户在 **Mino Studio**（已登录）复制 **一条安装命令** → 在 **执行机** 粘贴运行 → 脚本 **只从 GitHub Release** 下载 → 装完 **归属当前用户**。执行机 **不装 Studio**。

**铁律：** Nexus **不提供**安装脚本、**不转发** zip、**不**做「先下 bootstrap 再转给 Scout」的任何 HTTP 安装路径。Nexus 只负责 **发 token**（`POST install-token`）和 **REGISTER 时验 token、写归属**。

**状态：** Phase 1 进行中（`bootstrap.sh` + Studio「复制远程安装命令」已落地；需打 Scout tag 后执行机才可 curl GitHub）。

---

## 1. 用户怎么用

| 步骤 | 位置 | 操作 |
|------|------|------|
| 1 | 开发机 **Studio**（已登录） | Scout 节点页 → **复制安装命令** |
| 2 | **执行机**终端 | 粘贴整行回车 |
| 3 | Studio | 刷新节点列表 → 新节点 online，归当前用户 |

执行机需能访问 **GitHub**（下 bootstrap + zip）和 **Nexus**（Scout 连 `/node`），**不需要**访问 Studio。

---

## 2. 复制到剪贴板的内容（唯一形态）

Studio 拼 **一行 shell**，其中：

- **脚本 URL**：GitHub **固定 tag** 上的 `bootstrap.sh`（与 manifest 里当前推荐版本一致，**不用** `latest` 漂移到未知版本）。
- **凭证与归属**：通过 `bash -s --` 传给 bootstrap 的参数（**不**经过 Nexus URL）。

示例（版本号由 Studio 读 manifest 填入）：

```bash
curl -fsSL 'https://github.com/pengchengluoyi/MinoScout/releases/download/v0.1.20/bootstrap.sh' | bash -s -- \
  --token '【安装凭证】' \
  --nexus-url 'http://mino.local:10104' \
  --studio-id '【工作台ID】'
```

| 参数 | 来源 |
|------|------|
| `bootstrap.sh` 的 tag | Studio `getScoutLatestRelease()` → `version` → `releases/download/v{version}/bootstrap.sh` |
| `--token` | Studio `POST /runtime/nodes/install-token` 返回的 `token` |
| `--nexus-url` | 同上响应里的 `nexus_url`，或 Studio `nexusOrigin()` |
| `--studio-id` | 当前工作台 id（与 today 写 `config.json` 一致） |

**UI 必须提示：** 命令里含临时凭证（约 15 分钟有效），勿发到公开渠道；凭证校验在 Scout **REGISTER** 时由 Nexus 完成，**不是**下载脚本时。

**Windows（二期）：** 同 tag 的 `bootstrap.ps1`，Studio 复制 `irm … | iex` 等价命令。

---

## 3. 执行机上的数据流（不经过 Nexus 安装）

```text
curl GitHub …/vX.Y.Z/bootstrap.sh | bash -s -- --token … --nexus-url … --studio-id …
  → bootstrap.sh（GitHub 静态文件）
  → curl GitHub manifest.json（默认同 tag 或 bootstrap 内写死的 manifest URL）
  → 下载 combined / 分层 zip（仍只从 GitHub）
  → 解压，执行包内 packaging/install.sh
  → 写 $PREFIX/config.json（nexus_url、token、studio_id）
  → launchd / systemd 启动 mino-scout
  → 脚本退出；Scout 进程由服务管理，关终端不影响
  → Scout dial Nexus REGISTER（Nexus 验 token → owner_user_id + studio_id）
```

**Nexus 在整个安装阶段只有两类参与：**

1. Studio 事先 `POST /runtime/nodes/install-token`（浏览器会话）。
2. 装完后 Scout WebSocket **REGISTER**（执行机 → Nexus）。

---

## 4. Nexus 职责（无安装脚本）

### 4.1 保留

```http
POST /runtime/nodes/install-token
→ { "token", "expires_at", "ttl_sec", "nexus_url" }
```

`issue(user_id=当前登录用户)` → REGISTER 时 `owner_user_id`（已有）。

### 4.2 明确不做

| 不做 | 原因 |
|------|------|
| `GET /runtime/nodes/scout-install.sh` | 用户要求：安装流量不走 Nexus |
| Nexus 代理 GitHub zip / bootstrap | 铁律：二进制只在 GitHub Release |
| `GET /releases/scout/latest` 给安装用 | 客户端（Studio）直读 GitHub manifest；Nexus 代理仅可选兼容 |

### 4.3 后续（与安装无关）

- REGISTER 分配 `node_id`、长期 `node_token`（§8）
- Console PATCH 改归属（§9）

---

## 5. Studio 页面（待实现）

### 5.1 按钮

**「复制安装命令」**（专机安装）— 与「本机下载并安装」（Electron `scoutSetup`）并列。

Web 版 Studio 只要已登录 Nexus，同样可以复制命令；**执行机不需要 Studio**。

### 5.2 生成逻辑

```javascript
async function copyInstallCommand() {
  const rel = await getScoutLatestRelease({ os: 'darwin' }) // 或 hostPlatform
  const ver = rel.data?.version || rel.version
  if (!ver) throw new Error('没有可用的 Scout 发布版本')

  const { token, nexus_url } = (await createScoutInstallToken()).data
  const studio_id = await resolveStudioId()
  const owner = scoutManifestUrl() // 解析 GitHub owner/repo
  const bootstrap = `https://github.com/${owner}/MinoScout/releases/download/v${ver}/bootstrap.sh`
  const nexus = (nexus_url || nexusOrigin()).replace(/\/$/, '')

  // token 中单引号需转义
  const esc = (s) => String(s).replace(/'/g, "'\\''")
  const line = [
    `curl -fsSL '${bootstrap}' | bash -s -- \\`,
    `  --token '${esc(token)}' \\`,
    `  --nexus-url '${esc(nexus)}' \\`,
    `  --studio-id '${esc(studio_id)}'`,
  ].join('\n')

  await copyToClipboard(line)
}
```

`bootstrap.sh` 的 tag **与 Studio 展示「最新包 vX」同源**，避免命令装 A 版、UI 显示 B 版。

### 5.3 归属

| 字段 | 机制 |
|------|------|
| `owner_user_id` | install-token 绑定的 `user_id` |
| `studio_id` | bootstrap 写入 config → REGISTER |

**谁复制命令，节点归谁。**

---

## 6. MinoScout Release（GitHub）

每个 `v*` tag 附件（CI 增加）：

| 文件 | 说明 |
|------|------|
| `manifest.json` | 已有 |
| `MinoScout-*.zip` / 分层 zip | 已有 |
| **`bootstrap.sh`** | 新增；源码 `packaging/bootstrap.sh` |

`bootstrap.sh` 接口：

```text
--token          必填
--nexus-url      默认 http://mino.local:10104
--studio-id      可选
--manifest-url   可选；默认 https://github.com/.../releases/download/v{SCOUT_VERSION}/manifest.json
                 （构建时把 SCOUT_VERSION 打进 bootstrap，与 tag 一致）
```

行为：拉 manifest → 下 zip → `install.sh` → 写 config → **仅**注册并启动系统服务。

升级：`mino-scout update`（本机，仍只 curl GitHub）。

---

## 7. 进程模型与 configure（简要）

- 安装后只跑 **系统服务**；裸 `mino-scout` 在服务已运行时 **只打 status/版本**，不启第二进程、不泄露 token。
- 停止：`mino-scout stop`。
- CLI 仅 `mino-scout configure nexus-url …`；不可 CLI 改 token / scout_id。

---

## 8. scout_id 与 token 生命周期

- **scout_id**：Nexus 首次 REGISTER 分配并下发，Scout 写 `config.json`（待协议）。
- **token**：安装凭证短期有效；REGISTER 成功后换 **长期 node_token** 写 config（待实现）。bootstrap **不**向 Nexus 请求脚本，只把 Studio 给的 token 写入 config 供 Scout 使用。

---

## 9. Chromium 升级

发版锁 `playwright`；runtime 指纹含 chromium revision；planner 无 revision 变化 **不**下 browser 层；禁止 browser-only 增量。

---

## 10. Console 改归属

安装默认归复制命令的用户；Console **PATCH** 节点 `owner_user_id` / `studio_id`（后续），非安装主路径。

---

## 11. 实施顺序

| # | 仓库 | 交付 |
|---|------|------|
| 1 | MinoScout | `packaging/bootstrap.sh` + release.yml 上传同 tag |
| 2 | MinoStudio | 「复制安装命令」§5.2 |
| 3 | MinoScout | bootstrap 内 manifest/zip 全 GitHub；CLI/update/Chrome 规则 |
| 4 | MinoNexus | node_token + REGISTER 发号（**无**安装 HTTP） |

---

## 12. 验收

1. 抓包/日志：安装阶段 **无** 对 Nexus 的 zip/bootstrap 下载，仅有 GitHub + 最后 Scout WS。
2. Studio 复制的 tag 与装的 `config.version` / manifest 一致。
3. 用户 A 复制 → 执行机安装 → 节点仅 A 可见（非管理员）。
4. token 过期后 REGISTER 失败，安装本身仍可下 GitHub（需 UI 提示重新复制命令）。

---

## 13. 相关

- `mino_nexus/services/runtime_tokens.py`
- `MinoStudio/src/api/runtime.js` — `createScoutInstallToken`、`getScoutLatestRelease`
- `../MinoScout/packaging/install.sh`
