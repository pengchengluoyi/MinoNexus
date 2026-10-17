## 安装正式版本

**[打开下载与安装页面 →](https://pengchengluoyi.github.io/MinoNexus/)**

选择系统与组件，下载安装器，或一键复制下载链接和安装命令。

GitHub 下载和安装仅提供正式版本，没有测试环境选项。安装菜单只选择 Nexus、Workbench 或两者；包由 GitHub Actions 原生编译并发布到 Releases，用户电脑不编译源码。

支持 Linux x64、Windows x64、macOS Apple Silicon 与 Intel。以下入口可直接安装最新正式版本：

```bash
curl -fsSL https://github.com/pengchengluoyi/MinoWorkbench/releases/latest/download/install.sh | bash
```

Windows 在 [统一安装器 Releases](https://github.com/pengchengluoyi/MinoWorkbench/releases/latest) 下载 `MinoInstaller-win32-x64.exe` 运行，或在 PowerShell 执行：

```powershell
irm https://github.com/pengchengluoyi/MinoWorkbench/releases/latest/download/install.ps1 | iex
```

默认安装目录是 `~/Mino/nexus`、`~/Mino/workbench`。Nexus 地址为 `http://127.0.0.1:50104`，Workbench 地址为 `http://127.0.0.1:60104`。Workbench 安装完成后默认打开浏览器；以后可运行 `mino-workbench open`，安装时添加 `--no-open` 可关闭自动打开。Workbench 默认连接本机 Nexus，可用 `--nexus-url` 指定远程地址。无需安装 Python 或 GitHub CLI。安装器验证下载大小和 SHA256；已有目录或端口占用时停止并保留数据。

`bootstrap.sh` / `bootstrap.ps1` 默认只安装本仓库组件；`install.sh` / `install.ps1` 提供组件选择。原生安装器还支持 `--components nexus|workbench|both`、`--download-only` 和 `--package-dir` 离线安装。Linux/Windows 当前以用户后台进程运行，不注册开机自启；可使用安装目录内 `launcher/mino-maintain`（Windows 加 `.exe`）管理启动和停止，Nexus 命令前加 `d2`。

正式安装后打开新终端，可运行 `mino-nexus start|stop|status`、`mino-workbench start|stop|status|open`；不带参数时启动服务。命令注册到用户目录，不依赖全局 Python。已激活的开发虚拟环境仍使用自己的源码入口。

测试环境只从源码／PyCharm 启动，Nexus 使用 `--dev`（50101），Workbench 使用 `npm run dev`（60101）。Scout 不监听独立业务端口，只连接对应 Nexus。

# MinoNexus

**服务端。** 与大模型交互、持有能力目录与数据、驱动执行循环、向 UI 供数。

MinoNexus 是 [MiniOrangeServer](../MiniOrangeServer) 拆分出的「大脑」那一半，与 [MinoScout](../MinoScout)（「手脚」）配对使用。

## 是什么 / 不是什么

| 是 | 不是 |
|---|---|
| UI 唯一对话对象（HTTP `:50104` + WS 推送） | **不碰设备** —— 没有 adb / WDA / Playwright / ClawNode 连接 |
| 与大模型交互的唯一一侧：决策、定位、断言 | 不做设备驱动 —— 那是 Scout 的事 |
| 能力目录（`catalog_entries`）唯一真源 | 不实现能力 —— 只声明"能做什么、用什么实现" |
| 执行循环（observe → decide → dispatch）的持有者 | 不含图像算法 —— CLIP / OCR / 组件检测在拆分中已废弃 |
| 数据归属方：用例、任务、设备、知识、覆盖度 | 不含设备状态的权威副本 —— 那来自 Scout 的心跳 |

**一句话边界：MinoNexus 决定"下一步做什么"，然后让 Scout 去做。**

## 快速开始

```bash
uv sync                  # 或 pip install -e .
mino-nexus               # :50104，并注册 mino.local
```

文档库上传 PDF 依赖 `pypdf`（已写在 `pyproject.toml`）。若报「PDF 解析需要 pypdf」，在当前 Python 环境重新执行 `pip install -e .` 后重启 Nexus。

Console / Studio / Scout 打 `http://mino.local:50104`（Nexus 启动后 mDNS 注册）。首次启动写入本地账号 `admin` / `Mino@local`（可用 `MINO_BOOTSTRAP_PASSWORD` 覆盖）。契约见 [docs/HTTP.md](docs/基础框架/HTTP.md)。

Scout 安装包不在本仓：`GET /releases/scout/latest` 只在设置了 `MINO_SCOUT_MANIFEST_URL` 时代理 GitHub 的 `manifest.json`，数据目录里不要放 zip。凭证仍走 `POST /runtime/nodes/install-token`。

Nexus 必须能在**没有任何 Scout 节点**的情况下正常启动 —— UI 上明示「无可用执行节点」，而不是整个后端起不来。

单机完整链路：

```bash
mino-nexus                                                    # 终端 1
mino-scout --nexus ws://mino.local:50104/node --token <tok>     # 终端 2
```

## 架构一句话

```
Electron UI ──HTTP :50104──► MinoNexus ──WS /node──◄ MinoScout ──► 设备
                             循环 / LLM /             (Scout 主动连)
                             能力目录 / DB
```

执行循环在 Nexus：每一步 `EXECUTE screenshot` 取屏幕 → 本地跑 LLM 决策 → `EXECUTE` 派给 Scout。
因此 trace 天然写在 UI 要读的这个进程里，`agent_stream` 与前端接口无需改造。详见 [docs/ARCHITECTURE.md](docs/基础框架/ARCHITECTURE.md)。

## 文档

| 文档 | 内容 |
|---|---|
| [CLAUDE.md](CLAUDE.md) | 硬约束、目录与命名约定、改动前必读 |
| [docs/HTTP.md](docs/基础框架/HTTP.md) | Console / Studio 登录、节点、Scout 安装契约 |
| [docs/ARCHITECTURE.md](docs/基础框架/ARCHITECTURE.md) | 四层套娃、循环、`RouterProxy` |
| [docs/PROTOCOL.md](docs/基础框架/PROTOCOL.md) | 五条消息的字段级定义（与 Scout 同一份） |
| [docs/CAPABILITY_CATALOG.md](docs/基础框架/CAPABILITY_CATALOG.md) | 能力目录三层抽象、怎么加一条能力 |
| [docs/NODE_REGISTRY.md](docs/基础框架/NODE_REGISTRY.md) | `node_id` / `sn` / 设备归属与派单 |
| [docs/PROMPTS.md](docs/基础框架/PROMPTS.md) | 各 job 的 prompt 归属与铁律 |
| [docs/DATA_MODEL.md](docs/基础框架/DATA_MODEL.md) | 数据模型与归属 |
| [docs/DOC_LIBRARY.md](docs/基础框架/DOC_LIBRARY.md) | 文档库：PDF/Markdown 上传与全文检索 |
| [docs/9月12日_AppIntel信息基座.md](docs/9月12日_AppIntel信息基座.md) | 导航 + 知识 + 文档库统一信息基座（AppIntel，规划） |
| [docs/CONVENTIONS.md](docs/基础框架/CONVENTIONS.md) | 日志、异常、硬约束约定 |
| [docs/MIGRATION.md](docs/基础框架/MIGRATION.md) | 从 MiniOrangeServer 搬哪些文件、怎么改 |

## 守门

硬约束见 [CLAUDE.md](CLAUDE.md) §1：不碰设备、不含图像算法、不 import Scout、协议 fixture 两仓一致。

## 测试与正式环境

| 组件 | 测试 | 正式 |
|---|---|---|
| Nexus | 50101 | 50104 |
| Workbench | 60101 | 60104 |
| Scout | 主动连接测试 Nexus 的 50101 | 主动连接正式 Nexus 的 50104 |

Scout 不分配独立业务监听端口。测试与正式使用独立的数据目录、凭据和节点身份。已安装实例的配置不会因更新默认值而自动迁移。

开发与正式部署隔离及启动方式见 [环境说明](docs/ENVIRONMENTS.md)。

### 已有安装升级

从 0.1.45 起，重新运行 GitHub Release 的安装器会识别已有安装，先下载校验再停服升级；支持 Nexus 0.1.43 / 0.1.44 基线。使用原安装目录和原 Windows 登录用户，勿删除 `Mino` 目录。`--components both` 依次更新 Nexus 和 Workbench，每个组件独立保留恢复记录；并非跨组件原子事务。

升级保留账号、数据目录、配置、旧应用与启动器；Nexus 在停止写入后备份全部数据，在副本上重复验证迁移。活动任务或未完成维护会阻止升级。离线迁移失败时恢复停服前数据；开始启动新版后不自动回滚数据库，防止丢失新写入。启动检查中断可重新运行同一目标安装器；其他中断保留 `updates` 下的日志和备份，禁止删除账本绕过。Windows 原有权限错误不会通过修改 ACL 或改用另一个用户自动修复。
