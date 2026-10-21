# V5 方案一：执行侧范围、代码事实与决策记录

> **历史路线，仅供追溯，不作为当前默认观察策略。** 当前入口见 [V5 阅读入口](../README.md)，默认路线见 [DOM 优先方案](../方案/DOM优先执行与用例全覆盖开发方案.md)。

> **2026-10-14 新的开发方向**：[DOM优先执行与用例全覆盖开发方案](../方案/DOM优先执行与用例全覆盖开发方案.md) 将默认观察策略调整为“有效结构优先，结构不足用截图兜底”，并要求 Scout 端提炼、每步截图留证。本文原“看图优先/纯 DOM 不改道”和 Web 首期范围属于原阶段设计；新默认与平台覆盖按新方案实施，既有严格合同的写安全和证据要求继续保留。本轮为文档审计，尚未部署新策略。



> **前端架构硬约束（2026-10-14）**：Mino 项目只有 **MinoWorkbench** 一个前端。MinoConsole 与 MinoStudio 已取消，不再开发、构建、交付或作为配置/验收入口。管理、用例、环境、控件配置和证据查看全部在 Workbench；Nexus 与 Scout 保持后端和执行节点职责。历史文档中的 Console/Studio 入口均以 Workbench 为准。“三端”仅指 Web、移动、PC 被测平台。
> R8，2026-10-13。定位：从“能跑完”到“每个结论可举证”。V5 已取消用例生成需求。
> [方案二：唯一合同规格](../方案/三端UI自动化结果可信性方案.md)；[方案三：执行侧交付与验收清单](../方案/执行侧交付与验收清单.md)。第三篇已更名；两篇其他文件保留现名，标题与内容以执行侧为准。
> R8 当轮仅修订设计。2026-10-14 起的实现与验证范围见 [PR-A 开发记录](../执行开发与验收记录.md#record-pr-a)；不将局部实现视为整体合同或真实验收完成。

## 1. 结论与交付边界

R8 按五条反馈调整：①写动作结果未知不再等于“用例作废”，改为 case 级自动重试（新 run），run 内仍禁止续写；②保留文字用例导入，取消手写计划格式；③密码止漏是独立热修复，不阻塞 demo；④来源字段与控件字段精简；⑤并行改为单 Scout 内 lane 租用，不再要求多 lane 隔离矩阵。控件双真源、Web BACK、运行期定位回写的判断保持。Web 登录仍是首个切片；三端视觉校准并行采集，不把 Web 成功外推到移动端或 PC。

| V5 首期交付 | 明确范围 |
| --- | --- |
| 现存缺陷止血 | 密码出口、Web 输入法编排、同回合多设备动作复键、运行期定位回写 |
| 写安全 | wire 编号、原子在途登记、可靠写边界、禁写重发、全 run 门闩 |
| Web 准出 | 严格目标动作、定向原始事实、固定原子谓词、动作后来源、证据原子终结 |
| 文字用例导入 | 复用现有导入/编译（模型只在编译期），V5 资格检查、预览审阅、冻结发布、check v2 |
| 可观测与发布 | 证据链、新旧原因映射、lane 租用与容量上报、并行验收三项 |
| 失败韧性 | 可复位用例的 case 级自动重试（新 run，标 flaky）；不在 run 内续写 |
| 并行研究 | 现有三端视觉素材的离线标定与报告 |

生成器、需求理解、Normalizer、50 条生成输出、few-shot 和 80% 生成门槛均已退出 V5；手写计划格式与展开器同样撤销。V5 不新增用例生成能力，也不改变现有文字用例的编写方式；模型只在现有编译期参与，运行期不编译。

## 2. 本轮核对的事实与修订

下表是源码阅读结论，尚非运行复现。路径相对各仓根目录；影响规模仍需实际会话与脱敏日志归因。

| 源码位置 | 已核对事实 | 设计影响 |
| --- | --- | --- |
| Nexus `models/ui_control.py` | ORM 为 UiControl，实际表名 ui_controls；已有 project_id/control_id/element_type/scope/locator_value/pending_merge | 取消新建 project_ui_targets，继续以 control_id 为控件身份 |
| `services/control_registry.py` | 定位按 playwright/uiautomator/vlm_text 分格，来源也按格保存；编译盖 control_id | 复用身份和渠道结构；typed locator 与发布修订需新增，不能写成现有即具备 |
| `locator_stop_reason`、`note_execution_locator` | 空格时可 fill_locator，以 dom_hit/case 回写 | 错误定位可能固化；V5 运行全程只读选定配置，守卫放在服务写入口 |
| `nav_route.py::_attach_edge_control` | 也调用 ensure_control/fill_locator，来源为 capture | 审计其他写入口；保护已发布 V5 控件不能只判断 source=dom_hit |
| `stamp_compiled_plans` | 用“弹窗”字符串猜 scope/element_type | 去掉猜作用域规则；它是通用中文启发式，不直接等同某 App 文案硬编码，但不足以成为安全作用域依据 |
| Scout `playwright_executor.py::press_key` | BACK 且 browser_back 默认 true 时调用 page.go_back() | IME 收起与浏览器后退必须分开；不能用 browser_back=false 想当然保证平台正确 |
| Nexus `local_executors.py` | 勾同意框和通用 dismiss_ime 未排除 Web；发送验证码的 BACK 分支已有 channel != WEB | 分支分别整改；E00 覆盖真实同意框路径，撤销“换非导航键避开”的验收设计 |
| `_dispatch_device` / Scout `core.py` | 同一 event.seq 的不同设备调用可能共缓存键；完成缓存不比较请求内容 | 独立 wire 分配先止静默跳过，再与协议批次启用冲突检测 |
| Scout `playwright_locators.py::locator_from_params` | css/testid/role/text 候选链返回 loc.first | 严格 V5 路径需共享新解析器；不直接全局替换旧视觉路径 |
| Scout `_input/_commit_input_value` | 有坐标可编辑定位；fill 失败后 click/选全/删字/type，fill/click 含固定 2000ms | 新 input_target 完全绕开该链；驱动等待由同一 deadline 下传 |
| Nexus `_stored_plan_fresh` / `sync_case_step_program_keys` | freshness 要求 compiler=case-step-compile 且与文本一致；同步函数直接文本编译 | 冻结发布后的守卫放在同步函数内：已发布版本不被文本重编覆盖，重编译只产生 draft |
| `rProjectCases.py`、`case_store.py`、`case_key_registry.py` | 重编译、保存、校验、预览、摘要均涉及该同步收口 | 全部遵循“已发布优先、修改成 draft + 差异预览”，禁止展示与执行用两份不同的计划 |
| `build_check_program_plan_for_step` | 当前 mino.check_program_plan.v1 是 checkpoints/spans/key_refs 等内部结构 | 编译器产出 v2 字段并由固定映射表把 key_ref 映射到原子谓词；映射不了的由资格检查拦下 |
| `node_registry.py::resolve`、`device_targets.py::select`、RouterProxy | 可指定 sn，web{node_id} 归属固定节点，派发解析该 sn | 节点固定已有路由基础；新增的是 v5 准入与 lane 租用 |
| `device_admission.py` | 已有 SQLite 事务资源准入；headless Web 分四槽，headed Web 共享桌面独占；Scout 另有自己的常量 4 | 可作为准入基础，但两处容量各自独立，须改为以 Scout 上报为准；V5 并行要求 headless |
| Scout `core.py::_playwright_executor`、`_pw_shards` | 按 hash(run_id)%4 选单线程 worker；4 个互异 run 全部分散的概率约 9.4% | 多数情况下两个 run 共线程、串行排队，排队时间计入超时；v5 改 lane 租用 |
| Scout `playwright_hub.py::retire_owner`、`core.py::_retire_playwright_shard` | 卡死时作废该线程上所有 run 的页面，并替换整个 shard | 一 run 一 lane 后只波及自身；能否结束 lane 浏览器进程待实测 |
| Nexus `case_step_key_compiler.py`（`compile_step_spans`） | 操作/预期/前置编译均调用模型；运行循环未调用 `sync_case_step_program_keys` | 保留文字导入可行：编译期不确定，冻结后运行期确定；防覆盖点在编辑/重编译接口 |
| `control_registry.py::stamp_compiled_plans` | 编译期为未登记目标 ensure_control 创建控件（仅描述） | 导入后自然产生 draft 控件待办，V5 要求补 typed locator 并发布；运行期不得创建 |

前轮已核对的事实继续有效：password 进入 DOM text、hydrate_bound_text 和摘要有秘密外带风险；`_AUTH_PROBE_STEP=-7` 不缓存；报告 run 按案/设备隔离；SessionWriter 有共享 RLock，直接 store 调用仍需原子事务；Web 截图来源缺字段，Desktop 已有同名字段；全案 deadline 当前设置偏晚。

幂等命中包括合法同请求重取，不能直接当作假通过次数。文档只确认存在可触发的静默跳过路径，不声称已统计历史事故。

## 3. 对反馈的独立判断

| 建议 | 判断与采用方式 |
| --- | --- |
| 演进 ui_control | 完全采用。既有表 ui_controls 是唯一控件配置真源；不再添加同义 target 表或 target_ref 身份 |
| 只允许 Workbench 写入 | V5 发布配置采用此规则；在 fill/ensure 服务边界防护，避免旧模式或导航入口改到共享已发布控件 |
| 关闭所有旧模式学习 | 需迁移区分。未迁移旧控件行为可保留并显式标 legacy；已有 typed 发布控件始终受保护；不把关学习与旧定位解析全库切换绑成一个大改动 |
| Web 用 blur | 可选且需明确语义。首选跳过 IME 收起；确实需要 blur 时作为独立页面写动作，有独立键、预算和结果，不把它藏成只读操作 |
| 独立 tap_target/input_target | 采用。是主要 Scout 新工作项；目录注册、严格解析、敏感输入、超时透传及执行边界分别验收 |
| fill 失败禁止补写 | V5 完全采用；已调用写 API 后的异常保守 yes/unknown，不能 fallback 增补多个写 |
| 守卫只放 sync 足够 | 是必要收口，但不充分。已显式传 compiled_case 和直接编译的入口也须遵循来源分派；发布、预览、资源同步不能绕过 |
| 手写计划格式 + 展开器 | 撤销。保留文字用例导入，编译一次、审阅、冻结发布；手写格式把用例拆成“先配控件、再拼 JSON”两步，门槛高且割裂 |
| unknown 终结整个 run 使 CI 瘫痪 | 担心成立，原因判断需修正：后端慢不产生 unknown，真实 unknown 来自 RESULT 丢失或写 API 卡住。解法不是 run 内续写，而是可复位用例的 case 级自动重试（D8） |
| 密码整改阻塞 P0 | 一半误读：demo 账号 P0 不依赖它，只有真实凭据依赖；但它是现网漏洞，不能删，改为独立热修复 HF-1（D1） |
| 来源字段与控件字段过多 | 采纳精简：observation 只留 session_generation + after_step_idx，请求关联靠 reply_to；控件 P0 只加四个字段。“校验移到 Nexus”不可行：数据采集于动作前还是后只有 Scout 知道 |
| 多 lane 隔离矩阵门槛过高 | 采纳。根因是 hash 分配使多 run 共线程；改为一 run 一 lane 租用，不再证明“同线程隔离”，验收缩为三项（方案三 §6） |
| 专用节点/串行实例 | 撤销。设计前提是一台机器一个 Scout 承接最多 4 个 run；改为 lane 租用，不需要额外部署实例。节点固定已可经 sn 实现 |
| 前三个 PR | 大体采用。先止漏/编排止血；协议清单冻结后两仓升级；严格动作与事实为主要第三批。文字用例冻结与资格检查可并行，可信闭环必须等待它完成 |

## 4. 八项决策

### D1：止漏是独立热修复，真实账号以它为门槛

**HF-1 是独立热修复，不是 V5 前置。** password 的 value/text 清空，秘密输入不进入 bound_text、summary、plan_event 日志副本或模型载荷；第一阶段不增加新远程字段（nonempty/length 放协议批次）。它修的是现网（V4 起）真实凭据的外泄风险，应尽快单独上线，并用 demo 哨兵验证各序列化出口。V5 的 demo 验收使用 demo 账号，不依赖它；只有接入真实凭据才以哨兵验收通过为门槛。安全分类覆盖非 password 的敏感控件，不能只遮 hierarchy。

### D2：Web IME 与同键问题独立修复

同意框路径在 Web 跳过 BACK；显式浏览器后退能力保持原语义。多次设备派发各获独立 wire 键，包含移动编排与 logout。PR-A 可修新请求编号，in-flight/冲突检测与协议准入随后成批生效；实际完成缓存不能因编号变了就被视为安全。

### D3：控件身份和发布配置复用注册表

plan、do、check 都引用 control_id；plan 中不再另设 target_ref。UiControl P0 只演进 revision、locator_spec、sensitive、publish_state 四项；binding_context、fact_policy 后移（单 App、单平台用不上）。Workbench 保存显式版本，session 固化内容；运行配置只读，临时命中最多记诊断，不能成为站点知识。[具体合同见方案二 §4]

### D4：严格 Web 动作是一条独立实施路径

V5 使用 tap_target/input_target/read_web_target_facts，同一 typed resolver 与 scope。旧 tap_element/input_text 继续承接视觉任务；模式变化只能由开跑选择，失败时不跨路径兜底。选中目标唯一才写；不取 first、不按坐标猜、不换输入框、不走旧输入 fallback。

### D5：文字用例导入，编译一次、审阅、冻结发布

保留现有文字三列用例和导入解析，取消手写计划格式与展开器。编译期可调用模型，因此同一文字两次编译可能不同；V5 要求执行期确定：编译预览 → V5 资格检查 → 缺定位的控件补 typed locator 并发布 → 人工确认预期强度 → 冻结发布，运行只读冻结版本。重编译、保存、同步只产生新 draft 和差异预览，不覆盖已发布版本，守卫位于 sync_case_step_program_keys 内部。模型参与编译不影响可信性，前提是冻结后不再变，且编译期 dispatch_log 与版本被记录。详见方案二 §6。

### D6：派单复用，lane 租用新增

设计前提是一台机器一个 Scout，同时承接最多 4 个 Web run，不再要求专用实例或多实例。execution_mode=v5 的任务经 device_targets.select / Registry.resolve(sn) 固定到配置的 web{node_id}，复用现有设备选择与 RouterProxy。Scout 的 worker 分配由 hash(run_id)%4 改为 lane 租用：一个 run 独占一条 lane（线程+浏览器+context），卡死只隔离该 lane，容量由心跳上报，Nexus 按上报值在 device_admission 事务内准入。合同见方案二 §1.1。

同机若同时注册打包版与开发脚本两个 Scout（用户判断，待第 0 步核实），v5 只派给显式配置的节点。lane 租用落地前，v5 每节点仅允许 1 个活跃 run。

### D7：离线视觉与执行主链分开

P0 存动作后新截图和来源，不接在线 visual_review 恢复/里程碑判断；三端 VLM 标定并行离线运行。真实动作安全、DOM 准出和视觉误判率各有验收，不用其中一个指标替代另外两个。

### D8：unknown 不续写，但可整案重试

写动作结果未知时，run 内仍禁止续写（无法证明上次是否生效，重复提交会令最终结果出错）。为避免偶发 unknown 让 CI 不可用，可复位的用例可声明 retry_policy，由 effect_unknown 触发开新 run_id 整案重跑（默认最多 1 次，上限 2），通过则标 flaky，不算干净通过。后端响应慢通常不产生 unknown，而表现为 check 窗口耗尽；真实 unknown 规模待第 0 步用现有 session 统计。合同见方案二 §2.3.1。

## 5. 框架借鉴与限度

| 一手资料 | 可借鉴 | 本项目额外责任 |
| --- | --- | --- |
| Playwright Locator/Assertions [R1–R3] | 严格唯一 locator、就绪等待和断言有界重读；官方也指出 first/nth 容易误选 | 默认严格性被本仓 first 候选链削弱；恢复 strictness 不等于业务真值或 exactly-once |
| Stagehand [R4] | act/observe/extract 分工及结构化提取 | LLM 提取不是本方案确定性 DOM 裁判；不引入新的推理依赖 |
| OpenClaw [R5] | 专用浏览器 profile 与隔离操作 | 专用 profile 不自动等于每案新 context，更不证明后台写已停止 |
| Meta Muse 官方产品说明 [R6] | 专用 VM/浏览器隔离的产品思路 | 闭源说明不能证明测试断言准确率或内部协议机制；此处不推测其实现 |
| Python Future [R7] | running task、cancel 和等待超时的区别 | 超时不等于动作结束，实例释放须有真实停止依据 |

移动/PC 后续分别使用其驱动事实和视觉校准；Web 控件严格定位的结论不能直接移植为三端统一准出。

## 6. 参考资料与事实限制

一手资料于 2026-10-13 查询，用于设计比较，不代替本仓运行验收。

- [R1：Playwright Locators / Strictness](https://playwright.dev/python/docs/locators#strictness)
- [R2：Playwright Assertions](https://playwright.dev/python/docs/test-assertions)
- [R3：Playwright Locator API](https://playwright.dev/python/docs/api/class-locator)
- [R4：Stagehand Extract](https://docs.stagehand.dev/v3/basics/extract)
- [R5：OpenClaw Isolation](https://docs.openclaw.ai/tools/browser/isolation)
- [R6：Meta Muse 官方说明](https://about.fb.com/news/2026/09/introducing-muse-personal-ai-agent/)
- [R7：Python concurrent.futures](https://docs.python.org/3/library/concurrent.futures.html)

历史复键数、真实秘密残留、append 错误率、驱动耗时分布及视觉误判率均未实测；后续交付须明确样本与版本，不把本轮源码阅读写成已验收。
