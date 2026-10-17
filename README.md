## 一键安装（macOS Apple Silicon）

安装包由本仓库 GitHub Actions 编译，在 [Releases](https://github.com/pengchengluoyi/MinoNexus/releases) 下载；不是用户本机编译。首次安装：

```bash
curl -fsSL https://raw.githubusercontent.com/pengchengluoyi/MinoNexus/main/packaging/bootstrap.sh | bash -s -- --environment test
```

正式环境将 `test` 改为 `production`。默认目录为 `~/Mino/<环境>/nexus`，需要系统 `curl`、`unzip` 和 `python3`，不需要 GitHub CLI 或克隆源码。已有目录或端口占用时停止，不覆盖数据、不自动换端口。使用 `--version 0.1.37` 固定版本，`--download-only` 只下载校验，`--package-dir /绝对路径` 使用包含 manifest.json 与 ZIP 的离线下载目录。

测试地址为 `http://127.0.0.1:50101`，正式地址为 `http://127.0.0.1:50104`。Workbench 默认连接本机同环境 Nexus，可用 `--nexus-url` 指定另一台电脑的 Nexus。本机安装默认只监听回环地址。

仅已发布且带新格式 manifest.json 的 Release 可一键安装；CI 构建成功但尚未发布时，脚本会报发布物缺失，不能当成安装成功。

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
