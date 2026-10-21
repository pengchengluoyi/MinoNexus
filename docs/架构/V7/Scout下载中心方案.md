# V7-07 Scout 下载中心方案

建立：2026-10-18。修订：2026-10-19。状态：首期源码已实现，隔离真实安装通过；运行环境与浏览器联调待验收。详见 [开发记录](Scout下载中心开发记录-20261020.md)。

本版把首期收成「看状态 + 飞书/Meego 异步安装」。更新、修复、LangBot 环境切换、浏览器和输入法的启动下载改造，列为后期，不在首期编码。

## 1. 结论

在 Workbench「Scout 节点」增加「下载中心」页签，管理当前选中 Scout 上的功能组件。首期六类都显示用途和状态；只有飞书 CLI、Meego CLI 提供安装。Scout 核心只给手动升级说明。Chromium、ADB Keyboard、LangBot 首期只读。

接入配置继续管账号、密钥和登录。安装成功不等于已配置、已连通。不恢复已取消的产品自动升级/回退，不新建下载平台、插件市场或通用依赖引擎。Nexus 不在服务器上替节点下载安装包。

## 2. 六类条目

依据当前源码盘点，不代表各平台已经安装验收。

| 条目 | 作用 | 现有落点 | 首期 |
| --- | --- | --- | --- |
| Scout 核心 | 节点运行 | `layers.py` 有 runtime/app 分层；`core.py` 拒绝 `node.update`；Nexus `scout_updates.py` 与 `update_engine.py` 为 `upgrade_and_rollback_removed` | 只读版本和手动安装说明。不调用残留升级函数，不拆开升级 runtime/app |
| Chromium | Web 自动化 | `heavy_deps.py` 启动后准备；`install_plan.py` / `self_update.py` 下载与 runtime 配套的浏览器层 | 只读：有没有、是否匹配当前 runtime。不改启动准备，不在页面触发安装 |
| ADB Keyboard | Android 文本输入 | `adb_ime.py` 缓存 APK，设备安装/启用是另一条路径。默认源是可变分支 URL | 只读区分「电脑未缓存 / 已缓存 / 某设备未安装」。不新增下载，不把缓存成功显示成手机可用 |
| 飞书 CLI | 飞书工作项接入 | `plugins` 中 `cli/feishu`，`needs_install=True` | 可安装。装完引导到工作项配置 |
| Meego CLI | Meego 工作项接入 | `cli/meego`，manifest 优先，否则 npm 包 | 可安装 |
| LangBot | 飞书、钉钉、企业微信对话共用一个宿主 | `langbot_host.py` 建 venv，pip 安装锁定版本 | 只读状态和前置检查。缺系统 Python 3.11+ 时说明原因。首期不安装、不更新、不切换 venv |

飞书工作项 CLI、飞书 Webhook、飞书对话是三件不同的事。页面写清工作项 CLI 不管 Webhook 和对话。

随包内容不单独做成下载卡片：Playwright 的 Python 包和 driver、浏览器附带的 FFmpeg、随包 ADB、Pillow/NumPy 等基础库、cua-driver 等可选桌面包、uv、mino-bridge。缺 ADB 二进制时提示修复或手动升级 Scout，不另做 platform-tools 下载器。

不放进本页：用例服务 API、微信、Webhook、Gmail、空的 MCP 分类、被测业务 APK、附件和邮件、Scout 首次安装包、Nexus/Workbench 发布包、CI 依赖。首次安装仍在「添加节点」。尚未启动的 Scout 不能用下载中心安装自己。

## 3. 首期页面

页签为「概览 / 下载中心 / 接入配置 / 日志」，沿用节点查询参数，增加 `pane=downloads`。一个表加一块任务区，不拆二级页。

```text
当前节点：主机名 · 系统/架构 · Scout 版本 · 在线状态
[概览] [下载中心] [接入配置] [日志]

工具              用途          状态                 操作
Scout 核心        节点运行      已安装 x.y.z         手动升级说明
Chromium          Web 自动化    已匹配 / 缺失 / 未知  无安装按钮
ADB Keyboard      Android 输入  缓存与设备分开       无下载按钮
飞书 CLI          工作项接入    未安装 / 已安装·待配置  安装 或 去配置
Meego CLI         工作项接入    同上                 安装 或 去配置
LangBot           对话渠道      未安装 / 缺 Python    无安装按钮
```

状态以节点上报为准，上表不是实况。无节点时引导添加。离线只显示最后已知状态，不能新装。旧 Scout 没有本页所需字段时，对应格显示未知，并提示需要新版本，不把旧安装命令伪装成可恢复任务。

「已安装 · 待配置」的配置状态来自原接入配置，不由安装器推断连通性。下载中心不保存密钥。

## 4. 首期只异步安装两个 CLI

现有 `POST /runtime/nodes/{node_id}/plugins/install` 会同步等待，超时 600 秒。LangBot 子进程允许更久。下载中心不复用这条长请求当任务生命周期。旧接口保持原语义，避免把「已受理」当成「已安装成功」。

首期新接口只服务 `cli.feishu` 与 `cli.meego` 的 `install`。刷新状态走读接口，不创建任务。`check_update`、`download`、`update`、`repair`、`remove`、清缓存不在首期开放。

| 接口 | 语义 |
| --- | --- |
| GET `/runtime/nodes/{node_id}/tools` | 六类状态、配置跳转、当前账号可见的活动安装任务。离线时返回本账号最后一次已落盘快照，并标过期 |
| POST `/runtime/nodes/{node_id}/tool-tasks` | 字段：`operation_id`、`tool_id`、`action=install`。返回 202 和任务快照。受理不是安装完成 |
| GET `.../tool-tasks/{task_id}` | 读已落盘任务。节点离线也可读本账号任务 |

`operation_id` 由调用方生成。同一账号、节点、工具、参数的重复提交返回原任务；参数不一致返回 409。前端不自动重试。鉴权不可见返回 404。节点离线或合同不足返回 409。同一 CLI 已有活动安装时返回 409 `tool_busy`，不排队。

互斥范围只限同一个插件的安装和它的旧安装/移除入口，不锁整台机器。Chromium 启动准备、ADB Keyboard、LangBot 不进入这把锁，所以浏览器在下载时仍可安装飞书 CLI。

Nexus 先把任务写成 `pending` 再发给 Scout。Scout 对相同 `task_id` 和参数不重复执行。应答丢失时任务保持未知，只按原 `task_id` 查询；断线不能当成「没开始」。只有 Scout 明确没有该任务、身份仍匹配、且用户再次提交同一 `operation_id` 时才重发。进程重启后不能证明已装完的任务标为中断，不自动重装。

进度沿用现有插件阶段上报，补上不会被 Nexus 白名单丢掉的 `task_id`。`_public_plugin_job` 现在会滤掉未声明字段，所以任务标识要改两端协议，并同步 `PROTOCOL.md`。百分比只在总字节已知时显示；失败也可能出现 100%，以最终结果和错误为准。下载完成之后还有校验和安装。

安装记录继续用现有插件目录和 `.installed`。旧标记只有 `ok` 时版本显示未知，不推断成最新版。首期不为只读项新建 Scout 侧工具库。飞书/Meego 任务投影用 Nexus 一张 `node_tool_tasks` 表，走现有 additive 迁移；业务真源仍是 Scout 上的安装结果，Nexus 保存派发和给页面看的快照。`persist_snapshot` 今天不存 `plugin_job`，所以不能靠节点心跳恢复安装进度。

账号目录仍按 Nexus origin + owner 隔离。机器上别人的插件任务不给当前账号看。管理员不因此获得其他账号 Scout 的执行权。账号重绑前，进行中的安装必须先结束。

接入配置里的快捷安装调用同一任务。安装结束后回到原来的配置项。配置失败不触发重装。

`plugins/install.py` 当前会删掉旧目录再换入新文件，`remove` 会删目录并清密钥。首期安装走现有首次安装路径即可。后期若做更新，禁止用 remove 再 install 来代替。

部署顺序：Nexus 先认识新路由和可选协议字段，Scout 再上报并能执行，Workbench 按实际上报显示安装按钮。对不齐时降级成只读，不用版本号猜测能力。

## 5. 后期，本文件不冻结实现

下面这些留在边界里，避免做首期时顺手打开，也避免以后用重装冒充更新。

| 后期项 | 现在守住的边界 |
| --- | --- |
| 工具更新与修复 | 先有经验证的固定版本和摘要。不用可变 URL 或 latest 当兼容目标。更新失败时旧版本仍显示已安装 |
| Chromium | 只跟当前 Scout runtime 的 Playwright revision。需要换 runtime 时走 Scout 手动升级，不在后台替换核心。启动准备是否改成按需下载，另做决定 |
| ADB Keyboard | 发布条目提供固定提交和摘要之后才允许下载中心更新。电脑缓存更新不自动升级所有设备。现有自动准备保持原样，并标成旧路径 |
| LangBot | 三个渠道共用一个宿主，不能装三份。data、配置、密钥与 venv 分开；切换前停宿主。数据迁移没有验证过的恢复办法时，不做一键更新。缺 Python 只提示，首期不下载 Python 发行版 |
| 取消、卸载、清缓存、断点续传 | 首期没有入口。`download_file` 没有 Range。取消请求不等于已经取消 |
| Scout 核心升级 | 保持关闭。下载中心不恢复自动升级/回退 |

业务缺依赖时返回明确原因和本页定位。用户已经触发的启动准备可以继续。普通执行请求不附带安装或升级权。Mino Bots 同样不在失败分支里调用本页的安装。

## 6. 改动落点

| 仓库 | 首期 |
| --- | --- |
| Scout | 汇总六类只读事实；飞书/Meego 安装转入后台任务，短命令内返回受理；进度带 `task_id`；与旧安装入口互斥。不改 `heavy_deps`、`adb_ime`、`langbot_host` 的安装策略 |
| Nexus | 鉴权、任务表、转发和快照。请求模型写在模块级。不在服务器下载节点工具 |
| Workbench | `NodesPage` 增加页签和 `NodeDownloads`。飞书/Meego 走新任务接口；旧节点的接入配置保持现有安装流程和未知结果防重。活动时沿用约 2 秒、闲时约 15 秒轮询，不新增前端 WebSocket |
| 协议 | 两仓 `protocol.py` 与 `PROTOCOL.md` 同步最小命令、进度字段和旧客户端缺字段行为 |

## 7. 验收

在隔离账号、节点和临时数据目录做真实安装，记录 `task_id`、节点版本和阶段。不以静态页面或 mock 代替。不跑 pytest，不改真实业务库来造安装状态。

首期通过标准：六类用途和状态可见；核心没有升级按钮，自动升级入口仍拒绝；只有飞书和 Meego 能安装；短请求返回后页面能看到进度和失败原因；失败不显示成功；重复点击不第二次安装；刷新和切换节点不串任务；未知应答不自动换新 ID 重发；已安装未配置与未安装区分开；LangBot 缺 Python 时有明确只读说明；离线或无权限不能下发；Chromium 仍按原启动策略准备，不被这次改造停掉。
