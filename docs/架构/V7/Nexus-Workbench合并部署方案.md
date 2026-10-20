# Nexus + Workbench 合并部署方案

> 建立：2026-10-19。状态：**B1/B2/B3/B5/B6/B7 代码已实现，发布与实机验收待完成；B4 暂缓**；本文同时记录已落地的 GitHub Actions 变更与尚未实施的代码待办，二者不得混淆。
> 前端架构约束沿用 CLAUDE.md：Mino 只有 MinoWorkbench 一个前端；Scout 仍是独立执行节点。

## 当前实施状态（2026-10-20）

B1/B2/B3/B5/B6/B7 的源码已实现；B4 按用户要求暂缓。旧原生安装器、自更新、维护门禁已按用户授权移除，现场服务与数据未改动。

用户进一步要求移除非必要发布门禁，现以本节覆盖下文原始发布设计：

- Nexus CI/main/PR 直接构建 Workbench main → 内置网页的 wheel → 上传 Actions artifact。Nexus v* tag 发布 wheel/sdist、双架构镜像与离线镜像包。
- Workbench main/PR 构建并上传静态包，v* tag 发布静态包。Nexus 不再依赖该静态 Release，不再需要 web.lock 或 lock_web.py。
- tag 直接决定发布版本，不再要求手工同步源码版本；去掉重复安装烟测、容器登录/重建验收、lint 发布阻断和重复校验门禁。
- 实际编译和打包失败仍然失败；保留容器 HEALTHCHECK、下载用校验清单和来源 commit 记录。诊断脚本仅供按需手动使用。
- 默认 Workbench ref 为 main；同一 Nexus tag 重跑可能使用更新的前端 commit，不再承诺原锁文件方案的精确复现。需要固定时可指定 checkout ref / Docker WORKBENCH_REF。

本地已完成前端构建、类型检查、Python 语法/内部导入和完整 wheel 打包检查。未运行 pytest。Docker、launchd、实际浏览器、Scout 跨机重连与存量迁移仍待实机验收；代码实现不等同于这些验收通过。

GitHub 生效以提交并推送的工作流为准，未推送的本地修改不会改变线上触发规则。

## 1. 决定

1. **Nexus 与 Workbench 部署在同一台机器上，合成一个部署单元**：一个产物、一个进程、一个端口（正式 50104）、一个版本号。Workbench 的静态文件由 Nexus 同源托管，不再有独立的 60104 服务。
2. **按"本地服务"而不是"桌面软件分发"来部署**：用 Python 包（wheel）安装，由 launchd（macOS）常驻；**不再用 PyInstaller 冻结，不再有服务自更新引擎。** 升级 = 装新版本 + 重启，启动时自动迁移；B4 自动备份暂缓，升级前需另行备份。
3. **Scout 不改**：仍是装在连设备机器上的原生安装包，经 `ws://<nexus>:50104/node` 主动连 Nexus。协议、`protocol.py`、两仓 fixture 不动。
4. **同时支持 Linux Docker 部署**：同一个 wheel 另打成容器镜像，供 Linux 服务器使用（§4.5）。macOS 本机走 wheel + launchd，Linux 服务器走 Docker，两条路线共用同一套代码待办 B1～B4，镜像内同样没有自更新引擎。

## 2. 为什么改（现状问题）

现状：push main 即跑四平台（linux/win/mac arm/mac intel）PyInstaller 矩阵，产出冻结二进制 + `MinoInstaller` + `mino-maintain`；服务器端靠 `update_engine` 自查 GitHub、下载、停写、备份、迁移、切换、回滚。

| # | 问题 | 依据 |
|---|---|---|
| 1 | 把服务当桌面软件分发，重造 systemd/docker 已有的升级、回滚能力 | `update_engine` / `installer_upgrade` / `maintain` 合计 1000 多行 |
| 2 | 更新强依赖 GitHub 公网，仓库转私有、内网、离线、被限流即失败 | `github_release.py` 仅支持公开 Release，无 token；`bootstrap.sh` 写死个人仓库 `pengchengluoyi/MinoNexus` |
| 3 | PyInstaller 冻结服务端：`--collect-all litellm/…` 靠试错堆出，依赖一变可能静默缺文件，且无 lockfile 级复现 | `scripts/build_release.py` |
| 4 | Workbench 是静态前端却被冻成 Python 二进制，另起服务、另走更新链路，还要"声明兼容的 Nexus build"，且仅支持同机 | `MinoWorkbench/packaging/workbench_server.py`；`docs/github-updates.md` |
| 5 | CI 既是构建又是验收，约 230 行内嵌 Python，本地无法复现；升级基线写死 `0.1.43`，要从公网下载旧发布物，会腐烂 | `.github/workflows/release.yml`（改前） |
| 6 | 发布链路重复：release.yml 在 tag 自己发布，promote.yml 又凭 `run_id` 把 main 产物提升；Actions 产物过期后无法提升 | `promote.yml` |
| 7 | 兼容性要对每一对 `from--to` 版本做演练，复杂度随版本数平方增长 | `config/compatibility/<from>--<to>.json` |

## 3. 目标形态

```
一台 Mac（或任意单机）
├── Nexus 进程（mino-nexus，端口 50104）
│   ├── HTTP API / WS /ws（Workbench）/ WS /node（Scout）
│   ├── 同源托管 Workbench 静态文件：GET /  → web/index.html
│   └── 数据：~/Library/Application Support/Mino/nexus/data/（mino.db 等）
└── 浏览器 → http://127.0.0.1:50104/   （同源，无跨域）

另：Scout（原生安装，各自连设备）──ws──▶ Nexus :50104/node
```

- **一个版本号**：Nexus 的 `mino_nexus/VERSION`。Workbench 版本记录在包内 `web/BUILD.json`，仅用于排障，不参与兼容准入（二者永远同版本，不存在不兼容组合）。
- **数据目录不变**：`~/Library/Application Support/Mino/nexus/data/`，升级不搬数据。
- **端口**：正式 50104；开发 50101（源码启动，Vite 开发服务器仍用 60101，不受影响）。

## 4. 部署手册

### 4.1 正式安装（macOS 本机，目标形态）

> 以下命令需使用已经内置 Workbench 的正式 wheel；Nexus 会直接构建 Workbench 源码，B4 自动备份暂缓。

```sh
# 1) 安装（Release 附件里的 wheel，已内置 Workbench 静态文件）
pipx install https://github.com/<owner>/MinoNexus/releases/download/vX.Y.Z/mino_nexus-X.Y.Z-py3-none-any.whl
#    校验：与 Release 里的 SHA256SUMS.txt 对照

# 2) 注册为 launchd 后台服务（B3：由 mino-nexus 生成 plist，不手写）
mino-nexus service install      # 写 ~/Library/LaunchAgents/com.mino.nexus.plist 并加载
mino-nexus service status

# 3) 打开界面
open http://127.0.0.1:50104/
```

要点：

- 监听地址：局域网内要让其他机器的 Scout 连上，须监听 `0.0.0.0`；仅本机使用可用 `127.0.0.1`。生产模式默认 `0.0.0.0`（与现行为一致）。
- Scout 连接地址：`ws://<这台机器的地址>:50104/node`；本机 Scout 用 `127.0.0.1`。
- 数据目录与日志目录由 `service install` 创建；不得与开发数据根 `~/.mino-dev/` 重叠。

### 4.2 升级与回滚

升级（不再有自更新）：

```sh
pipx install --force https://github.com/<owner>/MinoNexus/releases/download/vX.Y.Z/mino_nexus-X.Y.Z-py3-none-any.whl
mino-nexus service restart
```

B4 原设计为启动迁移前在线备份；按用户要求本次暂缓，当前仅执行已有迁移后监听，不新增此通用备份保证。升级前须另行安排备份。

回滚：

```sh
mino-nexus service stop
pipx install --force <旧版本 wheel>
# 仅当新版本已执行过迁移且旧版本无法读取时，才从备份恢复 mino.db
mino-nexus service start
```

迁移是只增不改（additive ALTER），旧版本通常可直接读取新库；恢复备份是最后手段，且会丢失升级后写入的数据，操作前必须确认。

### 4.3 开发环境（不变）

源码启动：`mino-nexus --dev`（50101，仅 localhost，数据 `~/.mino-dev/nexus`）；Workbench 用 `npm run dev`（60101）连 50101。开发与正式互不读取对方目录、端口、变量。

### 4.5 Linux Docker 部署

> Dockerfile、Compose 与镜像 job 已写入源码，镜像尚未实际构建发布；使用前完成构建与部署验收。

**镜像获取（三选一）**

| 方式 | 做法 | 适合 |
|---|---|---|
| registry 拉取（主推） | tag 构建后推 `ghcr.io/<owner>/mino-nexus:X.Y.Z`；服务器 `docker compose pull && docker compose up -d` | 服务器可访问 ghcr；私有仓库用 token `docker login` |
| 现场构建 | 服务器 clone 仓库，`docker compose build` | 临时试用 |
| 离线镜像包 | Release 附 `mino-nexus-X.Y.Z-linux-<arch>.tar.gz`（`docker save`）；服务器 `docker load` | 内网、离线 |

**镜像要点（`Dockerfile`）**

- 基于 `python:3.12-slim`，安装本仓构建出的 wheel（已内置 Workbench 静态文件），不从源码目录运行。
- 非 root 用户运行；`EXPOSE 50104`；`VOLUME /data`；`HEALTHCHECK` 请求 `/health`。
- 入口为生产模式、监听 `0.0.0.0:50104`、数据目录 `/data`、`--no-mdns`（具体参数以 B1 定稿为准）。
- 架构：`linux/amd64`、`linux/arm64`。

**`docker-compose.yml` 要点**

- `ports: "50104:50104"`；`volumes: mino-data:/data`；`restart: unless-stopped`。
- AI 配置遵守当前 AGENTS.md §8：通过 Workbench 按账号保存，不启用已废弃的 MINO_AI_* 共享密钥覆盖；密钥不写进镜像或 Compose。
- 镜像 tag 钉死具体版本，不用 `latest`。

**升级与回滚**

```sh
# 升级：改 compose 中的 tag 后
docker compose pull && docker compose up -d
# 回滚：改回旧 tag
docker compose up -d
```

- 升级前先备份数据卷（停容器后复制卷内容，或使用 B4 的启动前自动备份，备份落在 `/data/backups/`）。迁移只增不改，旧版本通常可直接读新库；恢复备份会丢失升级后写入的数据，须确认。
- 同一数据卷不得被两个容器同时挂载。

**限制**

- mDNS 在容器内基本不可用，Scout 用 IP 或域名连 `ws://<服务器>:50104/node`，不能依赖 `mino.local`。
- `/ws` 的 origin 校验须放行用户访问 Nexus 所用的地址（含反向代理域名），属 B2 验收项。
- 对外暴露到公网时应放在反向代理（TLS）之后，不直接裸露 50104。

### 4.4 存量安装迁移

已装有 0.1.4x 冻结包（`~/Library/Application Support/Mino/{nexus,workbench}` 或旧 `~/Mino/…`）的机器：

1. 先 `mino-nexus stop`/旧 `mino-maintain` 停止 Nexus 与 Workbench 两个服务，**停写**。
2. 用 SQLite 在线备份（不要直接复制运行中的主文件）备份 `nexus/data/`。
3. 复制到**新目录**，新版本指向新目录启动并验收（登录、历史数据、Scout 重连）。
4. 旧目录保留供回滚，验收稳定后再人工清理。**不原地覆盖，不直接移动目录。**

## 5. Workbench 同源托管设计

现状：Workbench 在 `src/lib/config.ts` 中按顺序取 `window.__MINO_CONFIG__.nexusUrl` → 构建期 `VITE_NEXUS_URL` → 默认 `http://mino.local:50104`；独立服务 `workbench_server.py` 在 HTML 里注入前者。

合并后：

1. **构建**：Workbench 以 `npm run build -- --base ./` 产出静态目录，附带 `BUILD.json`。
2. **打进 wheel**：作为 `mino_nexus/web/` 包数据（`pyproject.toml` 的 `package-data` 增加 `web/**`）。一个 wheel = 完整产品。
3. **Nexus 托管**：
   - `GET /` 返回 `web/index.html`，并注入 `window.__MINO_CONFIG__ = {nexusUrl: <请求 origin>, buildId}`；其余静态资源走 `/assets/*` 等。
   - 路径冲突规则：API 路由先注册，静态托管最后注册并带 SPA fallback；`/ws`、`/node`、`/health`、`/static`（上传文件）不被覆盖。
   - 同源后 Workbench 的 API 与 WS 都走 `location.origin`，**满足"origin 编译进前端、不做发现"约束**。
4. **跨域与 origin 校验**：`app.py` 当前 `CORSMiddleware allow_origins=["*"]`；同源后可收紧为空或仅开发源。`client_gate` 与 `/ws` 的 origin 校验需确认同源场景放行（B2 的验收项）。
5. **取舍**：Nexus 发布直接构建 Workbench 源码。当前按用户简化要求，Nexus 构建时直接获取 Workbench main 源码并构建，不依赖前端 Release 或 web.lock；BUILD.json 记录实际 commit。需要固定源码时显式指定 ref。

## 6. GitHub Actions 变更

### 6.1 Nexus 仓（本次已落地）

| 文件 | 变更 | 作用 |
|---|---|---|
| `.github/workflows/ci.yml` | **新增** | push main / PR / 手动：ubuntu + macOS 上构建 wheel → 在**干净 venv** 装 wheel（cwd 不在仓库内，保证测的是安装后的包）→ 启动隔离数据根的 `mino-nexus --prod` → 烟测 |
| `scripts/ci_smoke.py` | **新增** | 烟测逻辑从 YAML 抽出，**本地可直接运行**：包数据完整（VERSION/catalog/prompts）、`/health`、`/sys/server_info`、带 prefix 的 router 在（`/auth/status` 非 404，防 CLAUDE.md §7.2 静默失效）、未登录 `/ws` 被 1008 拒绝 |
| `.github/workflows/release-wheel.yml` | **新增** | 仅 `v*` tag：校验 tag 与 `VERSION` 一致 → 构建 wheel/sdist → 再跑一遍烟测 → 生成 `SHA256SUMS.txt` → 发布到 Release |
| `.github/workflows/release.yml` | **已删除** | 依用户后续授权直接清理旧原生发布源码，现场安装未改动 |
| `.github/workflows/promote.yml` | **删除** | 它只能提升 main 上 `release.yml` 的产物，而该产物不再在 main 上构建；tag 发布本就在 release.yml 内完成 |
| `.github/workflows/release-wheel.yml` 增加 `image` / `publish-image` job | **代码已实现，待运行** | 依赖 B1。tag 上用 buildx 构建 amd64/arm64 镜像，先在本机 load 后启动容器、运行 `scripts/ci_smoke.py`（生产模式）通过，再推送 ghcr；同时 `docker save` 出离线包附到 Release |
| `.github/workflows/pages.yml`、`site/` | 下载页已更新，发布方式保留 | 指向 wheel / Docker / Scout，尚未发布网页 |

原计划保留旧通道的原因（现已按用户授权清理源码，现场旧安装未动）：存量 0.1.4x 实例通过 `manifest.json` 与安装器检查更新，立刻停发会让它们收不到版本、`bootstrap.sh` 链接失效。因此是**先加新、后拆旧**。

### 6.2 Workbench 仓（静态发布已实现，待实际发布）

Workbench 的 `release.yml` / `promote.yml` 与 Nexus 旧版相同。合并后应改为：

- 去掉 PyInstaller 与四平台矩阵；
- push/PR 仅做 `npm ci && npm run lint && npm run build`；
- `v*` tag 发布 `workbench-web-X.Y.Z.tar.gz` + `SHA256SUMS`，供 Nexus 的发布工作流固定版本拉取（§5.5）。

## 7. 原始代码事项（当前状态以上方实施表为准）

| 编号 | 事项 | 说明 |
|---|---|---|
| B1 | **生产模式开关** | 非冻结安装的 `mino-nexus` 现在默认 `--dev`（50101、仅 localhost、`~/.mino-dev`）。需新增生产模式（如 `--prod` 或 `service` 子命令传入），使用 50104、默认 `0.0.0.0`、生产数据目录，并保留"开发数据根不得与生产重叠"的校验 |
| B2 | **同源托管 Workbench** | §5：静态挂载、`/` 注入运行时配置、SPA fallback、收紧 CORS、核对 `client_gate` 与 `/ws` origin |
| B3 | **`mino-nexus service install/start/stop/restart/status`** | macOS 生成并加载 launchd plist（`deployment_cli.py` 已有 launchd 相关逻辑可复用）；Linux 可后置 |
| B4 | **启动前自动备份** | 迁移前对 `mino.db` 做 SQLite 在线备份，失败则不监听 |
| B5 | **拆除自更新** | 切换完成后删除 `update_engine`、`installer_upgrade`、`maintain`、`workbench_updates`、`bind-workbench`、兼容性演练矩阵及对应 `rReleases` 入口；**保留** `services/scout_install.py` 及 Scout 下载/更新相关入口 |
| B7 | **Docker 交付物** | 新增 `Dockerfile`、`docker-compose.yml`、`.dockerignore` 与 §6.1 的 image job；依赖 B1（生产模式、数据目录参数）与 B2（镜像内含 Workbench） |
| B6 | **前端更新入口调整** | Workbench 只读展示"有新版本"提示（可选），不再触发在线更新 |

## 8. 切换顺序与验收

1. **加新（可立即做）**：合入 §6.1，`ci.yml` 在 main 上连续通过；本地运行 `python scripts/ci_smoke.py` 可复现。
2. B1 完成并另行安排升级前备份后（B4 暂缓），在一台干净 Mac 上按 §4.1 手工安装，验收：登录、Scout 重连、重复重启、迁移后数据保留。
3. B2 完成后，把 Workbench 静态包打进 wheel，验收：`/` 可打开界面、`/ws` 登录后推送正常、未登录被拒、`/node` 不受影响。
4. B7 完成后，在一台干净 Linux 上按 §4.5 拉镜像部署，验收：登录、Scout 从另一台机器重连、重建容器后数据保留、升级与回滚换 tag。
5. B3 完成后验收开机自启与 `service` 子命令幂等（重复 start/stop 不报错、状态准确）。
6. 用一台已装 0.1.4x 的 Mac 做 §4.4 存量迁移演练，保留旧目录。
7. 源码清理已按用户后续授权提前完成；现场切换仍须先验收。原计划为验收全部通过后删除 `release.yml`（legacy）与 `scripts/build_release.py`、`write_manifest.py`、`packaging/` 中安装器与维护助手，执行 B5，更新 `site/` 下载页与 `docs/github-updates.md`、`docs/local-deployment-layout.md`、`docs/基础框架/HTTP.md`。

验收标准的通用原则：以 `/health`、`/sys/server_info`、实际登录与 `session_events` 为证据；不新增 `tests/test_*.py`（CLAUDE.md §1 约束 5），烟测脚本放 `scripts/`，属于发布门禁而非单元测试。

## 9. 明确不做

- 不做多副本、滚动升级与 Kubernetes：SQLite 单实例，一个数据卷只能被一个容器挂载。
- Scout 不进容器（要访问 adb 与真机）。
- 不做跨机器的 Workbench 单独部署：Workbench 随 Nexus 走，不再支持 `--nexus-url` 指向远程 Nexus 的独立安装形态。如日后需要远程访问，通过访问 Nexus 的地址即可，界面由该 Nexus 提供。
- 不改 Scout 的分发与更新方式，不改协议。
- 不为本方案新增或修改任何单元测试。
