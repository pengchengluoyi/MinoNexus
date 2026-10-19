# V7-07 Scout 下载中心方案

建立：2026-10-18；修订：2026-10-19。状态：方案完成，未实施。范围：代码盘点、下载安装更新流程、模块维护与最小接入，不建设新的下载平台。

## 1. 结论

在 Workbench「Scout 节点」中增加「下载中心」页签，管理选中 Scout 电脑上的组件。首期纳入 **Chromium、ADB Keyboard、飞书 CLI、Meego CLI、LangBot** 五类功能依赖，以及一个「Scout 核心」更新条目。

页面回答：这个工具做什么、哪些功能需要它、是否装好、下载进度怎样、失败怎么办。接入配置继续负责账号、密钥和登录。复用现有安装器和进度通道，不新增独立下载服务、通用依赖引擎、插件市场或发布系统。

## 2. 代码盘点：需要纳入的下载项

路径相对对应仓库；以下依据当前源码，不代表已逐个平台完成安装验证。

| 页面条目 | 对应功能 | 现有代码和下载方式 | 首期处理 |
| --- | --- | --- | --- |
| Scout 核心 | 节点运行、基础执行 | Scout `scripts/layers.py` 定义 runtime/app，保留分层与下载代码；但 `core.py` 拒绝 node.update，Nexus `scout_updates.py:request` 也已关闭入口 | 展示版本与手动安装升级说明，不恢复旧自动升级/回退；runtime/app 不提供任意拆开升级 |
| Chromium 浏览器组件 | Web 自动化 | `heavy_deps.py` 启动后后台准备；`install_plan.py` 处理 browser 与 runtime 配套；`self_update.py:apply_plan_step` 下载 | 必须纳入：安装状态、后台进度、缺失安装及失败重试 |
| ADB Keyboard 输入法 | Android 文本输入 | `adb_ime.py:ensure_apk_cached` 调用 `executors/adb_command.py:_download_apk`；`ensure_adb_keyboard` 执行设备准备；默认源为 ADBKeyboard GitHub 项目 | 必须纳入：电脑端 APK 缓存下载；设备安装/启用另列详情，不把下载成功当作设备可用 |
| 飞书 CLI | 飞书工作项/需求接入 | `plugins/state.py` cli/feishu；`plugins/install.py:_resolve` 优先 manifest，回退 GitHub `larksuite/cli` 平台包与校验文件 | 必须纳入：复用安装器和进度；安装后引导接入配置 |
| Meego CLI | Meego 工作项/需求接入 | cli/meego；`plugins/install.py` 优先 manifest，回退 npm `@lark-project/meegle` tarball 与完整性信息 | 必须纳入：展示状态、来源和重试；不是独立 GitHub 二进制下载 |
| LangBot 对话组件 | 飞书对话、钉钉、企业微信智能机器人 | im/langbot；`plugins/langbot_host.py:install` 创建 venv，通过 uv/pip 安装锁定版本的 langbot、langbot-plugin | 必须纳入：三个渠道共用一项；pip 阶段显示阶段进度，不能虚构下载百分比 |

当前 `plugins/state.py:CATALOG` 中 needs_install=True 的只有 **cli/feishu、cli/meego、im/langbot**。飞书工作项 CLI、飞书 Webhook 通知、飞书对话是三种用途，页面需要写清，不能装一个就宣称全部可用。

### 随包组件和外部前置条件

| 组件 | 已核对的代码事实 | 页面处理 |
| --- | --- | --- |
| Playwright Python 包、driver/Node | `pyproject.toml` 固定 Playwright；`scripts/build_binary.py` 收集 driver/Node | 合并到 Scout 核心详情，不新增 Node 下载按钮 |
| 浏览器附带 FFmpeg 等资源 | `scripts/layers.py` 按 ms-playwright 目录集合管理，明确 FFmpeg 也有 revision | 浏览器条目下的组件明细，不单独更新 |
| ADB / adbutils / uiautomator2 | `pyproject.toml` 声明依赖；`scripts/build_binary.py` collect_all 包含 adbutils、uiautomator2，说明 ADB 二进制随包 | 核心中的 Android 支持；缺失提示修复/更新 Scout，不另做 platform-tools 下载器；与 ADB Keyboard 区分 |
| Pillow、NumPy、httpx、certifi、websockets 等 | `pyproject.toml` 基础依赖 | 随 runtime 管理，不逐个变成可下载卡片 |
| cua-driver、pywinauto、pyobjc、Appium Python 客户端 | `pyproject.toml` desktop/pc/ios-appium 可选依赖，打包代码收集可用的 cua-driver | 根据实际发布包和检测结果展示；当前未发现统一独立下载入口，首期不承诺一键安装 |
| LangBot 所需系统 Python | `langbot_host.py:_base_python`：冻结 Scout 寻找系统 Python；安装阶段检查 Python 3.11+ | 显示前置检查和缺失原因；首期不新增 Python 发行版下载管理 |
| uv | LangBot 安装优先用已有 uv，否则回退 pip | 不要求额外安装 uv |
| mino-bridge | `langbot_host.py:bridge_package/ensure_bridge` 将随 Scout 的源码打包后通过 LangBot 本地接口安装 | LangBot 内部安装步骤，不作为独立公网下载工具 |

因此管理单位应是“功能组件”，而不是每个 pip 包。第三方库内部的设备初始化/下载未在本次静态盘点中逐个执行验证，不能把源码搜索当作覆盖所有第三方网络行为的证明。

## 3. 不放入工具下载列表的项目

| 项目 | 代码依据与原因 | 保留位置 |
| --- | --- | --- |
| 用例服务 API | `plugins/state.py` cli/case_api，needs_install=False | 接入配置 |
| 微信 iLink | bot/wechat，needs_install=False，扫码登录 | 接入配置 |
| 飞书/企业微信 Webhook | bot/feishu_bot、bot/wecom，needs_install=False | 接入配置 |
| Gmail 验证码 | mail/gmail，needs_install=False | 接入配置 |
| MCP 分类 | `plugins/state.py` 有分类，当前无具体目录项 | 不展示空的下载项；有实际安装器后再接 |
| 被测业务 APK | `executors/adb_executor.py`、`executors/adb_command.py` 可按 URL 下载业务 APK | 用例/设备任务；只有 ADB Keyboard 属于工具依赖 |
| 对话附件、邮件、二维码 | `plugins/im_attachments.py`、`gmail_otp.py`、`wechat.py` 等业务数据流 | 各业务页面 |
| Scout 首次安装包 | Scout `packaging/bootstrap.*`；Nexus `services/scout_install.py` 生成安装命令 | 「添加节点」；尚未启动的 Scout 不能通过下载中心上报自己的首次安装 |
| Nexus/Workbench 发布包 | Workbench `packaging/install.py`、`mino_github_release.py` 有原生发布下载 | 部署/更新入口，不放入选定 Scout 的工具列表 |
| CI 和开发环境依赖 | Scout `.github/workflows/release.yml`、`scripts/build_binary.py`、`packaging/install.*` 的 pip/Playwright 安装 | 构建/开发流程；最终发布内容归 runtime/browser 展示 |

“以 Scout 为主”是产品管理边界。代码也存在 Nexus/Workbench 发布下载，因此不将“只有 Scout 会被下载”写成绝对技术事实。旧代码的 Studio 字样仅作历史背景，界面仍只放 Workbench。

## 4. 最小页面方案

沿用现有截图的节点列表、节点信息和视觉风格。页签改为「概览 / 下载中心 / 接入配置 / 日志」；沿用 node 查询参数，增加 pane=downloads，无需一级导航。

```text
当前节点：主机名 · 系统/架构 · Scout 版本 · 在线状态
[概览] [下载中心] [接入配置] [日志]

节点列表 │ 下载中心                         [刷新状态]
         │ 搜索工具   功能：全部   状态：全部
         │ 工具                 用途          状态      操作
         │ Scout 核心           节点运行      已安装    版本/升级说明
         │ Chromium 浏览器组件  Web 自动化    下载中    查看进度
         │ ADB Keyboard         Android 输入 未缓存    下载
         │ 飞书 CLI             工作项接入   未安装    安装
         │ Meego CLI            工作项接入   已安装    去配置
         │ LangBot 对话组件     对话渠道     未安装    安装
         │
         │ 下载进度 / 最近结果
         │ 工具名 · 下载/校验/安装阶段 · 已下载/总量
         │ 失败原因与重试；完成后展示实际结果
```

以上状态是线框示意，不代表当前节点实况。首期一个工具表、一个进度区即可，不再拆多个二级页面。工具详情展示用途、版本、来源、前置条件、随包组件、安装位置和错误说明；大小和磁盘占用有真实数据才显示。

操作和状态：

- 未安装：安装；进行中：进度，禁止重复启动；失败：原因和重试；已安装：详情，需要配置的给「去配置」。
- 核心提供手动安装升级说明，不恢复已撤除的自动升级/回退；可选工具按下文补齐版本检查与手动更新，不先做通用版本选择器。
- 接入配置保留快捷安装也可以，但必须调用同一后台动作和任务；安装完成返回原配置项。
- 工具安装与账号配置分开，例如「已安装 · 待配置」，不将安装成功等同于功能可用。
- 无节点引导添加；离线显示最后同步时间，禁止新安装；旧 Scout 缺少上报能力时提示升级。
- 不提供尚未实现的暂停/续传、通用卸载、跨节点批量安装、镜像设置或自动清缓存按钮。

## 5. 复用现有进度，最少补齐什么

现有基础：Scout `http_fetch.py:download_file` 已有字节回调；`update_progress.py`、`plugins/progress.py` 有阶段快照；`transport/node.py` 上报 node.update_progress / node.plugin_progress；Nexus `services/node_registry.py` 保存节点 update_job / plugin_job。

不替换整条链路，先统一展示：

1. 进度补稳定 task_id、工具标识和来源（手动安装/启动准备/节点更新），避免新旧任务混淆。
2. Chromium 启动准备纳入统一展示；ADB Keyboard 下载补字节或阶段上报。其电脑缓存、设备安装、输入法启用分开报告。
3. 保留插件串行安装和更新互斥；后台与手动入口也要去重，防止同一浏览器被同时写入。首期不做并发调度器。
4. 页面读 Nexus 快照并复用现有轮询；Scout→Nexus 继续事件上报。刷新/切页不取消。保留必要的最近结果，Scout 重连补当前状态。不承诺无限历史，进程重启后无法确认的结果标记「已中断/待检查」，不自动写成功。
5. 总字节已知才显示百分比；LangBot pip 阶段显示正在安装及耗时。下载 100% 后仍有校验/安装。现有插件失败也可能发 percent=100，必须优先判断错误和最终结果。
6. `download_file` 以 wb 写文件，当前没有 Range 续传能力，因此不展示暂停/续传。重试先检查是否已安装成功，再沿用原安装器。

最小统一字段：工具 ID、名称、用途、适用平台、安装状态、配置状态、版本（可获取时）、允许操作、关联进度和错误。六类条目以现有目录和代码汇总，不新增可编辑目录数据库或复杂依赖解析。

## 6. 三仓改动边界

| 仓库 | 必要改动 |
| --- | --- |
| Scout | 复用插件 CATALOG、分层计划和安装器，汇总工具事实；补后台依赖/输入法进度、任务标识和快照；保留固定来源、校验和互斥 |
| Nexus | 在现有节点服务/路由汇总、鉴权并转发安装命令；补必要的状态字段和最近结果，不在服务器下载/安装节点工具 |
| Workbench | `src/domains/settings/nodes/NodesPage.tsx` 加页签，新增工具表和进度区；复用 React Query 轮询和样式；接入配置定位对应工具 |

前端不维护下载 URL。后端汇总现有组件定义；业务能力目录仍以 Nexus 为真源，不另建第二套业务能力库。保留 manifest/固定官方来源和校验，不开放任意 URL 或脚本安装。

权限沿用账号和节点归属：管理员不自动拥有其他账号的 Scout 执行权。需要增补协议字段时同步两仓协议和文档，老节点不支持时明确显示，不能假装安装可用。

## 7. 实施次序与完成标准

后续确认实施后分两步：

1. **先统一可见性：** 六类条目、用途、状态、可用的安装进度和配置跳转；旧 update_progress 只作为组件下载适配来源，不能据此开放已关闭的产品升级；无法上报的字段明确未知。
2. **再补齐闭环：** 浏览器/输入法后台进度、缺失安装与失败重试、工具手动检查更新/更新、任务标识及重连快照。保留现有启动准备策略，是否改成按需下载另行决定。

最小验收：实际下载能显示；更新不重复拉取未变层；失败不误报成功；刷新/切节点不串任务；重复点击不重复安装；LangBot 缺系统 Python 时明确提示；离线/无权限不能下发；已安装但未配置时状态正确。记录真实节点结果，不以静态页面或 mock 冒充安装验收。

本阶段只提交方案，不改三仓业务代码、不执行安装。工具下载、安装、手动更新和修复纳入设计；产品自动升级/回退不恢复。卸载/空间治理、断点续传、更多工具接入等按下面的边界处理。

## 8. 补充核对：存在代码不等于具备产品能力

本轮补查实际入口后修正上一版对核心升级的判断：

- V7《取消与不恢复事项》已取消 Nexus/Scout/Workbench 产品自动升级/回退，保留手动安装升级。Scout `core.py` 拒绝 node.update；`fixed_update.py` 与 Nexus `services/scout_updates.py` 存在 upgrade_and_rollback_removed。下载中心不能通过调用残留私有函数绕过这些入口。
- 工具依赖的更新属于本方案；“更新 LangBot/飞书 CLI”和“更新 Scout 产品”分开处理。当前用户要求补齐更新设计，不据此恢复整个产品自动升级体系。
- `plugins/install.py:install` 目前会删除旧 dest 再换入 staging；不能未经改造就宣称安全更新或可回退。普通插件 `.installed` 仅写 ok，不能据此知道实际版本。
- `plugins/install.py:remove` 会删除插件目录并清除密钥；LangBot 的 data 位于该插件目录内。更新、修复绝不能用“remove 后 install”实现。
- Workbench `NodePlugins.tsx` 已在安装请求结果未知时阻止重复派发，新页面必须保留这一语义，不能所有超时都显示“点击重试”。

## 9. 下载、安装、更新、修复的统一语义

| 操作 | 做什么 | 不应顺带做什么 |
| --- | --- | --- |
| 刷新状态 | 读取/探测本地安装事实与正在执行的任务 | 不下载包，不检查所有远程新版本 |
| 检查更新 | 获取可信来源的版本/摘要，筛选本机兼容版本 | 不自动安装，不改变当前可用版本 |
| 下载 | 将确定版本的包缓存并校验 | 不自动激活、不改账号配置、不安装到 Android 设备 |
| 下载并安装 | 解析固定目标、检查环境、下载校验、安装、探测并报告 | 不清理账号/业务数据，不盲目安装全量可选依赖 |
| 更新 | 从当前版本切到已检查的兼容目标版本，保留配置与数据 | 不使用 latest 漂移到另一个目标，不绕过占用检查 |
| 修复 | 优先重装原版本/原摘要的工具文件，然后探测 | 不默认升级、不重置密钥；原包不可得时说明并让用户选择兼容目标 |
| 清理缓存 | 删除已确认可回收的受管下载缓存 | 不卸载工具、不删除活动临时文件或用户项目 |
| 移除工具 | 停止使用并移除工具；展示影响与数据处理范围 | 不隐含清除登录/业务数据，不假设现有 remove 可无损复用 |

页面主操作仍以“下载并安装”为主，避免要求普通用户先下载再安装。独立“仅下载”只对支持缓存复用的包类安装器展示；LangBot pip 安装首期不拆成独立离线下载功能。清缓存、移除工具先定义边界，当前首期不新增通用入口；现有接入配置移除功能保留原确认，不改称无损卸载。

### 标准流程

1. 确认目标 node_id、tool_id、操作和当前状态。检查平台、外部前置条件、磁盘及受影响功能。
2. 安装器解析目标版本/摘要，提交时固定该目标；如目标已变化，返回新目标供确认，不能偷偷安装新 latest。
3. 复用同一目标已校验缓存；没有则下载到临时文件，校验后标记为可安装。包管理器仍走自己的受控安装步骤。
4. 检查占用。下载可进行时不打断使用；替换/重启前若相关功能仍在执行，首期返回“使用中，请空闲后重试”，不新做排队调度。
5. 安装到临时位置，尽可能在激活前探测，再切换并做最小可用性检查。成功更新安装记录，刷新受影响模块状态。
6. 失败按阶段保留结果。下载/校验失败不得动旧安装；切换失败尽可能恢复旧文件并重新探测，没有验证恢复成功就标记需修复。

包已下载、文件已安装、工具可运行、账号已配置、功能已连通是不同事实。工具安装成功无需等待用户填密钥；“可运行但未配置”属于安装成功、功能未就绪。

### 各工具的更新策略

| 工具 | 版本与来源 | 更新/修复边界 |
| --- | --- | --- |
| Scout 核心 | 已发布 Scout 版本与现有安装包 | 展示手动升级说明；安装成功以重连接入后的实际版本为准，不以复制命令为准。不恢复后台自动升级/回退 |
| Chromium | 与当前 Scout runtime 的 Playwright revision 及发布层摘要匹配 | 不追浏览器最新稳定版；如果目标要求升级 runtime，提示先手动升级 Scout，不能后台越界替换核心 |
| ADB Keyboard | 发布源可用的 APK 版本信息/内容摘要 | 当前默认源是可变分支 URL，不能仅靠 URL 判断“最新”。新增缓存记录摘要；缺可信目标摘要时标明更新检测不可靠，不伪造版本。电脑缓存更新不直接升级所有连接设备 |
| 飞书 CLI | 固定 manifest 条目或官方 release 的具体版本、平台与摘要 | 新增已安装版本记录；核对 Scout 调用合同再推荐更新，不把官方 latest 自动当作兼容版本 |
| Meego CLI | 固定 manifest 或 npm 具体版本、tarball 与 integrity | 同上；保留调用入口、运行前置检查，禁止无条件追 latest |
| LangBot | Scout 支持的 langbot/langbot-plugin 版本组合及 bridge 版本 | 更新到经适配的组合，不单独升级任一包；优先新 venv 准备后切换。保留 data/配置，切换前停止共享宿主并说明三类渠道短暂不可用 |

LangBot 若升级含业务数据迁移，不能仅切回旧 venv 就声称回滚成功；没有验证数据兼容时阻止一键回退并给出恢复说明。首期不提供任意历史版本选择。

## 10. 各模块如何使用下载中心

下载中心是统一入口和状态视图，工具生命周期逻辑落在 Scout 的薄管理层，现有安装器继续负责实际工作。所有入口使用同一工具 ID、安装记录和任务，禁止各功能页面各自发起一份下载。

| 调用模块 | 日常如何使用 | 缺失/更新时如何处理 |
| --- | --- | --- |
| Web 自动化 | 使用已安装且匹配 runtime 的浏览器；执行前检查 readiness | 返回 browser_missing/browser_incompatible 与工具定位链接；用户在下载中心补齐后重新执行，不在业务失败分支循环下载 |
| Android 文本输入 | 输入法模块复用受管 APK 缓存；设备侧安装/启用仍由原设备流程负责 | 缺缓存时统一报告下载；缺设备安装单独报告 serial 对应状态，不能用节点级 installed 覆盖所有手机 |
| 飞书/Meego 接入配置 | 保存配置、调用工具检测；通过既有后台动作安装 | 快捷安装和下载中心共用任务；安装成功返回原配置项；配置失败不触发重装 |
| LangBot 对话渠道 | 三个渠道共享宿主，分别保留配置和连通性 | 一个渠道请求安装可复用现有任务；更新展示所有受影响渠道，不能分别安装三份 LangBot |
| 启动后台准备 | 继续使用当前启动策略，但统一登记来源为 startup 的任务 | 与手动请求去重；失败可在页面重试，不因心跳/刷新反复重装 |
| Scout 手动安装/升级 | 引导安装已发布包；节点接入后重新探测清单 | 保留添加节点入口；旧节点协议不足时只展示事实和升级说明 |
| 下载中心 | 显示清单、当前任务、最近结果，调用固定操作 | 不掌握密钥、不自行拼下载地址、不从浏览器直连本机安装器 |

业务模块的执行要求由 Nexus 能力声明/现有路由确定，Scout 负责检查本地实况。首期仅维护这六类工具与用途的简单映射，不创建跨模块的通用依赖图系统。缺依赖返回明确原因；用户已启动的后台准备可以继续，普通业务请求不默认获得安装/升级授权。

## 11. 谁维护什么，如何新增一个工具

| 信息/逻辑 | 维护位置与责任 | 不重复维护的地方 |
| --- | --- | --- |
| 工具 ID、安装器与支持操作 | Scout；复用插件 class/id、分层标识，以固定适配器汇总 | Workbench 不另写一份安装规则 |
| 工具版本、平台包、摘要 | 已有发布 manifest/锁定版本定义；工具维护者随发布更新 | Nexus 不人工复制第二份包地址清单 |
| 功能为什么需要工具 | Nexus 现有能力/功能声明，引用工具 ID | Scout 不复制业务能力目录 |
| 实际安装版本、缓存、健康与任务 | 目标 Scout 的本地事实；Nexus 保存查询所需投影 | 前端 localStorage 不是真源 |
| 账号配置与连通状态 | 原接入配置模块 | 下载记录不含密钥，也不重建一套配置存储 |
| 列表、按钮、进度和跳转 | Workbench 通用展示组件 | 新工具无需复制一套独立安装 UI |

Scout 新增薄的 `tools` 包，具体文件和函数按第 14～20 节冻结的编码合同执行。每个适配器复用旧模块，明确返回支持操作；不是插件框架，也不支持上传任意安装脚本。

新增工具的最小维护步骤：

1. 定义稳定 ID、名称、用途、平台与前置条件；区分独立安装、随包组件、外部检测。
2. 指定可信固定来源、版本/摘要和安装器；说明哪些目录是工具文件、配置、业务数据、缓存。
3. 实现状态探测、安装及明确支持的更新/修复；接公共进度，保证失败不删除配置，使用中不强制替换。
4. 在 Nexus 功能映射中引用工具 ID；配置入口、业务缺失提示定位到同一条目。
5. 发布前完成一次实际安装、更新、失败和配置保留验证；不支持的平台/操作不要出现可点击按钮。

维护者升级第三方版本时，应先验证调用入口、参数、输出与配置兼容，再更新支持版本。用户“检查更新”只发现受支持目标；不把 npm/PyPI/GitHub 最新版本未经验证直接推荐安装。

## 12. 最小记录与异常处理

不先建立复杂新模型，给现有安装状态和任务快照补够用的字段：

- 安装记录：tool_id、installed_version 或 artifact_digest、来源、安装位置、安装时间、最近探测结果。普通插件的 ok 标记需要补版本/摘要；旧安装识别不出版本时显示“版本未知”，不推断为最新版。
- 任务记录：task_id、node_id、tool_id、operation、触发来源、固定目标、阶段、字节、最终结果及错误。重试创建关联的新尝试；相同活动操作复用任务。
- 任务状态先持久化再执行，保留当前和最近终态；Nexus 业务投影进入现有 SQLite，Scout 本地记录按现有存储约定扩展。消息补递增序号，旧事件不得覆盖新终态；不为此新建消息中间件。

| 情况 | 页面与后台行为 |
| --- | --- |
| 请求超时，是否启动未知 | 显示“结果待确认”，按 task_id 查询并重连对账；不能立即重复派发 |
| 网络断连 | 节点离线与任务失败分开；已开始的本地下载可继续，重连恢复快照 |
| 进程重启 | 探测文件和持久任务；不确定则中断/待检查，先核实再重试 |
| 来源失败/校验失败 | 展示明确阶段，旧工具继续可用；不自动切到任意镜像或取消校验 |
| 包已下载、安装失败 | 若缓存仍可信可复用；页面显示安装失败，不重新下载作为默认修复 |
| 新版不可用、旧版仍正常 | 保留旧版事实和更新失败结果；不能把整项错误地显示成“未安装” |
| 取消 | 只有安装器能安全响应时开放；收到取消请求不等于已取消，安装切换中不强杀 |
| 更新/修复遇到账号问题 | 返回配置/登录入口；不清除账号数据来“修好”工具 |

## 13. 本轮设计补充后的交付边界

首期完整目标是：能看清工具用途及依赖，下载安装有真实进度，能检查并手动更新受支持的工具，失败可诊断和修复，各模块复用同一状态与任务，账号/业务数据不因更新丢失。

“更新”不是在表格里加一个按钮：飞书/Meego 要补版本记录与兼容目标，LangBot 要处理共享宿主及数据保留，Chromium 要遵守 runtime 兼容，ADB Keyboard 要区分缓存和设备。缺少这些前提时，先显示限制，不以直接重装冒充已完成更新能力。

补充验收：下载安装与独立下载按钮语义一致；更新固定目标不漂移；配置/密钥和 LangBot data 保留；业务使用中阻止切换；共享渠道只安装一次；未知结果不会重复派发；核心自动升级入口保持关闭；版本未知、平台不支持和检查更新失败均有真实提示。

## 14. 编码基线：本节起冻结实现决定

前文保留产品意图与盘点；本节起明确技术选择，消除“实施时再定”。若文字冲突，以本节起的具体合同为准。当前交付仍只有文档，不代表以下能力已经存在。

新增核对结论：

- `rRuntime.py:install_node_plugin` 同步等待插件操作，timeout=600 秒；LangBot pip 子进程允许 3600 秒。新页面不能复用这个长请求作为任务生命周期。
- `_public_plugin_job` 按白名单过滤字段，直接往 Scout 旧事件里加 task_id 会在 Nexus 丢失；需要两端一起改。
- `NodeSession.persist_snapshot` 当前只存主机基本信息，未持久化 plugin_job/update_job；不能声称现有进度重启后能恢复。
- Workbench `queries.ts:useNodes` 是活动时 2 秒、闲时 15 秒轮询。首期保持此方式，不新增前端 WebSocket 订阅。
- `plugins/paths.py` 以 Nexus origin + owner 建立账号目录。新任务必须沿用此隔离，不能把所有账号插件状态合并成机器全局表。

冻结首期选择：六个工具条目；一个工具表和详情/任务区；短请求提交、Scout 单个变更任务执行；不建立队列调度、下载守护进程或通用依赖引擎。当前执行中另一个变更请求返回 busy，而不是隐式排队。状态查询不拿安装锁。

### 稳定工具 ID

| tool_id | Scout 适配器 | 作用域 | 配置跳转 |
| --- | --- | --- | --- |
| scout.core | core | 机器共享，只读 | 版本/手动升级说明 |
| browser.chromium | browser | 机器共享 | 无 |
| android.adb_keyboard | adb_keyboard | 机器缓存；设备状态另列 | 设备详情 |
| cli.feishu | plugin，class=cli/id=feishu | 当前已绑定账号 | 飞书工作项配置 |
| cli.meego | plugin，class=cli/id=meego | 当前已绑定账号 | Meego 配置 |
| im.langbot | langbot | 当前已绑定账号，共享三个渠道 | 对话接入配置 |

scope 只决定文件与状态归属，不扩大权限：即使是机器共享组件，Nexus 仍只允许当前节点归属账号发起操作。账号重绑时，旧账号任务记录不可暴露给新账号；重绑须先检查所有变更任务已结束，不能切换进行中的安装归属。

## 15. 首期接口与数据合同

### Workbench → Nexus

保留既有响应 `ok(...)` 包装，以下字段均指 data 内容。增加以下路由，不修改旧 plugins/install 的成功语义。

| 接口 | 请求 | 响应/语义 |
| --- | --- | --- |
| GET /runtime/nodes/{node_id}/tools | 无 | tools、active_task、recent_tasks（最多20条）、snapshot_at、stale、tool_management_version |
| POST /runtime/nodes/{node_id}/tool-tasks | operation_id、tool_id、action、可选 target_id | 202：task_id=operation_id、delivery_state、task；受理不等于安装完成 |
| GET /runtime/nodes/{node_id}/tool-tasks/{task_id} | 无 | 持久任务快照；节点离线也可读本账号已有任务 |
| POST /runtime/nodes/{node_id}/tool-tasks/{task_id}/cancel | 无 | 取消请求是否接受及最新快照；仅 advertised allowed_actions 包含 cancel 时允许 |

action 首期枚举：refresh、check_update、download、install、update、repair。refresh/check_update 也返回短任务，不在 HTTP 请求里等待源站；refresh 只探测本地。不同工具不支持的 action 返回 422 unsupported_action。remove/cache_cleanup 不在新接口中开放。

POST operation_id 为调用方预先生成的 UUID，同一个提交失败重试必须复用；Nexus 唯一键为 operation_id，重复且身份/节点/工具/参数一致返回原任务，不一致返回409 operation_conflict。前端 mutation 禁止自动重试。

提交前 Nexus 校验账号、节点在线/可执行模式、上报合同版本与操作支持；Scout 再校验绑定身份、本地占用、目标与安装器。鉴权不可见统一404；节点离线/休眠409；旧节点409 tool_management_unsupported；任务变更冲突409 tool_busy。目标已失效409 target_changed，并返回需重新检查的标记。

### ToolView

固定字段：tool_id、label、description、scope、feature_labels、platform_supported、installed_version（可空）、installed_digest（可空）、install_state、health_state、configuration_state、update_state、target（可空）、allowed_actions、blocked_reason、active_task_id、observed_at。

- install_state：unknown / missing / cached / installed。进行中的下载/安装状态属于任务，不覆盖旧版 installed。
- health_state：unknown / ready / broken；configuration_state：not_required / missing / configured，账号连通性继续用原模块字段，不由安装器推断。
- update_state：unchecked / up_to_date / available / unsupported / check_failed。源站失败不能显示 up_to_date。
- allowed_actions 由 Scout 返回，Nexus 做权限交集；Workbench 不按名称猜测操作。
- target：target_id、version（可空）、digest、size_bytes（可空）、source_label、requires_restart、impact_labels、checked_at。下载 URL 与本地执行参数不下发给前端。

target_id 是 Scout 保存的固定目标引用，默认有效期24小时；过期重新检查。update 必须带 target_id；install/download 未带时由任务在首次解析阶段固定，之后不得再追 latest；repair 使用原安装记录目标。缓存命中必须再次核对摘要。

### TaskView

task_id、tool_id、action、trigger（user/startup/legacy）、delivery_state、state、stage、seq、target_id、bytes_received、bytes_total（可空）、created_at、updated_at、finished_at、error_code、message、allowed_actions、previous_task_id（可空）。

delivery_state：pending / acknowledged / unknown / rejected；state：accepted / running / succeeded / failed / cancel_requested / cancelled / interrupted。未知应答只改 delivery_state，不凭空把 Scout 的任务判失败。

stage：resolve / download / verify / install / activate / probe。没有下载阶段的 refresh 不显示下载进度。安装任务最终结果由适配器检查决定；ADB Keyboard 节点任务以缓存校验成功为完成，不冒充手机安装。

## 16. Scout 协议、持久化与任务执行

### 复用传输，新增窄合同

两仓 protocol.py 同步新增 NODE_COMMAND_CAPS：node.tools_snapshot、node.tool_task_start、node.tool_task_status、node.tool_task_cancel；新增 NODE_EVENT_CAPS：node.tool_progress。仍通过现有 EXECUTE/框架事件传输，不用 node.update 执行任何操作。

Register/Heartbeat 增加可选 tool_management_version（默认0）与 tool_snapshot（默认None）。合同版本1包含当前账号工具摘要、活动任务和最近20条结果；按现有编码约定兼容缺字段，旧客户端未知字段处理须在协议联调中验证。0/未上报时只做旧状态只读映射，不将旧安装命令伪装成可恢复异步任务。

tool_progress 直接复用 _send_framework，但使用独立白名单，不依赖 _plugins_ready 才上报浏览器任务。Nexus 按认证连接确定节点与账号，忽略载荷伪造归属。task_id 内 seq 持久递增；只接收更大序号。新连接建立后拒绝旧连接迟到事件，重连通过快照补结果。

node.tool_task_start 只完成校验、持久受理和线程启动，目标应在数秒内返回；下载/安装放独立后台工作线程，不能持有 core 请求线程直到结束。网络提交等待上限15秒，超时标 unknown；查询/取消仍通过短命令。

### 最小存储决定

Scout 新增 `tools/state.py`，使用标准库 sqlite3 管理 `config_dir()/tools/state.sqlite3`，只存工具安装记录、解析目标及任务，不存密钥/业务数据。每条任务有 origin/owner 作用域；机器组件安装事实可共享，操作日志不可跨账号共享。使用事务约束去重，运行时变更互斥再加进程级文件锁，避免 CLI/后台线程绕过。

Nexus 新增一个 `node_tool_tasks` ORM 表，保存 operation_id、node_id、owner_user_id、参数、delivery_state 和公开任务快照；现有 nodes.extra 增加 tool_snapshot，按既有 additive 迁移方式创建。无需工具目录表、下载计划表、依赖图表。任务主事实来自 Scout，Nexus 仅负责派发状态与投影。

首期只返回最近20条，不自动删除任务去重记录。日志做长度上限和脱敏；后续再设计保留策略，不能清掉 operation_id 后让迟到重试重装。Scout 每阶段与终态立即持久化，字节进度最多每秒一次；终态同时刷新库存。Nexus 按更新序号落盘，不在高频字节事件中重复重建全节点。

### 受理、恢复与竞争规则

1. Nexus 在发送前落 pending；Scout 对相同 task_id + 参数返回已有任务，不重复执行。相同工具、操作、固定目标的活动任务可返回 existing_task_id；其他变更返回 tool_busy。
2. 请求结果未知时查询同一 task_id，禁止以新 ID 自动重发。若当前 Scout 证明该任务未受理且无活动冲突，可由用户重试同一 ID；断连不能作为“未受理”的证明。
   Nexus 启动/节点重连后，对 pending/unknown 任务先执行 node.tool_task_status 对账，不自动再次 start；任务 GET 也可触发一次限频对账并返回已有快照。若 POST 重复同一 ID，默认仅返回已有记录；只有已获得 Scout 的明确 not_found、原请求仍属于当前账号与节点且用户明确点重试时，才允许重新派发同一 ID。这样同时覆盖“Nexus 落盘后未发送就退出”和“Scout 已开始但回复丢失”。
3. Scout 重启发现未完成任务：检查激活记录与本地文件，能证明完成才恢复成功；其余标 interrupted，不自动重复安装。
4. 变更互斥覆盖网页、旧插件接口、启动准备、CLI heavy-deps，以及相关 remove。旧入口未迁入锁前不可宣称任务统一。
5. 业务调用使用同一工具使用锁/计数；激活前原子检查并占用，拒绝新使用者，结束释放。仅检查一次“当前 busy”不够，必须覆盖检查到替换间隙。依赖锁拿不到立即返回 tool_in_use，不阻塞业务线程等待安装。
6. 首期仅包下载阶段支持协作取消，pip/激活阶段不开放取消。取消确认后保留旧安装及已验证缓存；部分文件不标成可安装。核心重启操作遇到活动工具变更应提示冲突，不静默中断。

## 17. 安装器如何改，版本与数据如何落地

新建 `mino_scout/tools/catalog.py`（六条固定描述/动作）、`service.py`（状态/受理/互斥）、`state.py`（持久化）、`adapters.py`（对旧安装器的薄适配）。先一个 adapters 文件，不引入动态发现/插件注册框架。

适配器最小合同：probe(context)、resolve(action, target_id)、execute(target, report, cancel)。resolve 返回固定来源、摘要、平台、版本及影响；execute 不再解析 latest。安装器私有路径不能从前端请求指定。

| 改动点 | 编码要求 |
| --- | --- |
| `http_fetch.py` | 保留旧调用兼容，增加可选取消检查和受管临时下载；摘要校验在包适配器执行。下载成功与目标校验成功分开 |
| `plugins/install.py` | 拆出 resolve/install_resolved；记录具体版本/摘要；旧 install 包装层转入统一服务，避免递归调用。禁止 remove→install；旧目录保留至新目录检查通过 |
| `plugins/state.py` | installed 读取新记录并兼容旧标记；旧 ok 不伪造版本；状态查询不可偷偷联网升级 |
| `heavy_deps.py` | 启动准备经 service 提交 browser 任务；禁止对 runtime 步骤就地执行。返回 needs_scout_upgrade 时结束并提示手动升级 |
| `adb_ime.py` | ensure_apk_cached 改为复用统一缓存；原设备安装/启用流程不变，按 serial 报告；同一次设备漂移检查不反复强制下载 |
| `langbot_host.py` | 运行环境与 data 分离管理，激活前停止宿主，切换后恢复原启用渠道；不清除密钥和业务数据 |

**兼容目标来源定案：** 发布 manifest 增加可选 tools 条目，字段为 tool_id、version、platform、arch、artifact_url、digest_algorithm、digest、size_bytes、min_scout_version，以及需要时的 Playwright revision/版本组合。复用现有 plugins 条目解析为统一 target；不强制一次重写发布结构。工具维护者只发布验证过的目标。

首次插件安装可暂保留现有官方源回退，但必须把具体版本与摘要记录下来并校验；“检查更新”只推荐受支持 manifest 目标。无该条目时返回 update_state=unsupported、reason=compatible_target_unpublished，不能将 latest 当作已验证升级。LangBot 继续使用代码锁定组合，浏览器用匹配 runtime 的发布层。发布目标缺失是内容准备项，不阻碍编码，界面需如实禁用更新。

ADB Keyboard 当前可变 URL 无可信固定摘要：新增 tools 发布条目以固定提交 URL+摘要交付。缺条目时保留既有缓存事实，下载中心不新增未经固定目标校验的更新操作。既有自动准备不在本轮偷偷改变策略，作为 legacy 来源明确标识。

**LangBot 不搬用户数据：** 保留 root/data、账号配置和密钥路径。新版本建在 root/envs/<target_id>/，新增持久 active_env 引用；无引用则继续读取旧 root/venv。venv 在最终路径创建，避免移动后脚本绝对路径失效。旧 venv 原位保留；切换只更新受控引用。若升级涉及不可逆 data 迁移且无已验证恢复方案，禁止一键更新，返回 manual_update_required。首次纳管不移动旧数据。

浏览器包解压到同文件系统 staging，完成平台/摘要/目录约束检查后切换；旧 browser 保留到 probe 成功。任何包的路径、链接和解压范围必须限制在该工具受管目录内，不能通过链接写到用户其他位置。

## 18. 具体改动清单与兼容顺序

| 仓库/文件 | 交付改动 |
| --- | --- |
| Scout `tools/{catalog,service,state,adapters}.py` | 固定清单、任务受理、状态与适配器 |
| Scout `core.py`、`transport/node.py`、`protocol.py` | 新命令与独立进度事件；注册/心跳快照，后台任务不阻塞事件循环 |
| Scout `plugins/{install,state,service,progress}.py` | 安装/移除共用锁；旧安装调用共用任务并等待最终结果；统一版本事实 |
| Scout `http_fetch.py`、`heavy_deps.py`、`adb_ime.py`、`plugins/langbot_host.py` | 下载回调、后台入口、缓存复用与 LangBot 环境切换 |
| Scout `scripts/write_manifest.py` 及实际发布输入 | 校验并输出可选 tools 条目，旧 manifest 仍可读 |
| Nexus `models/node_tool_task.py`、`services/node_tools.py` | 一个任务表、鉴权受理/查询和快照投影 |
| Nexus ORM 注册/既有迁移入口 | 注册新表并走启动 additive 迁移；不能只有 ORM 文件而没有建表装配 |
| Nexus `routers/rRuntime.py` | 新 tools/tool-tasks 路由；请求模型定义在模块级；旧接口保持终态语义 |
| Nexus `services/node_registry.py`、`services/node_store.py` | 新状态白名单、seq/连接校验、nodes.extra 持久化及查询输出 |
| Nexus `core/protocol.py`、双方 PROTOCOL.md | 同步命令、事件、可选字段、版本与错误合同 |
| Workbench `src/api/nodes.ts`、节点 `queries.ts` | ToolView/TaskView、短请求、账号/节点级 query key、活动2秒/闲时15秒轮询 |
| Workbench `NodesPage.tsx`、新增 `NodeDownloads.tsx` | 新页签、列表、详情、目标确认和任务区，复用设计组件 |
| Workbench `NodePlugins.tsx`、`PluginStatusViews.tsx` | 新节点安装走任务 API，展示最终状态；旧节点保留现有流程和 unknown 防重逻辑 |

旧 plugins/install 接口仍返回“安装完成”而非“已受理”：新版 Scout 的兼容包装在同一个任务上等待，可能继续超时，但不会再次启动。新版 Workbench 对合同版本1使用新 API；旧 Scout 的下载中心只读，接入配置保留原能力。不得全局改变旧 API 返回值导致调用方把 accepted 当成功。

部署顺序：Nexus 先支持可选协议/路由 → Scout 上报合同版本1 → Workbench 根据实际能力显示操作。任意一步先后不齐都降级提示，不以版本字符串猜能力。Nexus 旧版对新字段的处理需联调后决定是否允许 Scout 单独先发版。

## 19. 分批编码与验收，不把后续需求塞进首批

| 批次 | 编码内容 | 本批完成证据 |
| --- | --- | --- |
| A 合同与状态 | 协议可选字段、六项 probe、任务表/本地记录、只读列表 | 新旧节点状态正常；离线读快照；账号切换无旧账号数据 |
| B 异步安装闭环 | task start/status、飞书/Meego 安装适配，下载中心和配置入口共用任务 | 短请求返回；真实安装/失败有 task_id；重复提交/刷新不重装 |
| C 后台依赖纳管 | Chromium、ADB Keyboard、启动/CLI 入口，包缓存和取消 | 后台任务可见；手动与后台去重；runtime 不被越界替换；设备缓存/安装状态区分 |
| D 更新修复与 LangBot | 固定版本目标、旧文件保护、使用锁、LangBot 环境切换 | 配置/数据保留；更新失败旧版状态准确；缺 Python 明确失败；受支持目标才可更新 |
| E 兼容和恢复收尾 | 重连、进程中断、旧接口、部署混合版本及回归 | 未知结果不重派；核心升级仍410/拒绝；所有已声明支持的平台给出真实结果 |

批次 A/B 可先交付开发验证，但整个需求不能只完成列表就标完成。首期范围不包含通用卸载/清缓存 UI、任意版本选择、断点续传、并行任务和跨节点批量操作。

验证方法：Workbench 使用现有 typecheck/build 与真实浏览器检查页签、筛选、任务和返回配置；Python 做语法/导入、路由注册和协议 round-trip 检查；真实安装在隔离账号/节点和临时数据目录中执行，记录 task_id/节点版本/阶段证据。不跑 pytest、不提交 mock 测试文件，不读写真实业务数据库来构造验收状态。

关键故障验收：下载断网、未知应答、摘要不符、磁盘不足、安装器失败、使用中更新、切换后探测失败、Nexus/Scout 单独重启、账号重绑、旧连接迟到消息。验证目的分别对应上述合同，不反复做无关全量审查。

## 20. 编码准入结论

当前可从批次 A 开始编码：工具标识、入口、协议、字段、存储、幂等、状态机、权限、维护位置和兼容策略均已确定，无需再决定一套新架构。

最小补充请求字段：POST tool-tasks 可带 retry_delivery=true，仅用于上文已对账证明 not_found 的同 ID 用户重派；不改变操作参数，不能跳过冲突校验。普通失败后的业务重试使用新 operation_id，并携带 previous_task_id；新任务仍需重新检查目标和占用。两个字段均由服务端验证，不由客户端声明“没执行”决定。

仍需在实施中产出的内容是实际兼容工具发布条目、各平台安装/更新证据和第三方版本验证。缺失时按照明确的 unsupported/manual_update_required 分支处理，不能临时开放不安全操作或声称完整支持。若实施发现新的产品取舍再回到本文件更新，不以持续扩写方案替代编码。
