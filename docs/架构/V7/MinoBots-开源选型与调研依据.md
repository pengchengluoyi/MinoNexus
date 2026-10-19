# Mino Bots：开源选型与调研依据

> **2026-10-19 流程已修订：本篇保留历史分析依据，人工接受/逐项批准/人工确认后推进的工作流不再有效。当前默认自动校验、自动应用、自动交接与执行；以 [自动推进与 Bot 生命周期设计](MinoBots-自动推进与Bot生命周期设计.md) 和 [全流程交付与现有框架影响](MinoBots-全流程交付与现有框架影响.md) 为准。用户纠正及必要事实补充不是例行审批。**


> 2026-10-18 补充修订：项目协作入口、开源组合及首期优先级，以 [方案复盘与 Workbench 交互设计](MinoBots-方案复盘与Workbench交互设计.md)、[同类项目扩展比较与复用落地](MinoBots-同类项目扩展比较与复用落地.md) 为准；实际价值与验证见 [直接收益与使用优化](MinoBots-直接收益与使用优化.md)。


日期：2026-10-18。配套：[产品场景](MinoBots-产品场景与总体方案.md)、[角色与 Job 复用](MinoBots-角色Job复用与全流程场景.md)、[架构落地](MinoBots-架构落地与分期验收.md)。

## 1. 调研口径

本轮读取了商业产品官方说明、公开 GitHub 仓库资料和部分关键配置。未登录使用商业产品、未运行候选开源项目、未验证其安全性、吞吐或模型效果。“公开实现/文档声明”不等于“在 Mino 验收通过”。部分搜索结果明显不相关，未采用；Grok Bot 最终通过官方开发文档核实，而非早期搜索摘要。

用户原文 gork bots 在本方案中对应 **Grok Bot**。Muse 对应 **Meta Muse**。OpenMuse 优先指 **CopilotKit/openmuse**。后两者不是同一公司产品，OpenMuse 也不应被称为 Muse 官方开源版本。

开源优先的目标是减少重复实现与维护，而不是把所有可见功能都装进 Mino。选型依次检查：真实用户场景、现有功能边界、是否可嵌入、身份和数据是否冲突、许可证/外部服务、运维成本、退出难度。

## 2. 首选组合与被否决的组合

**建议进入验证的主组合：assistant-ui（先验证既有事件适配）+ 已有 LangBot + 已有 Mino 角色/job/执行链；AG-UI 按接入需要采用，LangGraph / AgentScope 仅在现有编排不足时二选一验证。OpenMuse 提供任务与产物交互参考及适合移植的代码，Munder Difflin 提供角色交接参考。**

这不是已安装清单。依赖版本在 P0 验证后锁定；本文不擅自改代码或添加依赖。

| 方案 | 优点 | 代价与判断 |
| --- | --- | --- |
| 整体 fork OpenMuse | 快速得到个人助手、任务、浏览器、文件 UI | 需要从单 owner 改成团队项目授权；替换 Expo/Hono/PGlite 与现有 Workbench/FastAPI/SQLite；去除或接受外部 Intelligence。对当前目标改动面大，暂不推荐 |
| 整体 fork Munder Difflin | 已有 CLI 多角色、邮箱、看板、协作展示 | Electron/PTY、本地文件协作与 Mino Web/服务端分工不匹配；人类群组权限和测试语义仍缺失。不作为底座 |
| 自己从头做聊天、Agent 编排、渠道和存储 | 完全可控 | 重复最多、恢复与交互成本高；不推荐 |
| 嵌入开源组件，Mino 提供业务模型 | 保留已有身份、数据、测试和 UI；通用机制复用 | 需要薄适配层和一套业务版本/权限合同；推荐验证 |
| 同时引入 LangGraph、CrewAI、MAF | 看似能力全面 | 重复调度、记忆、追踪和版本升级；不采用。只选择一个主要协作框架 |

## 3. OpenMuse 深入评估

### 3.1 已核实的边界

公开版本快照：`1ac68f3909f2478ab6280883f1ab5ea65eb5719d`。[O1–O3]

- README 定位为 Alpha，单 owner 部署；UI 使用 Expo/React Native/Web，API 为 Hono，存储 PGlite 或 PostgreSQL。
- 有持久任务、SQL lease、计划、暂停/继续/取消、输入请求、审阅与收据。上游验证文档对真实浏览器、模拟模型和真实外部账号验收作了区分。
- 浏览器是独立 Chromium worker，另有可选 Linux 容器；当前验证说明不承诺敌对多租户隔离或完整桌面 VM。
- Rich Threads 在当前说明中要求 CopilotKit Intelligence key；该服务不包含在应用的 MIT 许可证里。聊天前端 follow-up 队列也不等于服务器持久任务队列。
- README 明示遥测及配置；应在实际集成前核对哪些数据外发、如何关闭，不能仅看到“self-hosted”就推断全部本地运行。
- 根 package.json 已包含 `@e2b/desktop`，但依赖存在不证明完整桌面能力已交付；成熟度以具体路径与验收说明为准。

### 3.2 逐模块取舍

| 模块 | 可复用价值 | 在 Mino 的做法 |
| --- | --- | --- |
| 活动/计划/产物卡 | 清楚表达“在做什么、缺什么、产出了什么” | 参考交互；用现有任务 ID、需求版本和证据 API 供数；React Native 代码需要适配 Web，不声称原样可用 |
| 输入队列、停止、草稿保留 | 减少持续任务中的输入丢失与串线 | 优先由选定聊天组件实现，再补 Mino 的任务状态映射 |
| AG-UI 接口 | 标准化文本、工具、状态事件 | 按外部接入需要采用协议/SDK；首期可复用现有事件 |
| Rich Threads | 会话列表与回放 | 不接默认外部服务作为首期真源；用 Mino 自有授权和存储，验证开源客户端可独立运行 |
| task worker/SQL leases | 持久任务思路 | 保留 Mino 现有业务任务；采用 LangGraph 的编排原语，不再并行运行 OpenMuse 任务引擎 |
| Chromium/容器执行 | 个人助理工作电脑 | 不接管 Mino 测试；设备工具仍在 Scout。以后独立的文档沙箱可单独评估 |
| Gmail、Calendar、财务/PDF流程 | 展示产物闭环 | 不是测试核心；不用为了“像 Muse”照搬这些连接器 |

如果无法摆脱外部会话服务而满足项目授权要求，仍保留 OpenMuse 作为交互参考，UI 使用独立开源组件。**不把这个限制变成从零手写聊天系统的理由。**

## 4. 候选项目比较

许可列是本次所读仓库 LICENSE 或 GitHub 元数据的范围，不涵盖商标、素材、全部依赖及云服务。正式引入必须按锁定版本保存 LICENSE/NOTICE，并确认是否有额外目录许可。

| 项目 | 场景与已读证据 | 许可/依赖关注 | Mino 结论 |
| --- | --- | --- | --- |
| CopilotKit/openmuse | 个人持续工作、计划、文件、浏览器；README、VERIFICATION、RICH-THREADS、package.json | MIT 应用；Intelligence 为另行配置服务；Alpha/单 owner | 深度交互参考，选择性移植，不整体替换 |
| assistant-ui | 可嵌入 React 的 Thread/Message/Composer、工具卡、附件和流式交互；README/ LICENSE | MIT；Cloud 是另一路径，选自托管后端适配 | Workbench 首选 UI 验证；Ant Design 外观适配，禁用通用“重试执行”直连副作用 |
| AG-UI | 前后端 Agent 事件协议与 SDK；README/LICENSE | MIT；协议不提供业务持久化/授权 | 按需采用标准协议；仍由 Nexus 审核每次动作 |
| LangGraph | 状态协作图、持久执行、中断、恢复、记忆；README/LICENSE | MIT 核心；LangSmith/托管服务不能混算开源核心 | 首选协作框架验证；作为 Nexus 模块，不另建业务后端 |
| CrewAI | 角色 Crews、任务/Flow、结构化输出；README/仓库元数据 | MIT 核心；AMP 为商业控制面 | 如果 LangGraph 角色配置成本明显过高，用同案例比较；不双引擎叠加 |
| Microsoft Agent Framework | 多语言 Agent、workflow、group collaboration、checkpoint；README | 本轮未逐目录核完许可；引入前补核 | Python 可接，但对现有栈暂无足够理由替代首选；保留备选 |
| AutoGen | 历史 AgentChat 与多角色协作 | 本次 README 明示归档/停止维护，而 API archived 字段为 false；许可证元数据也不能概括全部代码 | 不作为新核心；状态有冲突时采用保守判断，转看官方建议的 MAF |
| MetaGPT | 一句话需求→用户故事、需求、数据/API/文档；角色 SOP | 仓库元数据 MIT；README 安装段仍写 Python <3.12，需核锁定发行版 | 复用角色方法和模板；不整套接管测试流程 |
| ChatDev | 2.0 DevAll 多角色协作；README/仓库元数据 | Apache-2.0 元数据；模板、引擎和实际可嵌入性另验 | 开发文档与评审流程参考；不为模板另部署全平台 |
| LangBot | 多渠道机器人平台，当前 Mino 已有适配 | Apache-2.0 元数据；实际平台权限需逐个验证 | 继续采用已有 Scout 接入；不再写第二套飞书/企微网关 |
| Munder Difflin | CLI Agent 办公室、邮箱、角色、任务、Slack；README、架构、LICENSE | MIT 源码；README 有 Pro 功能，素材/商标不等于代码许可 | 参考角色交接与任务视图；不复制其全局共享记忆和文件邮箱作业务真源 |
| Orca | 多 CLI 编码、worktree、diff 评论、移动续聊；README | MIT 元数据及 README；依赖外部 CLI/账号 | 开发者可选工具，第二阶段通过任务合同连接 |
| Rome | 持续 Agent、Actions/Skills/Apps、渠道；README | MIT 元数据；功能范围广，未运行 | 更适合通用 Agent 平台；不与 Nexus 重建一套能力/存储体系 |
| Open Dots | README 明示早期原型、Claude-only、命名 Agent 尚无多 Agent；局部依赖 Boat | 搜索描述写 MIT，但 API license=null 且根 LICENSE 本次 404，许可未核清 | 不作为主方案；不能拿宣传“替代所有 Agent”推断团队能力 |
| nanoMuse | 跨设备个人 Agent；仓库描述 | GPL-3.0 元数据，部署/派生发行义务需评估 | 当前不优先；不把 GPL 项目直接复制进产品而不处理义务 |
| OpenClaw | 通用个人助手/工具运行时；README、仓库元数据 | MIT 元数据；有自己的渠道与运行时 | 沿用外部 MCP 客户端定位；不替代 Mino 官方入口 |

### 4.1 为什么首选 LangGraph，而不是“所有事情 CrewAI 化”

Mino 当前已存在专业角色、JSON jobs、规则 jobs、测试任务和审阅门禁。缺口主要是持续议题、多角色交接和版本化状态，而不是完全缺少 Agent。因此优先采用能在节点里直接调用现有函数的框架。LangGraph 节点可以是规则函数、现有 job 适配或持久任务交接，不强制每个角色再拥有一套模型客户端和记忆库。

CrewAI 的 Agent/Task/Flow 与用户想要的团队模型很贴近，应作为真正备选而非装饰项。P0 用同一退款案例验证角色配置、工具包装、人工中断、恢复以及身份注入；若其适配总量更小且合同通过，可以改选 CrewAI。比较完成后只保留一个主引擎。

### 4.2 为什么不一上来部署向量数据库或知识图谱

项目已有文档、FTS、向量字段和应用资料。先做好来源、版本、权限过滤与引用，验证检索是否足够；只有真实召回问题才评估专门组件。Bot 对话摘要和项目事实不可因为向量相似就合并，知识图谱也不能自动解决事实冲突。

## 5. 开源复用的具体交付清单

| 要减少的重复工作 | 首选复用 | 需要的 Mino 适配 | 不应自行扩大的范围 |
| --- | --- | --- | --- |
| 聊天输入/消息/流式 UI | assistant-ui primitives + 现有事件适配（AG-UI 可选） | actor 标签、议题选择、MinoCard、权限错误与续接 | 不自行造通用聊天组件库；不默认用云历史 |
| 长任务与角色交接 | LangGraph StateGraph/中断/检查点 | 当前 job、工具、owner、版本、收据适配 | 不新写图执行引擎；不把框架恢复等同设备安全重放 |
| 角色分析方法 | 现有角色/jobs + MetaGPT/ChatDev 可许可模板 | 补设计和开发合同、共享引用 | 不把模板当已实现服务 |
| 群平台消息 | 已有 LangBot | thread→topic 映射、披露过滤 | 不重新实现各渠道 SDK |
| 代码开发协作 | 已有开发链 + 后期 Orca/CLI 等 | 授权项目、分支、产物与任务 ID | 不默认复制终端宿主或任意 shell 网关 |
| 研究/报告 UI | OpenMuse 交互/可移植片段 | 文档版本、证据与需求卡 | 不复制其账户、云会话和浏览器执行系统 |

“Mino 新增业务逻辑”集中在需求/设计/开发/测试间的语义关系、权限、版本与证据。它不是通用轮子的替代品，而是开源工具不知道的产品业务。

## 6. P0 必须解决的选型问题

1. assistant-ui 的多 actor 消息、工具卡、React 19、AG-UI 是否能在现有 Vite/AntD 页面中直接工作？任何 Cloud key 缺失都不能让本地会话不可用。
2. LangGraph 能否调用现有 `qa_cover`/规则 jobs 和模型账号配置，不重复发送模型请求？中断后 owner、输入 revision 和 pending action 是否保留？
3. 框架检查点与业务 SQLite 的分工能否明确？检查点不得成为第二份需求/用例真源，恢复不得重复业务副作用。
4. 不部署 OpenMuse 的 Hono/PGlite/Intelligence，也能否实现计划、产物、待定问题和连续讨论体验？
5. LangBot 的真实群线程、编辑、引用和消息重投元数据是否满足映射需求？无法提供时在 Workbench 建规范议题，不假装跨渠道无损。
6. 复制代码/模板是否保留上游通知？模型、云服务、遥测、商业扩展和资产许可是否分别明确？

验证只在独立数据目录和批准的测试环境进行。遵循本仓约束，不给 Nexus 提交 tests/mock，也不为本次文档运行 pytest；实施验证以 HTTP/真实页面与任务/session 记录为准。

## 7. 来源与定位

以下链接于 2026-10-18 读取；动态分支可能变化，正式开发前锁定 commit/tag。公开官方页面中的描述属于该产品，不构成 Mino 的现状证明。

### 商业产品的直接依据

- **G1** [Grok Bot overview](https://docs.x.ai/grok-bot/overview)：长期 Bot、持久电脑、上下文与交接。
- **G2** [Create and manage Bots](https://docs.x.ai/grok-bot/bots)：角色、复制/模板、记忆边界。
- **G3** [Use cases](https://docs.x.ai/grok-bot/use-cases.md)：Bug Reproduction、Product Performance 等具体场景。
- **G4** [Message and collaborate](https://docs.x.ai/grok-bot/chat-and-collaboration.md)：群聊、@、任务交接与群内能力限制。
- **G5** [Team Bots](https://docs.x.ai/grok-bot/team-bots.md)：共享 Bot 与私人交流、身份、Slack、记忆及电脑边界。
- **G6** [Skills and routines](https://docs.x.ai/grok-bot/skills-routines-and-automations.md)：先成功案例、再技能、再计划任务。
- **M1** [Meta Muse 官方介绍](https://about.fb.com/news/2026/09/introducing-muse-personal-ai-agent/)：长期目标、后台工作、访问控制、行动记录。属于官方功能说明，非独立评测。

### OpenMuse

- **O1** [README 固定快照](https://github.com/CopilotKit/openmuse/blob/1ac68f3909f2478ab6280883f1ab5ea65eb5719d/README.md)。
- **O2** [VERIFICATION 固定快照](https://github.com/CopilotKit/openmuse/blob/1ac68f3909f2478ab6280883f1ab5ea65eb5719d/docs/VERIFICATION.md)：上游已经验证和未验证的范围。
- **O3** [RICH-THREADS 固定快照](https://github.com/CopilotKit/openmuse/blob/1ac68f3909f2478ab6280883f1ab5ea65eb5719d/docs/RICH-THREADS.md)：外部服务、单 owner、队列与真实任务的区别。

### 其他开源依据

- [assistant-ui](https://github.com/assistant-ui/assistant-ui)、[LICENSE](https://github.com/assistant-ui/assistant-ui/blob/main/LICENSE)。
- [AG-UI](https://github.com/ag-ui-protocol/ag-ui)、[LICENSE](https://github.com/ag-ui-protocol/ag-ui/blob/main/LICENSE)。
- [LangGraph](https://github.com/langchain-ai/langgraph)、[LICENSE](https://github.com/langchain-ai/langgraph/blob/main/LICENSE)。
- [CrewAI](https://github.com/crewAIInc/crewAI)、[Microsoft Agent Framework](https://github.com/microsoft/agent-framework)、[AutoGen 当前 README](https://github.com/microsoft/autogen)。
- [LangBot](https://github.com/langbot-app/LangBot)、[MetaGPT](https://github.com/FoundationAgents/MetaGPT)、[ChatDev](https://github.com/OpenBMB/ChatDev)。
- [Munder Difflin 固定快照](https://github.com/HarnessMD/munder-difflin/tree/78beb625d1c81cb18894573b0952094609e570a3)、[架构说明](https://github.com/HarnessMD/munder-difflin/blob/78beb625d1c81cb18894573b0952094609e570a3/docs/ARCHITECTURE.md)。
- [Orca 固定快照](https://github.com/stablyai/orca/tree/1938bd055aedd349d07172056fec6810992d5e0e)。
- [Rome 固定快照](https://github.com/rome-os/rome/tree/359944291738580124abced371bc7f8a8cb7f9e7)。
- [Open Dots 固定快照](https://github.com/Anil-matcha/open-dots/tree/96bdc5f8adc47c260a82451920871eba61ff673d)、[nanoMuse](https://github.com/nano-muse/nanoMuse)、[OpenClaw](https://github.com/openclaw/openclaw)。

未采纳搜索结果中的提示词泄漏仓库，也没有把任何泄漏内容作为产品功能或开源实现依据。
