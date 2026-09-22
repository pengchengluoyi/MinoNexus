# 05 — Studio 与运维

## 1. Studio 能力矩阵

| 场景 | 浏览器 Studio | 桌面 Studio |
|------|---------------|-------------|
| 复制远程安装命令 | ✓ | ✓ |
| 本机分层安装/更新 | ✗（不能写盘） | ✓ `scout-setup` IPC |
| 远程 `node.update` | ✓（在线节点） | ✓ |
| 远程 start | ✗ | ✗（Nexus 不支持） |
| 本机 start/stop | ✗ | ✓ launchctl / IPC |

远程更新 UI（≥0.1.28 Nexus + Scout）：

- 下发 update 后轮询 `GET /runtime/nodes`；
- 读 `update_job.label` / `percent` 显示在按钮与说明行；
- 请求超时 **11 分钟** 量级（大层下载）。

## 2. 本机常用命令（macOS）

```bash
# 真实二进制（勿用已卸载的 pip mino-scout）
SCOUT="$HOME/Library/Application Support/MinoScout/bin/mino-scout"

$SCOUT status          # running、version、binary、installed_layers
$SCOUT run             # 前台常驻（调试）
$SCOUT update          # 拉 manifest 自更新
$SCOUT stop

# 服务
launchctl kickstart -k "gui/$(id -u)/com.mino.scout"
```

路径含空格时：**不要**写 `"~/Library/..."`（引号内 `~` 不展开），用 `$HOME/...` 或完整路径。

软链（安装脚本写入）：`~/.local/bin/mino-scout`。

## 3. 配置与环境变量

| 变量 | 作用 |
|------|------|
| `MINO_SCOUT_HOME` | 安装根（默认 Application Support / `.config/minoscout`） |
| `MINO_SCOUT_MANIFEST_URL` | 覆盖 GitHub manifest |
| `MINO_SCOUT_UPDATING` | 仅 install 脚本内部（inplace 更新） |
| `MINO_SCOUT_SKIP_SERVICE` | CI：只装层不注册服务 |
| `MINO_SCOUT_NO_REEXEC` | 跳过 update/restart 后的 reexec |
| `PLAYWRIGHT_BROWSERS_PATH` | launchd 指向 `bin/ms-playwright` |

`config.json` 由 Studio / bootstrap 写入；**token 明文**——专机物理安全由客户负责（企业版可规划凭据仓，当前未做）。

## 4. 日志与排障

| 位置 | 内容 |
|------|------|
| `~/Library/Logs/MinoScout/scout.log` | 标准输出 |
| `…/scout.err.log` | 标准错误 |
| Nexus `NodeWS` / `NodeRegistry` 日志 | REGISTER、504、框架事件 |

**504 远程更新**：

1. 看 Scout 日志是否在下载/安装；
2. 确认 Scout **≥ 0.1.28**（inplace stop 修复）；
3. 弱网加大等待或本机 `mino-scout update`；
4. Studio 看 `update_job` 是否卡在某一 `stage`。

**command not found: mino-scout**：

- 用 `$SCOUT` 绝对路径，或 `export PATH="$HOME/Library/Application Support/MinoScout/bin:$PATH"`。

## 5. Release 与 Nexus 的协同

- Scout 发版：**打 tag `v*`** → GitHub Actions `Release Scout` → `manifest.json` + 分层 zip。
- Nexus **无需**随 Scout 每个 patch 发版，除非：
  - 协议变更（`node.update_progress` 等）——两仓 `protocol.py` + fixture 同步；
  - HTTP 行为变更（超时、504 文案）——已含 `update_job` 字段。

Studio 构建时可烘焙 `VITE_SCOUT_MANIFEST_URL`；开发环境可用代理 manifest。

## 6. 产品边界（当前未做）

以下在架构上预留或文档化，**不是 v0.1.28 承诺**：

- Nexus 托管安装包 / 私有 CDN 一等公民
- 远程 start、fleet 钉版本、灰度发布
- 代码签名 / 公证（Gatekeeper）
- token 进 Keychain

见 [03-更新链路.md](03-更新链路.md) 失败模式与 [06-执行能力与Web槽位.md](06-执行能力与Web槽位.md) 并发预期。
