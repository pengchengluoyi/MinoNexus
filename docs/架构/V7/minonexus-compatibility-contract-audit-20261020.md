# MinoNexus：旧版兼容删除范围与全局合同差异

日期：2026-10-20。第二轮只读梳理。

本轮采用用户确定的方向：测试阶段不承担旧版本、旧数据、旧客户端兼容。但当前请求是继续梳理，因此未删除源代码、旧目录或业务数据，未重启进程。

## 一、结论与覆盖范围

不是只补一个 win 枚举即可。代码里同时存在规范平台合同、旧移动平台模型、桌面兼容映射、按设备名猜平台、按列表首项选渠道，以及不同层对空值的不同解释。旧版兼容让这些差异得以进入主执行链，但部分差异本身是当前实现缺陷，删除兼容后也必须修正。

本轮检索 `mino_nexus/` 和 `docs/` 中 504 个 Python、118 个 YAML、222 个 Markdown 文件。输出 1081 条候选索引：兼容/迁移类 899 条、平台合同类 99 条、目录类 83 条。同一源代码行可以进入多个类别；这些数字是检索命中，不是缺陷数。人工重点追踪执行、目录、平台、环境、节点、账号、导航和能力链路，未声称每个候选都已逐项证明可删。

配套文件：

- `minonexus-audit-index-20261020.tsv`：源文件、行号、候选类别、原文，可用表格筛选。
- `minonexus-contract-observations-20261020.json`：19 个纯函数观测，包含正常对照。
- `scripts/minonexus-contract-audit-02.py`：扫描与观测脚本，只读源文件，不导入应用、不连接数据库、不执行 pytest。

下面源码路径均相对于 `/Users/changpengcheng/code/MinoNexus`；行号为读取时位置。工作树存在其他未提交和并发修改，不是固定提交的全量审计。

## 二、先纠正一个容易误删的名字

`execution_mode='legacy'` 目前承担普通 Agent 执行，不能按字面当成可整段删除的旧兼容。

证据：`loop/case_runner.py:158` 把空值和 agent 归一成 legacy；约 200 行的 v5 分支要求已发布用例、固定 Web 节点和 headless。`services/execution_requirements.py:125` 的 ordinary_requirements 构造普通执行要求，并输出 legacy 模式。系统能力和 OTP 绑定也使用这一模式。

因此可以删除的是旧名称/旧输入适配；普通执行能力需要保留并确定唯一名称，例如 agent。不能把全部任务改成 v5，否则会改变 Android、原生桌面、临时指令等当前能力范围。这不需要保留旧版支持，而是需要区分“产品模式”与“合同版本”。

同理，session_atomic.py 中 legacy 也用于指标计数前缀；不是另一套可整段删除的存储实现。

## 三、旧版兼容清单与删除边界

“删除”表示在全量切换当前格式的改造中删除该分支并更新调用方，不表示可无依赖地单文件移除。

| ID | 类型与定位 | 当前行为 | 无历史兼容后的处理 |
|---|---|---|---|
| C01 | core/paths.py:20–25 | macOS 正式目录未显式指定时，发现 ~/.mino-nexus 可自动沿用 | 删除旧库自动选择；环境、目录必须确定 |
| C02 | core/paths.py；cli.py:364 | 两处目录检查，开发/正式不同环境变量；文档又给旧默认 | 收敛一个路径解析入口；保留开发/正式隔离 |
| C03 | core/database.py:77；core/migration.py | 启动备份、ALTER、旧字段回填、历史表/旧文件处理 | 新库使用当前模型建表；旧 schema 明确拒绝，不在启动时隐式升级 |
| C04 | core/bootstrap.py | 旧知识迁移和一长串历史 upgrade 混在初始化中 | 删除历史迁移链；保留当前系统能力与恢复规则的完整初始化 |
| C05 | services/project_store.py:24–72,108–123 | 缺创建者回填 admin；读取 root 时发现缺字段会保存 | 删除读时补写；新建入口要求显式合法归属；非法记录报错 |
| C06 | services/pool_account_store.py:126,149,380 | 旧 env.test_accounts 合并入表并移除旧 blob | 号池只认数据库当前表；移除惰性迁移及相关读写调用 |
| C07 | services/project_env.py:98,134,210,361,1018 | 从旧渠道 ID 推平台、旧 OTP/login mode 转换、旧环境形状转换、空项目取第一应用环境 | 当前环境按明确 schema 读取；不猜渠道、不继承第一应用 |
| C08 | services/application_channels.py；project_store.py:407 | 接管旧平台渠道关联；处理未明确 app_id 的旧关系 | 新渠道必须明确应用归属；删除旧关系自动冻结/推断 |
| C09 | services/nav_calibration_store.py:198–270 | DB 保存异常回退 JSON；读旧 JSON；旧草稿迁移 | 删除 JSON 双真源及异常回退；保存失败明确失败；保留当前 DB 草稿功能 |
| C10 | routers/rNavFsm.py:788；loop/hierarchy_slots.py:101 | 旧 walkthrough/calibration 的读取入口与目录开关 | 停止兼容旧采集入口；保留当前 captures 及其证据读取 |
| C11 | services/nav_route.py:339；nav_screen_registry.py:2917 | 旧 page.tab_* 清理、旧 FSM 状态到新图谱的映射 | 新图谱只用稳定当前 ID，删除历史重映射；正常名称/语义查找不是旧兼容 |
| C12 | services/platform_migration.py；platform_migration_apply.py | 历史平台审计、推测与离线迁移/回滚 | 不再承担历史迁移时可移除相关工具入口；当前校验器保留 |
| C13 | runtime/run_context.py:339；services/node_registry.py | 识别/清理 web-local、web_local 等全局旧槽 | 只接受当前每节点设备身份；删除旧槽退休、孤儿清理分支 |
| C14 | services/node_registry.py:560；core/protocol.py:177 | 心跳 v0 按执行器增量合并，v1 按完整快照替换 | 只接收当前完整快照版本；Nexus 与 Scout 同步改 |
| C15 | runtime/platform_gate.py；device_targets.py；router_proxy.py:1061 | desktop/pc/macos/windows/ios/browser/playwright 等旧平台接受与转换 | 运行时目标仅 mac/win/android/web；不得误删 host OS 与 executor 的合法命名 |
| C16 | services/ui_devices.py:60；node_store.py:220 | 同时输出 adb_state 等旧 UI 别名，保留 studio_id 元信息 | Workbench 同步消费当前字段后移除旧投影 |
| C17 | services/node_claim.py；routers/rRuntime.py:186 | 管理员认领历史无归属节点 | 新节点注册就绑定 owner；不再提供历史认领入口 |
| C18 | catalog/skill_channel.py:41,116；catalog/loader.py:53 | 能力旧名转换；缺 caller/phases 时从 kind、可见性、白名单猜测 | 初始化写全合同，加载时校验；移除运行时补猜及别名 |
| C19 | services/execution_requirements.py:275 | adapt_event 给旧实现补 runs_on、参数/结果版本、requires_caps 等 | 先把普通执行所需当前声明写完整，再删除桥接；本地执行器适配功能必须有正式声明 |
| C20 | loop/session_ensure.py:88 | 无结构化资源合同的用例，只要前置文本非空就可能要求租号 | 未编译用例不准入；删除从自由文本兜底决定租号的分支 |
| C21 | services/case_compilation.py:202,248；case_review.py:284 | 收养历史用例快照/任务；从是否有 v5 publication 猜模式 | 新用例明确编译版本和模式；删除 adopt_legacy 路径 |
| C22 | services/control_registry.py:139–170 | 新控件作用域记录旧 desc_key 控件来源；空项目回退 _project | 当前控件身份显式包含合法项目及作用域；移除旧身份引用和伪默认项目 |
| C23 | action_space/scheme.py:7 | 旧 dom 输入映射 dom_preferred | API 固定策略名称；每轮 execution_scheme 返回 dom 是执行方式，不宜机械删除 |
| C24 | loop/input_contract.py:138；email_login_auto.py:302；ui_sms_request.py:150 | 旧节点错误文本识别、旧函数别名、旧调用默认 Android 规则 | 切到当前结构化错误/签名；调用方同步；保留未知写入与焦点安全判断 |
| C25 | loop/action_fuse.py；loop/web_progress.py；ai/job_slots.py:42；runtime/session_gate.py:205 | 旧导入转发、旧模板槽、吞掉 **_legacy 参数 | 更新全部调用与模板后移除；不能只删除文件留下旧 import |
| C26 | services/collab_workspace.py:400；collab_steward.py；routers/rMinoBots.py | 旧议题迁移、旧消息清理、已废弃接口返回 410 | 新库只建当前工作区模型；删除历史迁移和废弃接口专用提示 |
| C27 | services/assistant_gateway.py:271；assistant_store.py:146 | 旧永久 inbound claim 去重识别 | 新消息只用当前幂等身份；删除旧去重键兼容但保留当前防重复执行 |
| C28 | services/plugins_store.py:211,492,515 | 禁用的共享密钥迁移代码；单 template 转 templates | 删除死迁移代码与旧模板形状；不执行死代码，也不迁移共享密钥 |
| C29 | routers/rReleases.py:82 | items[]、单对象、旧 os→arch map 都能解析 | 自有发布产物只保留一种清单格式；发布端与下载端同步 |
| C30 | services/run_store.py:35；session_store.py:311；observe/session_project.py | 旧任务截图剥离、无绑定合同提示、坏旧缩略图/旧回放形状处理 | 区分纯旧数据修复与当前 UI 投影；前者删，后者须先改当前消费者 |
| C31 | services/device_admission.py | 无预约行的历史任务仍参与占用判断；旧模式与 v5 混跑判断 | 新任务必须事务创建预约；删除历史补算后，保留当前模式的资源竞争规则 |
| C32 | services/run_write_safety.py:170 | 无 generation 的旧写意图借当前内存记录确认 | 当前写意图强制完整 generation；删除旧证据兼容，不能删除隔离保护 |

以下不按“旧版兼容”直接删除：第三方模型服务响应适配、Windows/macOS 系统 API 名称映射、ARM/x64 架构、数据库事务、账号权限、未知写入隔离、幂等回执、DOM 不可用时的正常视觉降级、导航目标自然语言解析、用户导入表头映射。若要缩减这些产品能力，需要另列产品范围，不应靠字符串搜索决定。

## 四、同类合同差异：逐项证据与处理建议

证据级别：A=本轮精确提取源文件纯函数验证；B=源码两端对照；H=上一轮验证。A/B/H 都不等于真机端到端复现。

| ID | 发现与定位 | 证据 | 应统一的行为 |
|---|---|---|---|
| D01 | runtime/session_gate.py:31,126：win 不在合法场景集合，变 android | H：clamp(win)=android | 场景合法值使用同一目标平台合同 |
| D02 | ai/schemas.py:69–81：CaseScene 只允许 android/ios/web/any，不支持 mac/win | B：Pydantic Literal 与平台合同冲突；未确认所有调用都使用该类 | 删除旧场景定义或改用统一 schema，不能遗留第二套枚举 |
| D03 | runtime/platform_gate.py:165：仅 mac/win 解析平台覆盖合同 | A：explicit web-only 在 android 返回 None，explicit 空集合在 web 返回 None | 四平台一致解释 all/explicit/空/缺失 |
| D04 | services/nav_target_scope.py:61：非 web/ios 一律 android | A：mac、win、无效平台均返回 android | 导航作用域保留原生平台，不猜 Android；kind 同步扩展 |
| D05 | services/device_store.py:97：有 sn 的 mac/win 落 adb | A：mac:node1:console 与 win:node1:console 都返回 adb | 明确 kind 是设备类型还是执行器；禁止由“有 sn”推 adb |
| D06 | services/control_registry.py:234：DOM 定位单元只分 android/ios 和其余 | A：mac/win 都选 playwright；router_proxy 将其派给 desktop | 区分原生桌面定位与 Web DOM；这是定位字段选择，不是已证明实际误派 Playwright |
| D07 | services/project_env.py:763：缺匹配平台仍回退同 kind 首项 | A：仅 Android app 渠道，请求 win 却返回 android-main | 无匹配返回明确缺配置；禁止跨平台选择 |
| D08 | services/project_env.py:775：显式 surface 不存在仍继续猜选 | A：surface=missing 返回 android-main；device_targets.target_channel 则会拒绝不存在的显式渠道 | 显式选择无效即失败，不可换目标 |
| D09 | project_env.py:62,775 与 :1037：合法渠道 ID 被 _slug 改写 | A：web.shop 被去点后，解析选成列表首项 web-first。写入允许点、大小写和至多64字符，_slug 转小写、删点、截24字符 | ID 原样匹配；显示别名可归一化，标识不可改写 |
| D10 | services/nav_target_scope.py:84：origin 不同还允许 URL 字符串前缀/包含 | A：example.com 与 example.com.invalid/path 判 True；调用方包括采集和恢复 | 精确比较解析后的 scheme/host/有效端口；子域或路径规则显式建模 |
| D11 | services/collab_bot_scope.py:10,62：pc 同时映射 mac/win，缺平台变 android，忽略集合覆盖字段 | A：platform_allowed(mac,[pc])=True；B：筛选读单值 platform/meta.platform | Bot 与执行准入使用同一平台集合，去掉 pc 泛化 |
| D12 | services/collab_bot_scope.py:94：设置 execute_surfaces 后，空 surface 仍被允许 | A：仅允许 web-test，surface='' 返回 True；collab_worker 调用该判定 | 先解析确定的 surface，再鉴权；缺失不能穿过有限白名单。尚未证明完整路径可绕过权限 |
| D13 | runtime/run_context.py:115；services/ui_devices.py:16；loop/router_proxy.py:_ONLINE | B：分别接受 connected/available/Authenticated；connected/online/available；小写 authenticated 等 | 输入边界规范一次在线状态，UI、菜单、派发统一消费 |
| D14 | catalog/registry.py:49；case_clause_events.py:419；execution_binding.py:185 | B：前两处空平台集合/空目标放行，绑定则要求目标在明确集合中 | 明确每种实体的覆盖语义，统一转成明确集合后判断 |
| D15 | catalog/loader.py:53；skill_channel.py:233；execution_binding.py:180 | B：空 phases 会被推导或不拦，绑定却要求明确包含 phase | 缺失与明确空集合分开；当前目录必须完整声明，拒绝缺字段 |
| D16 | catalog/registry.py:70；skill_channel.py:176；execution_binding.py:178 | B：菜单/派发辅助只排除 deprecated，绑定要求 lifecycle==active | 唯一 lifecycle 枚举；未知/非 active 不在前面放行后面拒绝 |
| D17 | catalog/loader.py:78；runtime/menu.py:107；services/desktop_catalog.py:40 | B：普通目录装 params 列表，菜单读取 parameters dict；桌面动态能力另塞 parameters | 参数表示明确转换；当前 tool_schema 还有独立构建，不能据此断言所有普通工具参数丢失 |
| D18 | loop/case_runner.py:141–158；routers/rCaseRunner.py；control_registry.py:36 | B：入口默认 agent，内部 legacy；请求摘要在模式归一化前生成，后续又按模式分路 | 模式一个名称/枚举；规范化后才计算请求身份，避免语义等价输入冲突 |
| D19 | services/device_admission.py:busy_for/admit_in_transaction | B：Web 提示固定4，实际准入按节点容量、隔离槽、v5队列 | 共享容量计算；保留原子提交校验，区分排队与立即可执行 |
| D20 | services/project_store.py:_root；pool_account_store.py:list_accounts | B：读取可能触发归属补写/旧号池迁移；execution_dependencies 明确绕开旧读取 | 读路径无迁移副作用，写路径显式校验 |
| D21 | core/paths.py；docs/ENVIRONMENTS.md；docs/排查手册/01、02；docs/开发手册.md | B：当前环境与旧路径指引混杂 | 一个目录解析合同，文档引用实际实例；Linux 的 ~/.mino-nexus 仍是当前默认，不能全局替换路径字符串 |

D04、D07、D09、D10 是本轮优先级最高的新增问题：会改变平台身份、目标渠道或站点归属。D05/D06 表明设备分类与定位路径仍停留在旧模型。它们不应等到兼容清理结束后再单独处理。

## 五、关于“统一目录”的准确含义

建议统一规则，而不是让所有环境共用一个目录：

- 开发：显式 development，使用确定的开发数据根。
- 正式：显式 production，使用确定的正式数据根。
- 删除 macOS 从新路径自动退回旧 ~/.mino-nexus 的行为。
- 旧目录存在不改变当前实例选择；不自动合并、搬迁或清理旧库。
- Linux 正式默认 ~/.mino-nexus 与 macOS 的历史同名目录不能按字符串一刀切；若要跨系统也统一命名，应一次修改解析器、CLI、服务安装、容器配置和文档。
- 所有排查示例先取得当前实例数据根，不默认猜一个 ~/.mino-nexus。
- 当前系统能力必须可以在全新空库初始化；不能依赖旧库经多轮 upgrade 后才有完整字段。

本轮没有删除任何目录。删除兼容代码、停止使用旧库、清理旧库文件是三个不同动作，清单只落实前两个的设计范围。

## 六、建议的唯一合同边界

| 概念 | 建议唯一表达 | 明确禁止的混用 |
|---|---|---|
| 目标平台 | mac / win / android / web | browser、playwright、adb 不能同时当平台 |
| 宿主系统 | mac / win / linux | Linux 宿主能运行 Web，不代表 Linux 是原生目标平台 |
| 执行器 | desktop / playwright / adb 等当前实际实现 | 不用 executor 名推 target platform |
| 平台覆盖 | scope + 显式集合，经过单一编译器 | 空值在一层全平台、另一层无平台 |
| 设备身份 | 注册时验证的 node_id + sn + platform | 用 sn 前缀或任意字符串猜身份 |
| 渠道身份 | 精确 channel ID + app_id + platform | 去点、截断、跨平台首项回退 |
| 执行模式 | 普通 Agent 与严格发布模式各一个正式名称 | legacy 同时表示功能模式和旧合同 |
| 能力调用 | event + caller + phases + lifecycle + implementation | 运行时从名称/白名单补猜关键合同 |
| 站点范围 | 解析后的 origin 与显式拓展规则 | URL 包含/前缀作为同站证明 |
| 在线状态 | 单一状态规范化和判断 | 三处各列 accepted states |
| 数据版本 | 当前 schema/version 明确匹配 | 读时升级、失败后退另一种真源 |

## 七、无旧版兼容的实施顺序

1. 固定当前目标合同与功能模式。普通 Agent 的能力继续存在；所有旧名输入明确拒绝。
2. 建立当前空库初始化：当前 ORM、当前系统目录、当前默认配置。去除启动和读取过程中的历史补丁迁移；旧库版本不匹配直接说明。
3. 同步修改平台/环境/能力的生产端和消费端：导入、模型 schema、目录加载、Bot筛选、菜单、绑定、导航、存储、派发均消费同一规范表示。
4. Nexus、Scout、Workbench 同步切换协议与 API；然后移除旧心跳增量、旧 UI 字段、旧入口和导入转发。只改 Nexus 不能证明整个系统已无旧兼容。
5. 使用新测试数据走当前链路，并通过 session 证据验收：身份不改写、目标不替换、菜单和绑定一致、资源准入/释放可解释。保留未知写入隔离，不以放开保护换成功率。
6. 对当前文档和生成模板做一次同步清理。历史设计文档可归档标记，不作为当前操作指南；搜索索引中的文档命中不能等同于活跃运行分支。

## 八、交付边界

本轮交付可用于实施排期的兼容清单、21 类合同差异及逐行候选索引，没有执行删除。纯函数观测确认局部行为，未启动服务、未接触设备、未重新查询业务数据库，也未跨仓验证 Scout/Workbench。运行中是否实际触发某个分支，需要后续以相应真实 session 证据核对。

这份清单不声称所有 fallback 都应删除：历史输入容错应退场；当前执行失败后的合法恢复、重连、视觉降级仍是当前产品功能，须用明确合同保留。
