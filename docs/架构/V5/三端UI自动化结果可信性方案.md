# V5 方案二：执行安全、控件事实与固定准出合同

> R8，2026-10-13。本篇为接口、状态、预算、版本和原因映射的唯一合同真源。
> [方案一：决策与事实](看图优先与DOM后置混合执行方案.md)；[方案三：交付与验收](执行侧交付与验收清单.md)。以下标为新增的字段、能力、服务及迁移均尚未实现。
> V5 取消生成；EventStatus 保持五态。工具回执 pass 和业务 verified_pass 是两个层次。

## 1. 归属、派单与 lane 租用

| 模块 | 责任与复用边界 |
| --- | --- |
| UiControl / control_registry | 唯一控件身份、发布配置、版本和渠道；演进 ui_controls，不新建同义目标表 |
| 用例导入与发布服务 | 复用现有文字用例导入/编译（模型只在编译期参与）；新增 V5 资格检查、编译预览差异、冻结发布和来源分派；运行期不编译 |
| RouterProxy | 每次设备写的 wire 编号、有限网络等待、run 写门闩、事实查询和取消；循环只调用 dispatch |
| ScoutCore / executors | 原子在途登记、写边界事实、真实任务完成；lane 租用/隔离/容量上报；严格目标解析与原始事实采集 |
| structured_check | 复用原子评估模块，新增 v2 定向事实输入；唯一业务谓词评估者 |
| 状态机 | 节点推进、固定 required 消费、来源充分性、run 门闩、成功事务；不含业务文案 |
| session_store / Workbench | 原子证据终结、新旧状态映射及可复核链路 |

新增 execution_mode=legacy/v5，默认 legacy，开跑选择后不可变。v5 任务仅在节点上报 contracts.web_v5、使用 headless Web、控件已发布、用例已冻结发布时准入。

### 1.1 一个 Scout 承接多个 run：按 lane 租用

设计前提：一台机器装一个 Scout，同时承接最多 4 个 Web run（沿用 PLAYWRIGHT_PARALLEL_LANES=4 的并发量）。不引入多 Scout 实例或专用实例。

代码事实（源码阅读，非运行复现）：Scout 用 hash(run_id)%4 选 worker 线程，Nexus 与 Scout 各有一份常量 4。4 个互异 run 恰好落在 4 个不同线程的概率约 9.4%（4!/4⁴），多数情况下两个 run 共线程、串行排队，后者的排队时间计入其 action 与整案期限，易产生超时。某线程卡死时，_retire_playwright_shard/retire_owner 会作废该线程上所有 run 的页面。

| 项 | 合同 |
| --- | --- |
| 租用 | v5 run 在 preflight 阶段向 Scout 申请 lane，整个 run 期间独占；run 终态且真实停止后归还。无空闲 lane 时 Scout 拒绝，Nexus 排队（受 setup deadline 约束），不超额接单 |
| 独占 | 一个 lane = 一条 worker 线程 + 一个浏览器 + 一个 run 的 context；v5 不再按 hash 选线程 |
| 卡死 | 只隔离该 lane：run 以 effect_unknown 终结，lane 标 quarantined，可用容量下降，其余 lane 不受影响。Scout 终止该 lane 的浏览器进程以解除阻塞并恢复 lane，属本机资源管理。能否取得并结束 lane 浏览器进程待 Playwright sync API 实测；取不到时降级为 Scout 进程重启，并按“节点隔离”如实记录（会影响其余 lane） |
| 容量上报 | 既有心跳 DeviceWorkload 增加 lanes_total/lanes_free/lanes_quarantined；Nexus 以上报值为准 |
| 准入 | 在既有 device_admission 事务内，按节点上报的 free lanes 与 Nexus 侧预留共同计数；headed Web 共享桌面，只允许 1 个活跃 run，V5 并行需要 headless |
| 节点固定 | v5 任务经 device_targets.select / Registry.resolve(sn) 固定 web{node_id}。同机可能同时注册打包版与开发脚本两个 Scout（待第 0 步核实各自 sn 与 node_id），v5 任务只派给显式配置的节点，开发节点默认不入选 |

过渡：lane 租用落地前，v5 每节点同一时刻只允许 1 个活跃 run，该阶段结果只对此配置成立，不外推并行。并行的验收条件见方案三 §6。

## 2. 请求编号、在途与写边界

### 2.1 请求身份

所有新设备写由 RouterProxy 的共享 run 分配器原子生成单调正 wire step_idx。逻辑 case_step/event.seq 保留用于显示、预算和 check 引用，不再充当传输去重键。同一编排内 BACK/tap、logout/主动作、flow/recovery 各取新编号；多个 RouterProxy 实例共用 run 分配器。实际 action_ref={run_id,step_idx} 回填到结果和动作事件。

能力分类由 Nexus 目录选定实现与 Scout 支持合同确定，模型不能自报 readonly。只读观察使用负 wire step_idx，不命中完成缓存，Envelope.reply_to 关联本次请求。cancel/清理按独立控制白名单，不伪装为业务只读。

正常 scout_run_id 已按案/设备隔离；重跑另建实际尝试 run_id，不清空同一个 run 的编号后复用。编号修复对所有新设备任务生效，升级先排空旧在途。PR-A 可先修不同新请求的键；PR-B 启用冲突检测和合同准入时要求所有发送路径已完成迁移。

Scout 在锁内查 done/in-flight/请求内容并占位，再提交 Future，锁外等待：

| 收到的请求 | 处理 |
| --- | --- |
| 完成同键同请求 | 返回原结果，不再执行 |
| 在途同键同请求 | request_inflight，不重复排队、不触发 executor fallback |
| 同键不同请求 | request_key_conflict，附原请求是否在途；不能冒用前一结果 |
| 新键 | 原子登记后执行；入队和进入写 API 前均查 run 门闩 |

请求比较使用真实载荷，仅在进程内处理秘密；日志只存脱敏摘要，不写密码或密码 hash。done 只记录真实结束，Future.result 等待超时不伪造完成。Scout 重启丢内存去重不等于可续跑：旧 run 中断且不得在新进程重发写。

写请求关闭协议自动重发；只读重试至多一次、仍受整案 deadline。PR-B 不增加任务状态查询 RPC。

### 2.2 写边界必须由 Scout 报告

新增 Result.data.execution：execution_started=no/yes/unknown，phase、reason_code。RouterProxy 可以合成停止决策，不能替 Scout 猜 no。

```json
{
  "execution": {
    "execution_started": "no",
    "phase": "resolve_target",
    "reason_code": "target_not_found"
  }
}
```

| 值 | 定义 | 行为 |
| --- | --- | --- |
| no | 本请求未进入任何可能修改页面/设备的 API；有执行器证明 | 固定图内可重新观察/定位并恢复一次；必须是尚未执行的同一动作 |
| yes | 已调用 click/fill 等潜在写 API；不保证物理/业务成功 | 成功回执后独立 check；错误在 P0 保守终止，不继续补写 |
| unknown | 回执缺失、阶段无法证明或动作仍在运行 | effect_unknown，阻止该 run 任何后续业务写 |

参数/语法/匹配数量/只读就绪检查在 API 前失败可 no；排队取消成功且确认未开始可 no。click/fill 内部等待抛错不能仅按错误文案证明 no。每次进入潜在写 API 前设 yes，异常保留阶段；进入失败后不能同请求换 executor、换 locator 或进入输入 fallback。

复合编排传播所有子 action_ref。前一子写成功、下一子定位 no，不代表整个父事件从未执行；仅可恢复尚未写的子动作，不能重放整个编排。任何子未知立即关 run 门闩。

### 2.3 循环可见门闩

共享 run 控制状态 running → write_stopped → terminal，由 RouterProxy 与本地调度共用，不新增第六 EventStatus。EventResult.data.execution_control={stop_run,reason_code,action_ref}；agent、recovery、flow、本地守护和状态机均消费这一领域结果。循环不查询 Scout RPC，也不能把 fail 默认恢复覆盖 stop_run。

| 当前 | 事件 | 转移与结果 |
| --- | --- | --- |
| running | 明确 no 的失败 | 固定 expected 不变；一次安全恢复 |
| running | 动作成功 | 继续读取动作后事实，receipt 不推进 required |
| running | 未知写/在途冲突 | write_stopped；effect_unknown 和具体诊断 |
| running | 写前剩余窗口不足 | write_stopped；timeout_before_dispatch，实际写 0 |
| running | cancel/显式紧急停 | write_stopped；cancelled/stopped |
| write_stopped | 模型/recovery 继续写 | 拒绝 run_write_stopped，保留最初主原因 |
| write_stopped | 结束 | terminal；不挂人工复核 |
| terminal | 晚回执/模型 done | 仅审计，不翻绿、不重开 |
| running | required 完整、证据事务成功、预算内且无未知在途 | terminal；verified_pass |

未知结果不通过“看到登录态”挽救为通过；清理只关闭所属 context 等白名单，有独立健康期限。线程未停隔离实例，清理不能回滚后端副作用。

### 2.3.1 unknown 的范围与 case 级自动重试

unknown 只来自两类事件：写 API 已调用但 RESULT 丢失/任务未结束，或写 API 本身卡住。后端响应慢本身通常不产生 unknown，而是体现为后续 check 等待窗口耗尽（assertion_mismatch 或 unverified）；元素不可点/被遮挡属 execution_started=no，可一次恢复。需注意 Playwright 的 click 在触发导航时可能同步等待导航，其在 action_timeout 下的表现待实测，不能凭本文断言。第 0 步从现有 session 统计各写能力的超时/缺回执率，以判断 unknown 的实际规模。

run 内仍不允许“超时后接着写”：无法证明上次是否生效，重复提交会让最终结果出错。为避免偶发 unknown 让 CI 整体不可用，改为 case 级重试：

| 项 | 规则 |
| --- | --- |
| 适用 | 用例发布审阅时声明 retry_policy={on:[effect_unknown],max:1}（上限 2），并确认可复位：每次重试新 BrowserContext，数据/账号可复位或幂等。默认关闭；支付、发码、不可复位流程不得开启 |
| 做法 | 原 run 终态 unverified/effect_unknown，且其 lane 已隔离或确认停止后，开新 run_id（新 scout_run_id、新 wire 计数、新预算、新 lane）完整重跑；session_meta 记 retry_of、attempt |
| 证据 | 新 run 只用自己的证据；原 run 的 Evidence 保持 unverified，不并入新 verdict |
| 结果 | 重跑 verified_pass 则 case 为 pass 并标 flaky，保留 effect_unknown 历史，单列 flaky 率，不算干净通过；重跑仍失败则 fail |
| 预算 | 每次尝试各按整案公式计时；CI 总时长上限=(1+max)×单次预算，报表显示 |
| 禁止 | assertion_mismatch/verified_fail 不重试；不得重试到变绿 |

### 2.4 冻结一次协议批次：字段清单与 fixture 清单

仍使用现有消息和字典扩展；扩展是协议语义变更，按两仓 CLAUDE.md §5 同一 PR 周期完成。新增合同不能只存在方案文字里。

| 载荷位置 | 新增/规范化字段 | 接收侧要求 |
| --- | --- | --- |
| EXECUTE.params | control={control_id,revision,locator_spec,sensitive}；动作 payload 或 requested_facts；after_action | Nexus 从选定控件生成；Scout 不查目录/DB；V5 不接受候选坐标或另一套 target_ref |
| EXECUTE.device_hint | execution_mode | 已有 node/sn 定位不重造；Scout 据模式做 admission |
| EXECUTE.timeout_sec / params.timeout_profile | 当前请求期限；resolve_ms/action_ms 等受控子预算 | 相对时长下传，不跨机器传 monotonic 时间戳；逐次阻塞重新扣减 |
| RESULT.data.execution | execution_started/phase/reason_code | no 缺省为未知；旧结果不能进入 V5 安全恢复 |
| RESULT.data.observation | run_id / session_generation / after_step_idx | 来源由 Scout 实采并校验完成屏障；请求关联靠 Envelope.reply_to；缺字段拒绝证据 |
| RESULT.data.target_facts | control_id/control_revision/scope_resolved/collection_complete/count/各请求事实及完整性 | 事实字段有明确类型，不携业务 expected |
| 安全事实 | secret_value={nonempty,length}；配置可禁止 length | 不改变 text/value 字符串类型；不输出原值/hash |
| Web screenshot remote_detail | session_generation / after_step_idx + artifact 元数据 | Web 是新增实现，不能 Nexus 补造；Desktop 已有字段语义不变 |
| 既有 probe 响应 data.contracts | web_v5=1、locator_contract、predicate_facts_contract | 新能力支持声明；缺失拒绝试点，不悄然降旧路径 |
| 既有 DeviceWorkload（心跳） | lanes_total / lanes_free / lanes_quarantined | Scout 实报；Nexus 准入以上报值为准，不以两边常量 4 为准；缺字段按旧模式 |
| 本地 EventResult.data | execution_control/action_ref | Nexus 内部领域合同，列入实现清单；不需要另造 RPC |

fixture 文件名为拟新增的合同清单，不代表本轮已生成文件：

| fixture（两仓 tests/fixtures/protocol/） | 覆盖 |
| --- | --- |
| execute_v5_tap_target.json | typed control、模式、子超时、非敏感写 |
| execute_v5_input_target.json | 敏感分类、示例占位输入；不得含真实凭据 |
| execute_v5_target_facts.json | 只读请求、requested_facts、after_action |
| result_v5_execution_no.json | 写前拒绝、阶段事实 |
| result_v5_execution_unknown.json | 写 API 后异常/未终结的保守事实 |
| result_v5_inflight.json / result_v5_key_conflict.json | 在途拒绝、内容冲突及原任务是否在途 |
| result_v5_target_facts.json | 完整事实、来源、秘密安全元数据 |
| result_v5_target_facts_unavailable.json | scope 不可达/截断；不得冒充 count=0 |
| result_v5_web_screenshot.json | Web 来源字段与 artifact |
| result_v5_probe_contracts.json | 支持声明和合同版本 |
| heartbeat_v5_lanes.json | DeviceWorkload 的 lanes_total/lanes_free/lanes_quarantined；旧心跳缺省解码 |
| 既有 execute/result/desktop fixture | 旧字段缺省解码；旧模式兼容与 Desktop 语义不变 |

语义验收“实际写一次”由 demo 会话完成，round-trip fixture 不证明执行幂等。发布检查覆盖已有全套 fixture 的两端 round-trip、字节相同及 PROTOCOL §8 哈希；当前消息版本是否需递增由该批两仓协议审阅决定，支持声明是 V5 强准入条件，不能仅依赖旧端忽略字典字段。

## 3. 秘密输入与出口

本节分两层。**HF-1 止漏热修复**独立于 V5 发布节奏，可单独先上线，不改变网络字段形状；它保护的是现网真实凭据，不阻塞 demo 账号上的 V5 P0。**协议批次**才新增 secret_value。

第 0 阶段先保持既有输出形状止漏：采集 password 时 text/value 安全置空，禁止 hydrate_bound_text 将秘密复制进 rules.bound_text/expected_value；禁止输入摘要/错误/回传 plan_event/模型请求与日志副本外带原值。不要截密码前后缀，也不要把字典塞进原字符串字段。输出前脱敏，必要执行载荷仍由受控来源短暂供给 Scout，不能误把实际输入也改成掩码。

协议批次加入 secret_value；sensitive 由已发布控件与操作类型双重确定，password 强制为敏感，模型不能取消。非 password 秘密也按此处理。密码只核对非空/受控长度，凭据正确性依赖固定终态。普通 value_equals 禁止用于敏感控件；若不允许长度出口，仅声明 secret_nonempty 支持。

哨兵验收包含采集、bound_text、typed_by_step、summary/error、plan_event、raw_response、dispatch/session 和实际模型载荷，覆盖成功/失败/超时。排查报告只给匹配出口和计数，不回显哨兵或真实秘密。历史日志只读归因，未授权不清理；本轮不声明历史残留已清除。

## 4. UiControl 演进与严格 Web 路径

### 4.1 唯一配置真源

使用既有 UiControl / ui_controls / control_registry；所有 do/check 和事实都用 control_id。删除 project_ui_targets 设计与额外 target_ref 身份。保留既有渠道 locator_value/source 和 desc_key，旧消费者字段类型不变。

| 新增字段 | 规则 |
| --- | --- |
| revision | 同一 control_id 显式配置修改后递增；Console 使用 expected_revision 并发保存 |
| publish_state | legacy/draft/published；编译期自动创建的控件为 draft，只有 Console 人工核对 typed locator 后才 published；V5 只选 published，不将 dom_hit 自动升级为可信 |
| locator_spec（JSON） | 渠道分格，P0 只发布 playwright typed locator，含 scope 与 cardinality；不与旧字符串互相覆盖 |
| sensitive | 配置敏感分类；password 类型强制敏感 |
| updated_at / updated_by | 配置审计，复用现有操作者身份 |

binding_context、fact_policy 后移：P0 只有一个 App、一个 Web 平台，归属沿用 project_id 现列与既有 scope；基数在 locator_spec.cardinality；文本/值读取上限用 Scout 默认常量，可由 requested_facts 参数下调。多 App/多平台共享控件时再加 binding_context。

当前表 control_id 没有项目级唯一约束，迁移前做重复/归属审计；正常写入口保证 (project_id,control_id) 唯一，发现重复先报告，不自动合并用户记录。既有 desc_key 唯一约束保留；V5 引用显式 control_id，不以描述相似自动合并两个页面目标。

首期保存当前行与递增 revision；session 的选定配置快照用于历史复核，无需先建全量配置版本仓库。Console 显示现配置和已有会话快照，但不承诺尚未实现的任意历史版本回滚。修订兼容策略见 §6.4。

typed locator 示例，仅为 demo 配置，需经 Console 发布：

```json
{
  "playwright": {
    "contract": "web.locator.v1",
    "scope": {"kind": "page", "page_id": "main"},
    "selector": {"kind": "test_id", "value": "logged-in-marker"},
    "cardinality": "one"
  }
}
```

P0 支持主 page 的 test_id/role/label/css；role/label 明确 exact=true，单一指定策略，无候选链。iframe/dialog/shadow/new_window/webview 沿用既有 scope 枚举但不自动声明实现：开工选一个实际需要的 scope，未实现 preflight capability_unavailable；现代 SPA 主 page 无 URL 改变仍可测试。不靠“弹窗”等描述推导 scope，不允许任意 JS selector。

Nexus 固化全部所需控件 revision/内容后下发；同一 run 的动作和事实解析同一副本。Console 修改只影响后续选定配置；紧急停另走实时控制。

### 4.2 发布入口和运行只读

Console 使用“项目详情 → 执行配置 → 控件注册表”，增加渠道 typed locator、明确 scope、敏感标记的轻量编辑，并列出导入产生的 draft 控件待办。列举、expected_revision 保存/发布、只读探测由 control_registry 服务承接，按项目/App 权限；不建第二套目标 CRUD 或录制器。新 UI/API 是开发项，不宣称当前已有编辑入口。

所有控制写入口执行同一守卫：V5 runtime 不得 ensure/fill/发布；published typed 控件只允许显式授权的配置服务修改，旧模式执行/导航也不能回写它。locator_stop_reason、note_execution_locator 和 nav_route 的 capture 写入不得绕过。匹配命中可存 session 诊断，不生成已启用配置；日志写成功也不授权回写。

旧 legacy 控件可保留既有字符串路径，需在 UI 明示来源风险；迁移到 V5 由人工核对 scope/selector 后发布 typed spec，不自动把历史 dom_hit 当正确 locator。stamp_compiled_plans 旧描述启发式拆除，V5 不调用隐式 ensure_control；缺作用域/归属不默默猜。

### 4.3 独立动作能力与共享解析器

新增 tap_target/input_target/read_web_target_facts，目录通过 Console 注册，Scout 只实现并声明合同。这是主要 Scout 工作项，包含严格 resolver、动作 API、阶段/超时和事实，不描述为“小改现有输入”。

| 能力 | 操作合同 |
| --- | --- |
| tap_target | 显式 scope/selector → 只读 count=1/就绪检查 → 一次 locator.click → execution 与 action_ref |
| input_target | 同一 resolver → 唯一可编辑目标/敏感分类 → 一次 locator.fill → 安全回执；无坐标命中或键盘补写 |
| read_web_target_facts | 同一 resolver → 只读请求事实 → 来源/完整性；不执行业务 predicate |

不取 first/last/nth，不重新猜 role/text 或改坐标。写前 count>1 为 target_ambiguous，count=0 在定位窗口耗尽为 target_not_found；no 允许重新读取同配置一次，不能悄悄换目标。并发 DOM 变化仍由驱动 strictness 保护，调用 click/fill 后错误不能按预检查 count=1 猜已成功。

input_target 不调用 _commit_input_value、locate_editable_at 或旧 fill→click→快捷键→type 链。action timeout 直接传 click/fill 等驱动 API；只读就绪/读数也有剩余期限。普通 visual 模式保留 tap_element/input_text；V5 失败不回退到它们。旧模式的 fallback 风险单独可见，不标为 V5 已验收。

Web 的 IME 收起默认跳过；若确需 blur，显式列为潜在页面写，不复用 BACK，不藏在 tap_target 内。真实浏览器 back 保持独立的导航语义。

## 5. 定向事实与来源

内部拟议接口 RouterProxy.query_control_facts(control_id,requested_facts,after_action,deadline)。由 Nexus 从 runtime_context 取配置并走既有 EXECUTE，循环不感知网络。每次读新事实，负 wire 编号不缓存。

```json
{
  "observation": {
    "run_id": "rid::cid::web-slot",
    "session_generation": "web-context-1",
    "after_step_idx": 3
  },
  "target_facts": {
    "control_id": "ctl_demo_marker",
    "control_revision": 1,
    "scope_resolved": true,
    "collection_complete": true,
    "count": 1,
    "visible": true,
    "text": "示例已登录标记",
    "text_complete": true
  }
}
```

示例 ID/读数仅用于合同说明。Scout 不接业务预期文本/数量来判通过，原始事实由 Nexus 评估。secret length 的业务比较也在 Nexus；端内只采安全元数据，不再同时声明两处裁判。

Envelope.reply_to 绑定请求；session_generation 是真实 BrowserContext 代次；after_step_idx 由 Scout 在核对该 run 已完成写且无未知写在途之后，填入最近已完成写的 wire 编号，不照抄请求。Nexus 核对 run、generation、配置 revision、after_step_idx 与请求 after_action 一致以及响应关联。基线观察可 after_action=null，但必须标用途 baseline，不能作为动作后 check。P0 不新增 capture_id/snapshot_id/capture_scope；Desktop 已有字段语义不变。

scope 不可达、语法错误、截断和采集超时为 unavailable；完整 scope 内 count=0 是合法不存在事实。set 仅用于数量，单目标读值/动作要求 one。visible 不等于视口内/未遮挡/业务可点击；未实现这些语义不声明支持。text/value 截断必须标 complete=false，不能用于精确比较。

一次事实读取不保证页面事务；document 改变或读数不一致则不可用/有界重读。整树继续给 planner 导航，v2 不读取 ctx.nav_hierarchy_nodes。截图和 DOM 分次采集、各自对应独立请求（reply_to）；不伪装同一瞬间。

## 6. 文字用例导入、冻结发布与 check v2

### 6.1 输入方式与流程

V5 保留文字（三列）用例作为唯一用例来源，沿用现有导入/编译，不建设手写计划格式或展开器。编译期允许调用模型（`compile_step_spans`），同一段文字两次编译结果可能不同；V5 要求的是**执行期确定**：编译一次、人工审阅、冻结发布，运行只读冻结版本。运行循环本身不调用 `sync_case_step_program_keys`（源码核对），需要防的是编辑、重编译接口和目录版本变化覆盖已发布版本。

```text
文字用例（三列）
  → 现有导入解析/编译（可含模型；记录 dispatch_log 引用、prompt/目录版本）
  → 编译预览：每条操作/预期 → 能力、控件、谓词、等待策略
  → V5 资格检查（§6.3）
  → 缺定位的控件列为待办 → Console 补 typed locator → 人工核对后发布控件
  → 人工确认预览与预期强度 → 冻结发布
  → 运行只读该版本
```

控件在编译期创建：`stamp_compiled_plans`/`ensure_control` 为未登记目标创建 draft 控件（有描述，无 typed locator）。这就是“先导入、再补控件”的自然入口，用户不必事先在 Console 注册。运行期仍不得 ensure/fill。

### 6.2 预期到原子谓词的确定性映射

编译预期时，对能确定映射的 key_ref，由代码内一张固定映射表产出 check v2 字段；映射不了的保持原样，交资格检查拦下。映射表为拟议的小表，开工时对照现有 `expected.*` 词表落地，例如 `expected.ui_text` → text_equals/visible，`expected.input_value` → value_equals 或 secret_nonempty，计数类 → count_equals；表中没有的键，V5 不声明可执行。

`verifier_kind` P0 只有 dom、`predicate_contract` 只有 web.atomic.v1，由编译器作为非语义默认值填写，用例作者不写；`wait_policy` 取谓词默认（初读加最多两次额外读，总窗口 3s），可在预览中调整。业务字段（目标、期望文案/数量、required）一律来自用例文字，不默认。

check plan 使用 mino.check_program_plan.v2，仍由 `case-step-compile` 产出，新增以下明确语义：

| v2 字段 | 定义 |
| --- | --- |
| checkpoints / point_id | 固定检查身份 |
| verifier_kind / predicate_contract | P0 dom / web.atomic.v1，运行不换 VLM |
| control_id / expected | 单原子或固定 all/any 树；不放 locator/坐标 |
| after_operation_id | 绑定静态操作，运行映射实际 wire action_ref |
| required | 必须实际消费；空 check 不算通过 |
| wait_policy | 整体 timeout 与 max_reads，含所有查询等待 |
| schema / plan_revision | 新格式与冻结版本，不能假称沿用 v1 |

既有 spans/key_refs 等兼容索引仍由编译产出，不承担预期语义。structured_check、StepPointer/check 消费、case_key_registry 预览/支持度和 Workbench 按版本分派；v1 保留旧模式，未知版本拦截。V5 不以 v1 缺字段猜默认通过，也不将 v2 静默交给旧消费者。

### 6.3 V5 资格检查

| 检查 | 不通过的情况 |
| --- | --- |
| 预期映射 | 落入 fallback_lines / unmapped / blocked，或只能靠看图判断 |
| 控件定位 | 引用的控件没有 published typed locator（只有 vlm_text，或 dom_hit 回填未经人工发布） |
| required | 无任何 required 预期 |
| 资源/数据 | resource_key 缺失或版本不足；账号/数据引用不可供给 |
| 未解析 fallback | 编译告警含 blocked/未解析项 |
| 敏感 | 涉及密码等敏感输入而控件未标 sensitive |

不通过的用例仍可按旧模式执行，但不标为 V5 已核验，并列出原因（v5_ineligible）。资格检查只验证结构与合同一致，不证明预期强度足够；强度由发布审阅人负责。

### 6.4 冻结发布与同步收口

meta 保存 plan_source=text、plan_state=draft/published/needs_revalidate、frozen_revision、编译预览摘要（不含秘密）、发布人/时间、资格检查结果、编译期 dispatch_log 引用与 prompt/目录版本。

| 动作 | 行为 |
| --- | --- |
| 发布 | 把当前编译产物标 published，记录 frozen_revision；preflight 读取 published 版本并校验摘要，不一致 plan_invalid |
| 重编译 / save_case / 文字修改 | 只产生新 draft，并给出与已发布版的差异预览；已发布版本继续生效，直到人工再次发布 |
| sync_case_step_program_keys | 守卫位于函数内部；对已发布用例返回发布产物，不按文本重编覆盖；显式传 compiled_case 的路径同样遵守 |
| 资源元数据同步 | 发布时校验并冻结；其后变化使 plan_state=needs_revalidate，不回写已发布产物 |
| 控件 / 目录变更 | 见 §6.6，标受影响用例 needs_revalidate，不自动重编 |
| 未冻结的旧用例 | 行为不变，走旧模式 |

文字修改后界面显示“有未发布修改”，不静默改变执行。此首期需要发布状态、差异预览和资格检查结果的展示，不建设通用结构化编辑器。

### 6.5 原子谓词与准出

Nexus structured_check 消费目标事实；P0 支持 visible/not_visible/text_equals/value_equals/count_equals/secret_nonempty/secret_length_matches。URL 只在明确需求和事实支持时启用；style/spatial 等旧能力未经来源适配不能算 V5 覆盖。

原子结论 true/false/unavailable。固定 all：任一 false 为 false，否则有 unavailable 为 unavailable；固定 any：任一 true 为 true，否则有 unavailable 为 unavailable。只有发布时认可的 any 分支可以提供充分证据，运行不能临时降级分支。Evidence 存实际消费分支与所有已求值子证据，无法读取 required 输入不变成 true。

wait_policy 初次读加最多两次额外读；整体等待受同案 deadline 与该 check 固定窗口约束，不给每次读重置窗口。有效未满足在窗口耗尽为 assertion_mismatch；缺失耗尽为 unverified。登录可用需求认可的单一强可见标记；SPA 无须路由变化。身份需求需实际身份断言；HTTP 200 或 cookie 线索不能替代需求的业务结论。

### 6.6 版本与改动策略

predicate_contract 固定语义；verifier_build 随 session 保存实现版本。兼容修复先过受控反例会话，不强迫全库重编译；breaking 语义变更仅影响相关计划，重新校验发布，运行不自动换谓词。

control locator 的兼容修正仍经 Console 发布新 revision，下个 run preflight 重新解析并校验，同案继续用已选快照。控件 identity/scope/敏感属性/事实支持变化标受影响计划 needs_revalidate；数据语义变化不当作 locator 修复。历史结果用当时 session 快照解释，不拿当前控件覆盖。

## 7. 唯一预算与写准入

保留 compute_case_wall_budget_sec：units=max(1,prep_units+max_step_number+check_units)，当前 check_units=max_step_number；budget=units×30 秒。正 wall_budget_sec 覆盖下限 30 秒。例如 1 前置+3 操作与同号校验=210 秒。低层子动作、恢复和观察不额外续 30 秒。

run_case 的 t0 后初始化/context/preflight/未登录基线/prep/do/check 都计入；deadline 前移到首次阻塞前，进程内用 monotonic。案外队列/资源分配有单独 setup deadline；结果收工与有限清理分开。

profile 明确 resolve/action/query/screenshot/transport/commit 上限。每次阻塞用 min(本项上限,剩余阶段期限,整案剩余)；Scout 请求只接相对 timeout，内部统一扣减，不为每个 API 重开完整子预算。LLM/provider、RouterProxy、观察、驱动、事实重读和 DB 都要适配，不能只改 agent_loop。

min_write_window=该操作所有驱动阶段上限+计划要求的后置 check 窗口+必要截图+传输+终结预留。写前剩余不足不派发，timeout_before_dispatch；不是把最后一次写硬裁成一秒后制造未知态。存在多条 required check 时预留其实际组合预算，不固定只留一次查询。

串行 demo 初值（尚无实测）：resolve 1s、tap/input 写 API 5s、单 check 总窗口3s、终态截图3s、transport1s、commit2s；单终态 check 的写窗口15s。导航动作 API10s对应20s。若需更长 check/多个 check，窗口按计划调整；填写5s但底层仍默认30s不算实现。P95 和超时样本只辅助调参，不是硬耗时界；若驱动不能可靠停止，真实超时后仍隔离实例。

## 8. 开跑快照、基线与原子落库

SessionMeta.runtime_context 保存实际用例来源与冻结发布版本（frozen_revision、编译版本、编译期 dispatch_log 引用、prompt/目录版本）、required、目录选定实现、每个 control 内容/revision、谓词合同/build、执行模式、选定 sn/node/lane、retry_of/attempt、timeout profile、预算与截止。秘密只留引用和分类；脱敏快照不含真实值。静态 ui_coverable、preflight runnable_now 和运行结果分开，runnable_now={checked_at,ready,reason_code,details} 写入 runtime_context，并列出实际执行的检查。

preflight 每案新非持久 BrowserContext，不复用 cookie/storage_state；记录实际 generation，读未登录基线失败不能降级 guest 后继续。demo 以可观测标记证明基线；业务站额外核对账号、数据和可复位。运行配置变化不续命，cancel/显式紧急停实时关门闩；没有信号源的账号授权撤销不写成已接入机制。

append=0 分 closed/missing_session/empty_type/store_error；只有活跃关键记录故障导致证据失败，先按入口统计 eligible_append_attempts/store_errors，计数分母不能只写到已故障日志。

新增 append_batch(session_id,records,meta_patch,deadline)→EventRef 列表。底层原子 seq 分配、插入、计数和 meta 更新同写事务；保留 SessionWriter RLock，迁移直接 store 入口。action/观察分别短事务提交；关键 Evidence 与 verdict/meta 同次终结事务，不持整案长事务。

提交未知先查相同 finalization 标记，仅重试本地提交，不重做 UI。不能确认证据终结就 evidence_persist_failed/unverified；CI 响应仍非成功，不因最后一条失败日志落不进去而放绿。

## 9. 原因码、五态与展示映射

统一 reason_code/verification_status/legacy_error_kind，保留旧值；未知旧原因标 needs_mapping，不从 summary 猜 assertion_mismatch。主原因保存首次触发，后续拒绝/清理为诊断。

| 现有来源或新事件 | reason_code | 验证状态 / case与UI |
| --- | --- | --- |
| observation_missing；事实/截图读取失败 | observation_unreadable | unverified / fail，未核验 |
| dom_check_unavailable：字段/范围不可读 | evidence_invalid 或 observation_unreadable | unverified / fail |
| dom_check_unavailable：谓词未实现 | capability_unavailable | preflight not_started；运行期 unverified / fail |
| 完整事实与固定预期不符 | assertion_mismatch | verified_fail / fail，已核验失败 |
| 明确写前0匹配/多匹配/非法定位 | target_not_found / target_ambiguous / locator_invalid | no 可一次恢复；耗尽 unverified / fail |
| 写超时/缺回执/阶段不明 | effect_unknown | unverified / fail，效果未知 |
| 同键在途/不同内容 | request_inflight / request_key_conflict | 不再派发；原写未停附 effect_unknown / fail |
| 写窗口不足/全案到期 | timeout_before_dispatch / timeout | unverified / fail；附 deadline_scope |
| 来源/计划版本非法或缺失 | plan_invalid / plan_incompatible | not_started或unverified / fail，不文本回编 |
| 控件发布修订冲突 | configuration_conflict | 保存拒绝；运行不替换已选副本 |
| 缺能力/控件/资源/配置 | capability_unavailable / configuration_missing | not_started / fail；executed=false |
| 文本用例未通过 V5 资格检查 / 控件仍为 draft | v5_ineligible | not_started / fail；executed=false，可按旧模式跑但不标 V5 已核验 |
| 节点/lane 准入拒绝或隔离 | execution_mode_rejected / node_mismatch / lane_quarantined | not_started或unverified / fail；无空闲 lane 属排队，不是失败 |
| 关键存储故障 | evidence_persist_failed | unverified / fail |
| required完整且预算/事务/门闩合法 | verified | verified_pass / pass |
| case 级重试后通过 | verified，attempt>1 标 flaky | verified_pass(flaky) / pass，单列统计，不算干净通过 |
| cancel/紧急停/后续写被门闩拒绝 | cancelled / stopped / run_write_stopped | 终止/诊断；保持最初主原因 |
| 历史 unverifiable/untestable/unexecutable | 保留旧 reason，有明细才映射 | 保留历史状态，兼容显示未核验/不可执行 |

EventStatus：pass=工具回执完成，fail=工具故障，declined=不适配，skipped=未做，blocked=既有阻塞语义。V5 未知/不可读用 fail+领域控制，不冒充人工 blocked；旧 assert/dom unavailable 分支须在 v2 适配。run_store task.done 只表示收工，CI 看每案验证状态；unverified 非成功、立即终态、不占设备等人。

映射器服务 session_events/meta、run_store 和 Workbench，同种原因只一主码。旧 pass_rate 若排除不可核验，另报完整分母的未核验与误拒，不能作为可信度。

## 10. Evidence、verdict 与复核

Evidence 至少包含 EventRef={session_id,seq}、check/point_id、predicate_contract/verifier_build、逻辑 after_operation_id 与实际 action_ref、control_id/revision、事实 observation 引用、完整性、脱敏原读数、原子/组合结论及必要 artifact。case verdict 引用全部 required Evidence；receipt、do.plan_digest、模型 done 不能替代 check。

Workbench 展示 verdict→check→动作→事实→原读数/截图与当时配置，另显当前配置、来源版本、profile 和原因映射。截图/事实分别有来源，不声称同时原子采集；文件过期标 expired。指标和验收只在方案三定义。
