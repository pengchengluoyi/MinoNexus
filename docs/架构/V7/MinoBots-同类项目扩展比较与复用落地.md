# Mino Bots：同类项目扩展比较与复用落地

> **2026-10-19 流程已修订：本篇保留历史分析依据，人工接受/逐项批准/人工确认后推进的工作流不再有效。当前默认自动校验、自动应用、自动交接与执行；以 [自动推进与 Bot 生命周期设计](MinoBots-自动推进与Bot生命周期设计.md) 和 [全流程交付与现有框架影响](MinoBots-全流程交付与现有框架影响.md) 为准。用户纠正及必要事实补充不是例行审批。**


调研日期：2026-10-18。补充首版仅少数产品深入比较的不足。以下区分：公开项目材料支持的能力、对 Mino 的适用性判断、未来需要验证的接入方案。未安装或实际运行这些平台，不把 README 的能力陈述当作已完成生产验收。

## 1. 不只看“像不像 Muse”

Mino 需要同时处理人类协作、Bot 工作分配、开发交接和测试证据；没有一款被调研项目能够开箱替代全部现有架构。比较应分为团队代理产品、多人 AI 工作台、流程/编排工具、测试管理/报告四组。后两组不是 Muse 的直接替代品，但对我们的实际落地同样重要。

复用分四种：直接引入库；通过服务接口集成；借鉴交互但不复制代码；暂不引入。源码可用不等于抽出一个组件就能独立运行；部署整个产品也不等于降低维护成本。

## 2. 团队代理、开发代理与持续任务产品

| 项目 | 公开产品的典型用法 | 对 Mino 有用的场景 | 复用方式与成本 | 判断 |
| --- | --- | --- | --- | --- |
| Eigent | 桌面 Cowork，把一项任务交给 workforce；并行专家处理材料与产物 | 多人需要看到谁在处理、哪些任务等待；日志调查与报告交接 | Apache-2.0 根许可证；桌面 React/Electron 与 Python/CAMEL 体系。借鉴任务进度、角色工作分配；整套搬入会增加桌面端及执行运行时 | 高价值产品参考；不以其桌面替代 Workbench。云快捷启动和本地自部署条件分别核查 |
| OpenHands / software-agent-sdk | 开发代理在工作空间处理代码任务；控制面组织任务与开发结果 | 把已确认需求、验收标准交给开发代理，回收 patch/提交/报告 | SDK 根许可证 MIT 已核实；可后续用 SDK 做独立开发适配。Canvas/其他包许可需分别核对 | P3 重点候选。代码工作空间与 Scout 设备执行分开，不让 Nexus 获得任意设备控制 |
| DeerFlow | 基于子代理、技能、沙箱与记忆推进复杂调研/产物任务，提供嵌入式 Python client | 跨文档需求分析、大型方案报告、分段处理产物 | README MIT；已有完整 runtime/gateway，嵌入 client 不代表可以绕开全部依赖。先比较是否比现有 job 链更省适配 | 专项研究候选；不与 LangGraph/现有助手重复部署一套持久任务系统 |
| Paperclip | 以目标、任务、负责人、预算和适配器管理 AI 团队 | 谁负责、何时阻塞、成本是否超限、任务如何交接 | MIT 根许可证；Node/React 完整应用。任务卡/责任人/预算交互可参考，整套引入增加组织及任务真源 | 借鉴轻量责任与成本展示，不把 Mino 做成公司组织模拟器 |
| OpenMuse | 围绕持续目标形成对话、工具调用和产物 | 长任务可继续、在产物旁反馈、显示行动记录 | 首版已核实 MIT 应用、alpha、单 owner 等限制；Rich Threads 外部服务依赖不能混算为开源自托管能力 | 挑选交互与可独立模块，不整体 fork 后端 |
| Grok Bot / Muse | 品牌角色、持续任务及协作体验，参考官方场景 | 产品交互对标和用户预期 | 商业产品不是开源依赖；团队 Bot、Bot 群和多人共享空间要分别定义 | 作为对标，不进入软件依赖清单 |

实际使用例：一个支付 bug 需要产品解释规则、测试复现、开发定位。Eigent 的“分工可见”适合呈现过程，Paperclip 的负责人适合防止无人接手，OpenHands 适合后续代码任务，而真实设备复现仍由现有 Scout 完成。不能把四个平台全部部署来完成这一条流程。

## 3. 多人 AI 工作台与知识协作

| 项目 | 已公开能力与典型场景 | Mino 适用范围 | 接入与许可限制 | 结论 |
| --- | --- | --- | --- | --- |
| LibreChat | 多用户认证、多模型、Agent、MCP、Artifacts、会话检索 | 个人助手与团队 AI 门户，文档/代码产物并排查看 | MIT 根许可证已读；是完整应用，重用账户/会话体系会与 Nexus 重叠。多用户支持不能推导为所有线程多人共同编辑 | 产物与会话搜索参考；若只需聊天组件，优先更小的 assistant-ui |
| LobeHub | Agent 工作台、记忆/知识与协作体验 | 专家配置、项目材料组织、长期讨论体验 | 当前 LobeHub Community License；修改并分发衍生作品要求商业许可，不能当作普通 Apache 库随意移植 | 交互研究；不列入默认免费白标复用 |
| Open WebUI | 多模型工作台；README 的 Channels 提供人和模型共享时间线、@、线程等 | 最贴近“人类群组＋Bot 在一个议题持续沟通”的 UI 参考之一 | 当前许可含品牌保留条件及适用例外；历史组件按 LICENSE_HISTORY。完整应用仍有独立账户/权限/知识体系 | 优先研究群协作体验，生产白标集成前单独审查许可，首期不整体嵌入 |
| LangBot（已有） | IM 消息渠道、Bot 应用与插件 | 把已有 IM 群消息关联到项目议题 | 复用已接入版本和签名/绑定链；一次只选一个渠道核对线程、引用与撤权 | 保留；不因新 UI 再建一套 IM 网关 |

多人沟通产品值得借鉴的重点不是头像排列，而是：回复绑定哪条消息、Bot 是否自动响应、谁能访问引用文档、后来加入的人看哪些历史、离开项目后还能否打开证据。Mino 首期采用项目成员继承与必要的议题访问控制，不复制一个通用企业 IM。

## 4. 工作流与 Agent 编排

| 项目 | 实际使用场景 | 可以复用什么 | 代价与不采用边界 |
| --- | --- | --- | --- |
| Dify | 业务团队搭建工作流、RAG 和工具应用，再通过 API 调用 | 若客户已有 Dify，可把某个分析应用当外部工具 | 修改版 Apache；多租户环境需授权，使用其前端不得随意去标。默认不拿来做 Mino 多租户后端，不复制工作流编辑器 |
| Flowise | 可视化构建 LLM 流程和 Agent | 已有 Flowise 客户的特定工作流可接口接入 | LICENSE.md 明确 enterprise 目录及特定文件为商业许可，其他主要 Apache-2.0；不能把企业能力一并算免费 |
| Langflow | 可视化 Python Agent/工作流、API/MCP 服务 | 快速试验外部分析工具 | MIT 根许可证；整套部署增加运行服务与模型设置。适合外部工具，不替换现有 job 真源 |
| AgentScope | Python Agent 团队、TeamPipeline leader/member、消息与工具组合 | 角色交接、受控小组讨论、工具/job 适配 | README Apache-2.0；2.0 API 与旧资料有差别，SOP 等实验能力不作为必需。必须验证现有模型客户端和身份是否能复用 |
| Agno | Agent/团队库及服务化运行、工具、存储、审批 | 有需要时比较轻量 Agent 库接入 | 当前根 LICENSE 为 Apache-2.0，不沿用历史许可印象；AgentOS 全栈部署可能重复 Nexus API/状态，不默认引入 |
| LangGraph | 用状态节点管理分支、持久执行和中断 | 已有 job 作为节点，明确版本状态与人工等待 | MIT 核心；checkpoint 不代替业务数据库。interrupt 恢复会重新进入节点，前置副作用需幂等；thread_id 必须稳定 |
| CrewAI | 按角色/task/flow 组织协作 | 角色声明体验与少量专家任务 | MIT 核心与商业 AMP 分开；额外模型调用/记忆层适配需评估，不与 LangGraph 叠加 |
| MetaGPT / ChatDev | 多角色软件开发流程示范 | 研究需求→设计→开发职责和交接材料 | 首版已列；整套软件公司流程与 Mino 既有执行链差异大，不作为首期运行底座 |

**修订首版结论：不再预先锁定完整“assistant-ui＋AG-UI＋LangGraph”组合。**

先用现有 job/助手编排打通单条链；UI 优先验证 assistant-ui 的 ExternalStore/custom runtime 与现有事件兼容性。AG-UI 适合统一第三方 Agent 事件，但不是多人权限、消息持久化或测试状态机。若现有编排确实无法可靠处理交接和等待，再用同一个真实案例比较 LangGraph 与 AgentScope，仅选择一个；CrewAI/Agno 为条件性后备，不全部试接。

选择依据不是功能最多，而是已有 job 无需重写、模型账号不复制、恢复不重放副作用、人工等待不丢版本。若两者都增加复杂度，继续已有编排也是合格结果。

## 5. 测试领域也必须比较

| 项目 | 典型场景与公开依据 | 对 Mino 的价值 | 接入建议 |
| --- | --- | --- | --- |
| Kiwi TCMS | 手工/自动测试管理，用例、计划、执行、缺陷集成与 API | 学习用例和执行组织；客户已有系统时关联外部用例/计划 ID | 与现有用例/任务重叠，首期不迁移；连接器仅在明确客户需求时开发。此次未完成固定版本许可审查，不推荐直接搬代码 |
| Allure Report | 聚合多框架执行结果，显示步骤、附件与报告 | 跨团队交付测试报告，统一外部 CI 报告阅读 | 可后续增加结果导出/链接；开源 Report 与商业 TestOps 区分。不得通过格式转换把 Mino 未验证状态映射为 passed |
| ReportPortal | 集中自动化结果、日志、失败分析与缺陷系统集成 | 执行量大时研究失败聚类与结果趋势 | README Apache；项目多服务，明显重于首期需求。已有客户部署才优先接入，逐组件核对版本许可，不新建第二个证据中心 |

这些产品补足“测试协作与报告”的比较维度。Mino 的差异点应是需求到真实设备证据的连续链，而不是重做 Kiwi 的所有管理页面或 Allure 的报告站。

## 6. 对比如何落到具体场景

| Mino 场景 | 优先参考/复用 | 必须保留的 Mino 业务 |
| --- | --- | --- |
| 产品、测试、开发在一个议题确认需求 | Open WebUI Channels 的线程体验；assistant-ui 组件候选 | 项目授权、业务基线、成员与执行 owner |
| 一个 Bot 分配任务，另一个产出用例 | AgentScope TeamPipeline 或 LangGraph（条件性） | 现有角色/job、预算、版本合同 |
| 长任务中途补一句新要求 | OpenMuse/DeerFlow 持续任务交互 | 输入 revision、取消范围、已有收据 |
| 测试失败需要开发改代码 | OpenHands SDK（后期） | 确定的工作区、变更引用、真实 build 和复验 |
| 负责人想知道卡在哪 | Paperclip 责任人与阻塞展示 | 现有任务状态与真实证据 |
| 客户已有 AI 工作流 | Dify/Flowise/Langflow API，先明确许可/身份 | 不接管客户平台；外部工具只返回候选 |
| 客户要标准化测试报告 | Allure 格式适配/原报告链接 | 版本、目标、attempt 和未知状态不丢失 |

## 7. 具体复用步骤与退出条件

### 首期直接考虑

- assistant-ui：只接消息、输入、引用、工具结果渲染，外观沿用 Workbench。先验证 React 版本、长消息、多 actor metadata 和现有事件订阅，不依赖云历史。
- Mino 现有组件和服务：用例、审阅、版本链、执行证据、助手待输入、IM 网关优先复用。
- 文档编辑先用现有文本/Markdown能力；差异与引用为业务层。未证明富文本需求前不引入独立文档系统。

### 明确缺口后再引入

编排库只做协作控制，节点调用既有 job；模型凭证由 Mino 执行用户配置提供。不得同时启动原 job 和框架自建 Agent 去做同一分析。框架检查点保留业务 ID，不把业务结果迁移到另一套存储。

开发代理使用适配器提交版本化工作说明，返回可审阅变更引用。需要单独确定代码执行环境与权限；本次方案不启动服务或安装 SDK。

### P0 小规模验证（未来执行）

统一案例：需求 r1→分析→等待产品答复→r2→生成候选→中途恢复→逐项接受。比较新增依赖/服务数、需要重写的 job 数、重复模型请求、owner 传递、恢复行为和输出完整性。再覆盖并发更新、重复接受、撤权和无模型配置。

合格条件是复用现有能力且闭环正确，不是必须选出新框架。需要替换账号体系、数据库真源或 Scout 链的方案退出首期。未完成版本/许可/依赖审查的模块不能进入发布清单。

## 8. 来源与证据边界

本轮读取公开 README、许可证和部分官方说明；默认分支会变化。实施时固定 commit/tag，保存依赖清单、许可证及 NOTICE；本轮不把默认分支视作可复现发布锁定。首版已记录的特定 commit 仍见《开源选型与调研依据》。本节链接同时提供可复核入口，不声称每个项目都做过源码级集成测试。

- [Eigent](https://github.com/eigent-ai/eigent)、[LICENSE](https://github.com/eigent-ai/eigent/blob/main/LICENSE)
- [OpenHands](https://github.com/OpenHands/OpenHands)、[software-agent-sdk](https://github.com/OpenHands/software-agent-sdk)、[SDK LICENSE](https://github.com/OpenHands/software-agent-sdk/blob/main/LICENSE)
- [DeerFlow](https://github.com/bytedance/deer-flow)，README 的 Embedded Python Client 与部署说明。
- [Paperclip](https://github.com/paperclipai/paperclip)、[LICENSE](https://github.com/paperclipai/paperclip/blob/master/LICENSE)（分支以仓库默认分支为准）。
- [LibreChat](https://github.com/danny-avila/LibreChat)、[LICENSE](https://github.com/danny-avila/LibreChat/blob/main/LICENSE)
- [LobeHub](https://github.com/lobehub/lobehub)、[LICENSE](https://github.com/lobehub/lobehub/blob/main/LICENSE)
- [Open WebUI](https://github.com/open-webui/open-webui)、[LICENSE](https://github.com/open-webui/open-webui/blob/main/LICENSE)、LICENSE_HISTORY。
- [Dify LICENSE](https://github.com/langgenius/dify/blob/main/LICENSE)：工作空间多租户及前端品牌附加条件。
- [Flowise LICENSE](https://github.com/FlowiseAI/Flowise/blob/main/LICENSE.md)：商业目录及文件边界。
- [Langflow](https://github.com/langflow-ai/langflow)、[LICENSE](https://github.com/langflow-ai/langflow/blob/main/LICENSE)
- [AgentScope](https://github.com/agentscope-ai/agentscope)、[TeamPipeline](https://docs.agentscope.io/latest/en/building-blocks/pipeline/team)
- [Agno](https://github.com/agno-agi/agno)、[LICENSE](https://github.com/agno-agi/agno/blob/main/LICENSE)
- [assistant-ui ExternalStore](https://www.assistant-ui.com/docs/runtimes/custom/external-store)、[AG-UI runtime](https://www.assistant-ui.com/docs/runtimes/ag-ui)
- [LangGraph durable execution](https://docs.langchain.com/oss/python/langgraph/durable-execution)、[interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)、[AG-UI events](https://docs.ag-ui.com/concepts/events)
- [Kiwi TCMS](https://github.com/kiwitcms/Kiwi)、[Allure Report](https://github.com/allure-framework/allure2)、[ReportPortal](https://github.com/reportportal/reportportal)

具体许可适用以采用版本原文为准：Dify/Flowise/LobeHub/Open WebUI 不应笼统标为“全部功能自由开源”。尤其 Open WebUI 当前品牌限制含滚动 30 天不超过 50 名最终用户等例外，不能将例外推广为 Mino 商业分发的普遍许可。
