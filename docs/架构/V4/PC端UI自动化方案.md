# PC 端 UI 自动化测试插件方案

> 范围：现有 Mino 自动化扩展到 PC；Workbench 是唯一用户入口，节点、插件、应用、任务、报告及管理配置均在其中完成。本文基于当前项目代码与 Cua 官方文档，未安装或运行 Cua；能力覆盖和效果需实测。

## 1. 推荐结论

**Scout 接入 Cua Driver，优先使用 Python SDK；Nexus 保留现有 `agent_loop` 的业务决策与准出。** PC 窗口观察、激活、前台读取与动作投递统一交给 Cua；驱动缺口明确阻塞，不回退自研 UI Automation / AX 原生封装。当前实现以 §9.5 为准。

视觉执行分两条可比较的路径：① Cua 截图、Nexus 看图定位；② Cua 截图并做本地文字 / 图标解析，Nexus 根据图像及区域信息选目标。第二条是增强感知，不是把整段用例交给 Cua 自己跑。

首版优先验证 Windows 和 macOS 的目标应用，Linux 按需求后置；复用 Android / Web 的用例、调度、验证和报告。原有 Web Playwright 路线继续保留，不为统一名字而改用桌面点击。

## 2. Cua 提升在哪里，与通用自动化差什么

先区分三个层次：**驱动负责观察与操作，感知负责从图中找区域，Agent 负责理解任务、选择动作和判断结果。** Cua 仓库同时包含多个层次的组件，但安装 Driver 不等于安装一套会自主做测试的 Agent。

“Playwright 和 UI Automation 只截图”仅适用于我们主动选择的纯视觉使用方式，并非工具本身的能力上限。当前 Mino Web 的 `dom_hierarchy.py` 已返回 DOM 节点、ARIA、焦点、页签等信息；Windows UI Automation 本身提供控件属性、模式和事件，截图一般由配套屏幕采集接口提供。项目目前没有完整的 Windows UIA 执行器，不能把它写成已接入能力。

| 对比项 | 传统脚本 / 单平台驱动 | 当前 Mino + Playwright / Android | 引入 Cua 的实际增量 |
| --- | --- | --- | --- |
| 流程由谁决定 | 测试脚本写死流程与断言 | Nexus 已按用例和画面决策，已经有 Agent 循环 | 主要补 PC 设备层，不是首次引入“AI 自动化”。 |
| 平台覆盖 | UIA 管 Windows，AX 管 macOS，Playwright 管浏览器 | 已有 Android / Web 等通道，缺通用 PC 桌面通道 | 复用跨平台应用、窗口、控件、截图与输入接口，少维护多套原生封装。各平台覆盖仍不等价。 |
| 控件不可访问时 | 需另接图像识别或坐标脚本 | Nexus 视觉模式已可看图决策 | 可选 perception 提供文字和图标区域，给自绘页面提供候选落点；提升幅度需要对照实验。 |
| 动作可靠性 | 等待、句柄失效、输入效果等由测试框架封装 | 已有焦点、输入契约、回看和熔断，PC 还需适配 | 可复用窗口 / 捕获引用校验、实际执行路径、动作效果分级和局部后置条件验证。不是所有问题自动解决。 |
| 不抢焦点的操作 | 需各系统分别实现 | 当前主要按现有通道执行 | 部分应用可后台操作，但不能由此推导所有操作可并发，也不能代替真实前台交互测试。 |
| 稳定性与成本 | 稳定控件脚本通常更快、更可重复 | 视觉路径有模型延迟和费用 | Cua 本身不保证更快、更准；本地解析也有额外耗时，原生控件定位仍可能是最佳路径。 |

**价值在于复用 PC 执行基础设施和增加可追溯的感知 / 动作事实。** 用例设计、业务预期、数据准备、覆盖率、缺陷归因和完整报告仍由 Mino 做。不能拿 Cua 官方演示或驱动样例通过率当本项目测试通过率。

## 3. “Cua 自己看图”到底做了什么

### 3.1 三类能力不要混用

| 组件 | 已核实的行为 | 没有替我们做的事 |
| --- | --- | --- |
| **Cua Driver 默认安装** | 采集窗口 / 桌面状态与图像、读取可访问控件、执行输入；`verify_state` 验证调用方指定的局部结构化条件 | 默认不带视觉模型；不会自动解释截图中的业务含义，不负责用例编排。 |
| **可选 `cua-perception`** | `parse_visual_regions` 对一张已捕获截图做 OCR / 图标区域检测；基于本地 worker、ONNX 模型，返回文字 / 图标区域。当前发布材料记录 PP-OCR、OmniParser 检测器 | 不自行截图、不选择动作、不发送输入、不判断“订单是否创建成功”。检测框不等于真实控件，也不保证可点击。 |
| **另行接入的 Computer Use Agent / 视觉模型** | 可依据画面和目标推理动作；具体能力取决于模型与外部执行循环 | 不是 Driver 自带能力。把整个循环交给它会改变当前架构，本方案不采用。 |

perception 不调用 Nexus 的大模型，也不需要模型服务凭据；它做的是局部识别。因而并非“两个 Agent 都在规划”，但确实增加了一次视觉推理计算。读取控件树、OCR 识字、理解业务三者在日志中必须区分。

### 3.2 两条看图路径与一个结构化路径

```text
A 纯视觉（首版基线）
Cua 截图 → Scout 返回图像 → Nexus 看图识别、选目标与坐标
→ Scout / Cua 执行动作 → 新图 → Nexus 验证

B 视觉 + 区域辅助（可选，重点验证其收益）
Cua 截图 → perception 解析同一张图 → Scout 返回图像 + 区域
→ Nexus 理解用例、选择区域或落点 → Cua 执行动作
→ 重新采集 → Nexus 按后置条件验证

C 结构化控件
Cua 控件快照 → Nexus 选择明确控件 → Cua 用新鲜控件引用执行
→ 控件状态读回 / verify_state + 必要截图 → Nexus 判定步骤结果
```

例如“点击保存”：A 中 Nexus 自己从截图找“保存”；B 中 perception 先给出带“保存”文字的区域，Nexus 再判断哪个区域属于目标窗口和当前任务；C 中 Nexus 使用控件名称、角色与作用域选择真实控件。三条路径都不能仅因点击接口成功，就断言数据已经保存。

建议配置为 `action_scheme=visual` 下的 `perception_mode=none|cua_regions`，PC 控件路径新增 `accessibility`；保留 Web `dom`。这些是 **Mino 拟新增配置**，不是 Cua 原生枚举。任务开始时固定方案，不在失败后静默换模式；B 是否允许解析失败后回退原图，由任务策略明确声明并写入轨迹。

区域框与原图应来自同一次 capture。Nexus 可以按选中的区域确定落点，但不能把 OCR 文本当作 UIA 属性，更不能把区域置信度当作测试通过概率。目标模糊、低置信度或多个“保存”按钮时需继续观察 / 判定，不能无条件点区域中心。

**收益验收要特别覆盖中文、自绘和缩放。** 当前感知扩展的材料列出英文识别器，不能据此承诺中文识别质量；以目标版本、真实中文界面实测为准。感知扩展并非默认依赖：默认 Driver 为 MIT，OmniParser 检测器有 AGPL-3.0-only 条款，发布给用户前需确定相应交付方式。

## 4. 对现有 `agent_loop` 有什么影响

**主流程保留，但不是零代码改动。** 仍是“观察 → Nexus 决策 → 派发 → 新观察 / 回看 → 后置条件 → 下一步”；不在 Scout 放第二套任务规划器，不让 Cua 的建议自行触发改道。

| 环节 | 改动 |
| --- | --- |
| 平台与菜单 | 显式增加 Desktop；当前 Nexus `executors_for_platform()`、`UiChannel` 与 Scout `DeviceRef.compatible_executors` 的未知平台回落需修正，不能错派 ADB。只提供本机实测可用的动作与输入组合。 |
| 观察 | 扩展 OBSERVE 的桌面元数据、控件和可选 visual_regions。B 路径由一次截图产生原图与区域，不各截一张。原图仍交给 Nexus 做上下文理解及业务验证。 |
| 模型输入 | A 保留原视觉输入；B 新增有上限的区域槽，携带来源、capture 引用、解析版本与状态。当前 visual 分支会清空 hierarchy 槽，不能把区域伪装成 DOM 塞进去；单独接入提示词组装。 |
| 目标与坐标 | Nexus 选业务目标；Scout 适配千分比或控件引用。窗口图像由 Cua 捕获和缩放，点击绑定 pid/window_id/capture_id，窗口偏移、Retina 与压缩映射由 Driver 处理，不在项目中再次叠加变换。 |
| 执行 | 继续 `RouterProxy → EXECUTE → Scout → Cua`。选择方式（区域 / 像素 / 控件）与 `delivery_mode` 分开；像素定位也可能经辅助功能后台投递。严格鼠标交互测试须约束并核对实际 route，做不到就报不支持。 |
| 动作回执 | 读取 Cua `effect / route / delivery / evidence`，不以调用无异常直接推进里程碑。`unverifiable` 或 `partial` 触发新观察与显式判定，不当成业务失败或成功；证据不足才报告 blocked。 |
| 验证与恢复 | Nexus 继续负责业务断言。可将已编译的窗口存在 / 控件值条件交给 `verify_state`；`unknown` 不得转 pass。Cua 的 escalation 仅是建议，预算、重试和路线切换仍由 Nexus 决定。 |

**捕获引用约束要进入执行契约**：Cua `capture_id`、控件 `snapshot_id`、Mino 报告的 screenshot hash 是不同身份。捕获、解析、动作必须共用持久 SDK runtime 或连接，不能每步重建 Driver。capture-bound click 一次就消费捕获引用；动作尝试、超时、窗口移动 / 缩放、滚动后必须重新观察。当前复合点击等能力须逐项核实，不能把同一 capture ID 循环用于多个动作。

Nexus 的五态结果继续保留，Cua 的动作效果单独记录：`confirmed` 只证明动作有相应读回证据，不证明用例通过；`suspected_noop` 需核实；拒绝按原因分为能力不支持、需权限或实际故障。对结果未知的动作先观察，不能直接再次提交表单。

**不受影响的职责**：用例编译、前置依赖、账号 / 数据管理、取消、步数预算及最终通过标准仍归原流程。局部视觉解析 worker 位于 Scout 的可选插件中，不在 Nexus 安装 OCR / 检测库；Scout 也不调用远程 LLM 做业务规划。

## 5. SDK、MCP、CLI 如何选，为什么

上一版以“进程隔离”为由默认推荐 MCP 不充分：**隔离进程和选择通信协议是两个问题，SDK 也可以运行在 Scout 管理的独立 worker 中。** Scout 是我们自己的 Python 应用，不是一个必须经 MCP 才能发现工具的外部 Agent。

| 接法 | 适合本项目的地方 | 代价 / 边界 | 选择 |
| --- | --- | --- | --- |
| **Python SDK** | `cua_driver` 提供异步调用、同一 Rust runtime、图像及结构化结果；容易保留请求上下文和会话，也可接已有 daemon | 锁定 wheel、原生库、系统架构和版本；typed API 存在版本迁移，平台特有能力可能要 generic call；直接加载的崩溃会影响宿主 | **默认路线**。按需放入独立 Desktop worker，用本地受控 IPC 与 Scout 通信。 |
| **MCP stdio** | 适合直接接外部 Agent、复用已有 MCP 客户端，或某发布版本 SDK 尚未覆盖的调用；独立驱动进程便于管理 | 多一层协议 / schema / 结果转换与图像传输；不能默认比 SDK 功能强或更稳定 | 保留适配选项，只有 PoC 证明有实际优势才切换；不是 Scout 到 Nexus 的协议。 |
| **CLI** | 安装、版本查看、诊断、人工排障 | 每步启动一次会丢失进程内捕获注册表，不能串起 capture → parse → action | 不作为运行时逐步执行主通道。 |

SDK 与 MCP 共用底层 Driver；二者都能使用可选 perception，不存在“SDK 不会看图，MCP 才会看图”的差别。类型化 SDK 的可移植字段可能少于平台扩展，需要保留 `structured_json / raw_json` 或对应 generic call；具体返回包装按固定版本适配，不混抄不同版本示例。

macOS 无论哪种方式都要解决稳定的权限宿主：官方 App / daemon 或受支持的嵌入宿主。使用 MCP 不会自动获得录屏 / 辅助功能权限，使用 SDK 也不表示可随意由后台服务控制登录桌面。

```text
Workbench → Nexus agent_loop → RouterProxy → Scout DesktopExecutor
    → CuaAdapter → 持久 Desktop worker（Python SDK / 合规权限宿主）
        → Cua Driver → 操作系统 / 被测应用
        → 可选 perception worker → 只返回图像区域
```

SDK 集成不必启用 Cua 云中继、Spaces、第二套账号系统或自主 Agent。Cua SDK / Lume 的 VM 管理是另一层可选能力，本期不和 Driver 接入捆绑。

## 6. 测试报告能增加什么，缺什么

### 6.1 数据来源对照

下表区分工具能力与本项目接入现状；默认比较 **Cua 桌面通道**，不能把它另有的浏览器通道数据自动算进来。

| 数据 | Playwright | Windows UI Automation / 配套采集 | Cua Driver 接入后的情况 |
| --- | --- | --- | --- |
| 截图与几何 | 页面截图、视口；当前 Mino 已使用 | UIA 给控件范围，窗口图像需另采集 | 可取得窗口 / 桌面图像、目标身份及截图元数据；捕获引用有失效校验，须保留原始来源。不是只有 Cua 能截图。 |
| 结构与属性 | DOM / ARIA、页面和页签上下文；当前 Mino 已接入部分 | AutomationId、ControlType、Name、Value、状态、模式和事件等，依控件提供者 | 返回可访问元素、引用和状态，但可能只是投影 / 裁剪结果；部分属性、模式、事件未必透传。需按目标控件核对，缺失不代表控件不存在。 |
| 视觉区域 | 不内置通用 OCR / 图标检测模型 | 不内置通用图像识别 | 装 perception 后可增加文字 / 图标区域及解析来源；无控件树页面仍可能得到候选框。未安装时没有这项增量。 |
| 动作的实际路径与证据 | 定位器动作 / 等待等；完整 Trace 须启用采集 | 原生调用与属性读回；统一证据需自己封装 | 标准化 `effect`、`route`、`delivery.mode`、读回 / 窗口变化证据和拒绝原因；可以解释“有发送但效果未确认”。 |
| 局部后置条件 | 定位器断言等，须明确条件 | 属性查询 / 事件加条件判断 | `verify_state` 返回 satisfied / unsatisfied / unknown；用于明确的局部条件，不等于视觉语义或业务成功。 |
| 网络、JS、DOM 调试 | 可采 HTTP 请求响应、HAR、JS 错误、DOM 快照与 Trace 等，需配置；本次未确认当前 Mino 已把这些全部写入报告 | UIA 不提供这类浏览器内部数据 | 普通桌面快照 / click 不带这些数据；Cua 浏览器功能要单独接入、单独验证，不宣称取代 Playwright Trace。 |
| 应用内部故障 | 浏览器事件可见部分，后端仍需日志 | 不自动给出业务异常堆栈、网络请求或数据库结果 | 同样不提供通用桌面业务堆栈 / 网络真相；必要时增加被测应用日志或专门观测接口。 |
| 完整测试轨迹 / 视频 | Trace、视频需启用并保存 | 需框架记录 | Driver 不是 Mino 报告库；视频 / 历史能力需另启用与验收，不能假设自动有录屏。其历史预览也不保存完整截图和输入，不能替代 Session。 |

因此，**对 Playwright，Cua 的增量主要是桌面跨应用和可选图像区域感知，浏览器内部调试信息未必更多；对 UIA，增量主要是跨平台统一接口、视觉辅助及动作证据契约，原生控件细节可能反而少。** 缺失数据应标“不可用 / 未采集”，不能拿截图猜成结构化事实。

### 6.2 如何接进当前报告

当前协议 `Result.extra / data` 可承载扩展，`RouterProxy` 会转入 `EventResult.raw_response`；现有报告已用 `session_events / session_meta`、截图引用、动作耗时和执行结果。新增数据还需要显式落库与 Workbench 展示，不能以“放进 raw_response”视为完成。

| 记录项 | 来源 / 落地办法 |
| --- | --- |
| `session_id / step_id / attempt_id / action_id`、目标窗口、请求坐标 / 控件引用 | **Mino 自己记录请求上下文**。Cua 动作回执是闭合契约，不保证回显这些字段，不能从 summary 文本反解析。 |
| capture ID、snapshot ID、截图原始尺寸 / 变换、图像引用 | 取观察结果，保留它们与当轮截图的关系；请求图与结果图分别关联动作。Scout 本机路径不能当作 Nexus 可读取的图像。 |
| 原生 `effect / route / delivery / evidence / error / escalation` | 原样保留 Cua 提供的事实，适配后再给 Mino 五态；不要丢掉 unverifiable，也不要把 escalation 自动执行。 |
| 控件 / 区域选中过程 | 存来源、被选引用、候选框、解析状态、版本和可用的置信度；不存在的字段留空。控件快照不完整或被裁剪时标记，不能用“未返回”验证“不存在”。 |
| `verify_state` 与业务断言 | 分栏展示“驱动局部验证”和“Nexus 用例断言”，分别关联实际证据；局部 satisfied 不能覆盖业务 fail。 |
| 性能与费用 | Scout 计截图 / parse / execute 耗时，Nexus 计模型耗时和 token；本地推理不是零成本，也不产生 LLM token 记录。报告记录驱动与扩展版本。 |

上述关联字段是 **Mino 拟新增报告结构**，不声称 Cua 原样提供全部字段。大图、完整树和区域集合按现有附件机制保存，事件只放摘要与引用；当前 `SessionWriter` 对列表和字符串有截断，不能把大 JSON 塞进去后声称证据完整。

Workbench 一个步骤应能看到：**执行前图 + 候选 / 落点叠加 → 实际 route 与动作效果 → 执行后图 → 局部验证 → 业务断言**。原图保留，框图只作辅助；模型理由来自 Nexus，Cua 不替模型编造理由。

## 7. 实施范围与验证

| 阶段 | 做什么 | 怎么判断值得接入 |
| --- | --- | --- |
| P0 选型验证 | 锁定 Driver 与 SDK 版本，在目标 Windows / macOS 应用跑同一组最小动作；检查权限、中文、截图、焦点、取消、陈旧引用和返回字段 | 列出 SDK 实际覆盖及缺口；仅有 MCP 优势的能力单独说明，不默认全链路用 MCP。 |
| P1 最小闭环 | SDK 适配、Desktop 设备槽、平台菜单、截图与输入、A 纯视觉、报告证据 | Workbench 能创建并查看 PC 用例任务；Nexus 完成原有观察—执行—验证链路，不出现第二套规划器。 |
| P2 感知对照 | 在同一数据、模型、重试预算下比较 A、B、C；B 额外安装已确认交付方式的 perception | 比较定位命中、一次运行成功、误通过、P50 / P95 延迟、模型 token、本机资源及证据完整度。B 若未改善目标自绘 / 中文用例，不默认开启。 |
| P3 扩展 | 推动 Cua 补齐目标控件缺口；按需求增加 Linux、Electron DOM 专用通道及隔离测试环境 | 每个平台独立验收，不把一组系统 / 应用的效果推广到所有 PC 软件。 |

建议固定 10 条代表性用例各跑 10 次，覆盖原生控件、自绘、重名按钮、滚动、窗口移动、中文输入和失败状态；目标一次运行成功率 ≥95%，已知失败不得误报通过。指标是待验收目标，不是已测结果；遗漏平台 / 不支持动作必须计入覆盖缺口。

**代码落点**：Scout 新增 `desktop/cua_adapter.py`、`executors/desktop_cua_executor.py` 与可选独立 worker；复用插件安装、探测、设备注册和回执。Nexus 修改 Desktop 路由、`UiChannel`、`action_space`、`agent_loop` 的模式载入 / 观察槽、提示词组装与回执判定；Workbench 增加插件状态、模式设置和报告明细。业务配置与能力目录仍在 Nexus 的 SQLite，通过 Workbench 管理。

**运行约束**：一个交互桌面首版只跑一个 PC 任务，与同桌面的 headed Web 共享锁；Driver 支持部分后台操作不等于可以撤销独占。锁屏、休眠、RDP 断开、权限撤销明确阻塞；停止任务应停止新动作并处理在途结果。恢复时重采快照，不重放未知结果。支持用户从 Workbench 暂停 / 接管 / 继续 / 停止，本机提供急停。

**安装与协议**：固定发布源、锁定包与原生库版本，SDK 与 Driver 同步升级；不要按已停止维护的 `cua-agent / cua-computer` 旧教程集成。双仓同步协议字段与 golden fixtures，并核验能力上报。按仓库要求以真实 UI 联调及 `session_events / session_meta` 验收，不新增或运行 pytest。

## 8. 选型与资料依据

Cua 官方仓库调研固定到提交 `5458c77b04a6e5e7a099929e0c4d4366e83dd403`。部分文档包含“下一次 breaking release”的 typed SDK 示例，因此实现时以实际发布包为准；不要把主分支文档等同于当前已安装 API。

- [Driver 组件边界](https://github.com/trycua/cua/blob/5458c77b04a6e5e7a099929e0c4d4366e83dd403/libs/cua-driver/README.md)、[Python SDK](https://github.com/trycua/cua/blob/5458c77b04a6e5e7a099929e0c4d4366e83dd403/libs/cua-driver/python/README.md)、[SDK 契约与可移植字段范围](https://github.com/trycua/cua/blob/5458c77b04a6e5e7a099929e0c4d4366e83dd403/libs/cua-driver/contract/README.md)。
- [视觉解析与捕获绑定](https://github.com/trycua/cua/blob/5458c77b04a6e5e7a099929e0c4d4366e83dd403/libs/cua-driver/docs/perception-extension.md)、[感知模型及许可](https://github.com/trycua/cua/blob/5458c77b04a6e5e7a099929e0c4d4366e83dd403/libs/cua-driver/docs/perception-third-party-notices.md)。
- [动作回执与后置条件验证](https://github.com/trycua/cua/blob/5458c77b04a6e5e7a099929e0c4d4366e83dd403/libs/cua-driver/docs/action-result-contract.md)、[窗口 SDK 迁移与快照完整性](https://github.com/trycua/cua/blob/5458c77b04a6e5e7a099929e0c4d4366e83dd403/libs/cua-driver/docs/native-window-sdk-migration.md)。
- 项目代码依据：Nexus `loop/agent_loop.py`、`loop/router_proxy.py`、`action_space/*`、`core/protocol.py`、`core/schemas.py`、`loop/observe/session_log.py`；Scout `core.py`、`dom_hierarchy.py`、`input_contract.py`。可靠性约束参见 [UI 自动化执行异常问题解决](UI%20自动化执行异常问题解决.md)。

Dots / Muse / Cue 可参考任务可视化与接管体验，OpenClaw 可参考节点 / 工具分层；它们不替代上述驱动、感知、规划、验证的明确分工，也不作为未经核实的 SDK 依赖。

## 9. 2026-10-12 纯视觉兼容性整改

### 9.1 案例事实与归因边界

`cr-5900abfcbdda::case-ed53e597::desktop:e22615fbbb917111:console` 共 37 条事件，18.033 秒。设备平台为 Desktop、系统为 macOS、目标为配置中的 bundle ID。第 1 回合租号成功；第 2 回合 `launch_app` 返回 blocked，原因是“被测 PC 应用未进入前台”，此后直接终止。尚未执行登录点击、输入、发码或业务断言，没有进入该案例的模型决策调用，不能将本次失败归因为模型定位或验证码判断。

历史 `observe/screen` 的 capture_id 与 session_generation 为空，程序链 tool/result 也没有 Scout/Cua 的完整诊断。可确认启动前台条件未通过；无法仅凭这份旧记录区分驱动激活拒绝、窗口尚未就绪或前台识别时序。修复不能将 blocked 强行改为 pass，也不能修改用例预期或数据库来绕过。

### 9.2 本次代码修正范围

1. macOS 启动按进程及实际窗口激活，等待窗口与前台状态有明确上限；遵循工具 schema，检查激活回执，保留失败阶段及驱动代码，不重复启动。前台判断统一以 NSWorkspace.frontmostApplication 的 processIdentifier 与 Cua 应用身份交叉确认，与 AX 键盘控件焦点分开；读取失败或身份无法关联时明确为 unknown 并阻塞。Windows 保留原生目标标识与系统路径。
2. 正确读取点击直接返回的 typed ActionResult，同时兼容输入/按键的 ToolResult 和 JSON。未验证的 HID 投递保留 `verification_required`，业务结果等下一次纯视觉观察；部分执行、拒绝与疑似无效果分别保留。
3. PC 输入摘要与 current_focus 投递契约一致。PC 验证码填写与账号填写统一进入看图执行链，不按 Web/Android 的旧节点或坐标直接派发。看图按键也纳入回看。
4. 截图元数据通过 Result.extra 传递并写入 Session Log；保留原始捕获尺寸、压缩后图像尺寸与捕获引用。截图失败清除旧观察引用，Retina 点击按原捕获像素反算千分比，不再叠加缩放或窗口偏移。
5. Desktop 页面身份、字段读回与补采观察明确分流，不按 Android 控件树处理，也不额外发起未支持的 hierarchy 采集。
6. 程序链与普通动作共用执行诊断记录，保留实际 executor、error、attempts、Scout 及 Cua 元数据，不包含输入正文或密钥。
7. 模型密钥页新增独立“PC 压缩”：`pc_compress_ratio` 默认 1，范围 1–10，贯通表单、HTTP、账号设置、截图执行与喂模。同一张图不叠加 Plan；即使 PC 设置为 1 也不额外缩小。旧客户端未发送该字段时保留已有设置。

### 9.3 验收边界

当前已完成静态编译、Workbench 类型检查及现有协议 golden fixtures 双仓 round-trip。未修改/运行 pytest，未写真实业务数据库。上述结果不能替代真实 UI 验收；需部署更新后的 Nexus、Scout 与 Workbench，再通过 Workbench 创建新任务验证窗口启动、中文输入、OTP、Retina 压缩后落点和业务断言。旧 Session 不回填；该案例尚无修复后的成功 Session，不宣称用例已通过或已覆盖所有 PC 应用。

本次针对 A 纯视觉执行及已有 Desktop 动作；B perception 与 C accessibility 仍按前文独立实施和验收，不因本次修正就标记为已接入。

### 9.4 重跑后的前台校验修正（2026-10-12）

`cr-f6d74d409816::case-ed53e597::desktop:e22615fbbb917111:console` 共 37 条事件：第 1 回合租号，第 2 回合实际派发 launch_app，返回 blocked，摘要为“无法读取 Mac 原生前台进程，请检查辅助功能权限并重新观察”。现有代码在该原生探针之前已调用 Cua launch_app、等待目标窗口并调用 bring_to_front；因此不能解释为“租号后只检查、没有启动或拉前台”。该 Session 未保留激活回执和读取错误类型，尚不能断言系统已完成激活或实际 Scout 缺少授权。

按用户选择保留当前桌面截图、坐标及驱动路线，先修启动/激活后的原生校验，不迁移整条执行链到 Cua 窗口托管。Mac 前台身份改读 NSWorkspace.frontmostApplication，避免将 AXFocusedApplication 读取失败直接视为应用未激活；AX 键盘控件焦点仍用于 current_focus 输入保护，录屏及辅助功能权限要求不变。

启动失败在窗口等待、激活、前台读取、身份匹配各阶段保留 launch 诊断：launched、activated、foreground_verified、pid、window_id、foreground_pid、foreground_package、foreground_source、foreground_error/error_type。前台读取在有界窗口内允许等待瞬态状态，不重复提交启动或激活。Cua 未提供该前台 PID 的完整身份时返回 unknown 并阻断；Nexus 的取证与菜单守卫共同消费 Desktop 前台结果，不再按 Android package 字段解析，也不把缺失 target_active 转成 no。

需更新并重启 Scout 与 Nexus 后，通过新 Session 验证第二步是否完成 foreground_verified，再检查首次截图、点击和焦点输入。旧会话不回填；尚无本轮修正后的成功案例证据。

### 9.5 指定窗口与 Cua 托管（2026-10-12，替代 9.4 的原生探针路线）

本轮按用户要求将既有 PC 设备操作统一交给 Cua Driver 0.34.1；保留 Nexus 用例决策、账号/数据资源、任务调度、独占租约、取消/预算、业务断言和 Scout 协议适配。没有迁移为另一套 Agent，也没有新接入 perception 或 accessibility 业务路线。

1. 首张 PC 截图前由 Nexus 显式派发 launch_app，Cua 启动/激活被测应用、等待普通窗口、bring_to_front(pid, window_id)，以 SDK activated=true 回执完成窗口验证，不再要求全局前台持续归属目标进程。窗口准备使用独立框架步骤号，避免与冷启动/版本读取的幂等缓存冲突；PC 不执行 Android 冷启动链。成功同步 app_launch_confirmed，失败保持 blocked 等实际状态，不再先截整屏让模型猜前台应用。
2. 每个 run 绑定 {kind:window,pid,window_id}。明确 window_id 时精确选窗；未指定时从应用窗口中选唯一可见或完整 z_index 中唯一最前窗口；不能唯一选择就 blocked。已绑定窗口丢失或所有者变化由 Cua 拒绝，不静默改选其他窗口或桌面。
3. SDK 通用 get_window_state 指定 pid/window_id、include_accessibility_tree=false、include_screenshot=true、display_only=false，返回注册窗口捕获。typed WindowStateOutput 未暴露 capture_id，使用通用结构化响应保留该引用；snapshot_id 不能替代它。PC 压缩由 Cua max_image_dimension 完成，压缩比大于 1 时先校准原尺寸再注册最终缩图；Scout 不再另行裁切、缩图或重编码。两次捕获有额外开销，只有最终图和引用进入模型动作链。
4. 点击只使用 ActionTarget.WINDOW(pid, window_id) + CAPTURED_COORDINATES(x,y,capture_id) + FOREGROUND。项目只把千分比落点转换成最终图像像素，屏幕映射、Retina/缩图转换、捕获消费与窗口几何校验全部由 Cua 负责。
5. current_focus 保留业务 focus_ref，Scout 只记窗口与字段落点；输入前用新捕获经 Cua 重新定位字段，窗口或尺寸变化则拒绝。replace/append 的选中/移至末尾、输入正文及 press_key 均绑定相同窗口并使用 foreground；取消/超时/代际变化清理引用与目标。自研 NSWorkspace、AX 焦点及 Win32 探针不再进入 Desktop 执行路径。
6. readiness 和权限消费 Cua health_report；跳过 screen_capture_capability，避免诊断抓整屏或申请权限。前台身份只用 Cua list_apps.active。项目代际跟随生命周期与 readiness 变化，捕获和动作的真实有效性仍以 Cua 的窗口准入为准。
7. Session Log 的 observe/screen 保留 capture_scope、pid/window_id、capture_id、snapshot_id、代际、原始/最终图像尺寸及缩放；tool/call 在准备动作执行前写入，tool/result.desktop 保留窗口目标、launch 分段与安全动作证据，便于核对截图与点击属于同一窗口。

验收边界：本轮需完成源码编译与固定版本 SDK 参数核对；本地独立 Python 的 Cua health 报告缺录屏/辅助功能授权，不能代表已授权 Scout 宿主，也不能据此宣称真实窗口截图和点击验收通过。更新 Scout 与 Nexus 后仍需在实际授权宿主跑新 Session，并核对窗口图像、点击、replace/append 与窗口移动/关闭拒绝行为。Windows 实机验收单独记录。

### 9.6 窗口截图前的重复前台门槛（2026-10-12）

最近两次任务的 Session 真源显示两种不同失败：

- cr-827d55828be4，20:27:42 开始，三个 case 都未记录首图前的 launch_app，截图返回“被测应用尚无指定窗口，请先执行 launch_app”，无 observe/screen。该轨迹没有走到新窗口准备步骤。
- cr-911dde39f9ae，20:28:53 开始，case-3973849e 的框架步骤 10004 已执行窗口准备。Scout 0.1.82 返回 launched=true、activated=true，SDK 代码 bring_to_front_exact_window_verified，目标 pid=87848/window_id=379214；但项目随后读取 list_apps.active 得到飞书 com.electron.lark，自己将状态改为 blocked。它在首张截图前退出，没有窗口截图接口失败的证据，也不能归因为录屏权限。

修正为直接消费 Cua 的窗口激活确认，不再以另一次全局前台读取推翻回执。activated 缺失或 false 仍阻断，不能仅凭请求受理放行。Nexus 的 ensure_target_app_foreground 对 Desktop 不执行全局前台恢复，避免窗口截图成功后仍因用户切换应用而反复启动。get_foreground_app 保留真实查询；截图继续要求指定 pid/window_id 和有效 capture_id，输入继续由 SDK foreground 模式做准入。launch 诊断增加 activation_code、window_verified，成功阶段为 window_verified；foreground_verified 仅表示 SDK 激活时的验证结果。

此次定位依据现有 Session 事件，源码修正不代表新版本已经部署或新 Session 已通过。需更新 Scout 与 Nexus 后验证首图出现及后续点击；没有将历史失败回填为成功。
