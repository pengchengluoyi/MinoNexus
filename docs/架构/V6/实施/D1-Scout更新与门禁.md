# D1：Scout 更新与门禁

## 代码依据

core.py node.update 当前直接调用无版本参数的 run_update；self_update.py 按配置 manifest/latest 选择。固定版本需贯通请求、EXECUTE params、处理器、安装计划和助手，不能只改签名。

enter_sleep 的活动任务检查可参考，但新 IDE 操作也需纳入，检查与安装锁必须防止竞态。Windows windows_update.py 已有延后安装、备份及安装失败时尽力恢复；不是跨平台完整历史版本回退。

NodeSession.draining 会使逐步 resolve(sn) 拒绝节点。新增 update_hold 只限制准入；persist_snapshot 仅写固定字段，需补持久化与启动恢复，不能假定新增内存属性自动保存。

case_runner 创建时选择设备并存 device_targets，RouterProxy 执行时仍 resolve(sn)。准入固定设备/节点，RouterProxy 核对漂移；循环代码保持不感知 Scout。

## 工作项

固定版本输入、平台匹配与摘要校验；update_hold/截止时间/队列状态；Scout 本地安装互斥；旧层保留与恢复；运行 build 确认；本地 CLI 固定版本安装及回退。

验收：现有批次后续步骤不被 hold 阻断；新批次排队回显更新 ID；超期释放仅自己的 hold；安装/超期竞争；重注册恢复；助手退出；版本回退与配置保留。D1 在 B 后开始，不依赖 C。


## 2026-10-17 开发记录

状态：已完成首轮源码实现与隔离验证，正式安装验收未完成；不处理 V5 遗留问题。

本轮交付：
- Nexus `scout_updates` 表及后台协调：固定 operation_id/release_tag/build_id，唯一节点门禁，默认1800秒空闲等待、取消/超时、持久回执。派发前登记，断线/重启只查询原操作。
- 批次资源准入在原 SQLite 写事务中检查门禁；已有批次继续，保存 allowed_node_ids；RouterProxy 所有实际发送路径核对设备节点漂移。
- 开发准备创建/派发共用门禁；运行中与未知效果 IDE 操作阻止安装，尚未派发的准备队列不算活动项。
- Scout 本地设备/IDE 活动复核、安装锁、固定 GitHub Release 清单及平台/摘要/大小/层依赖检查；独立旧 runtime/app 助手、完整旧 bin 保留、兼容回退、原操作人工恢复。
- 运行确认使用进程捕获 BUILD 与在线 status 回执；磁盘 VERSION 不再冒充已运行版本。CLI 提供固定版本更新、状态及恢复入口。
- MCP 更新/查询/取消工具及双仓协议文档同步；manifest 生成器显式接受已验证旧 build 列表，默认不授予自动回退。

接口：POST `/runtime/nodes/{node_id}/update` 必填 operation_id、release_tag、build_id；
GET `/runtime/nodes/{node_id}/update-status?operation_id=...`；
POST `/runtime/nodes/{node_id}/updates/{operation_id}/cancel`。
不支持排队的批次/准备入口返回409、blocked_by_update、更新 operation_id 与请求 task_id，未创建任务；不伪造已排队。
`release_tag` 是 GitHub tag，`build_id` 是清单及包内 BUILD 身份，两者分别核对。

已验证（全部使用独立工作区数据，没有触碰正式业务库）：
- 隔离 Nexus 实际启动，health、OpenAPI 三个接口、必填参数与未鉴权拒绝。
- SQLite 门禁阻止同节点新任务，其他节点不受影响；已有任务可以继续落状态。
- 取消幂等，等待过期释放，已派发安装不能被等待期限或取消释放。
- 另一个 Python 进程读取原操作及门禁；IDE 运行/未知与未派发队列的忙闲区分。
- 两仓实际 EXECUTE 编解码往返及 Scout runtime_capabilities 合同验证。
- 固定 tag 拒绝 latest/路径注入，本地锁互斥，ZIP 越界拒绝。
- 无回执、只读查询及错误 build 不宣称成功；匹配进程身份方可通过核验。
- 实际 ScoutCore 更新状态分发返回 not_found，不进行设备操作或安装。

发现并修复：首次新数据目录创建失败；在 SQLite 写锁内读取 NodeRegistry 的锁序风险；
严格执行和 Gmail 子路径绕过统一发送检查；助手写 PID 信息可能覆盖先到的成功回执。

未完成的正式准出项：
1. A 阶段 clean 发布包与 D1 正式基线安装；旧节点无 D1 合同明确 unsupported，首次升级仍走已有安装入口。
2. 真实活动批次后续步骤、安装/等待到期竞争、重注册、助手各阶段中断、实际旧层回退及配置保留的安装演练。
3. macOS/Linux/Windows 各目标平台的冻结运行时、服务托管、实际包层布局与恢复验收；源码检查不能外推跨平台支持。
4. 新旧 build 必须经过兼容性实测并列入 manifest rollback_build_ids；未列入时停止在维护状态，不能自行放行。

没有执行正式更新、重启、Git 提交/推送或 Release 发布；没有运行 pytest 或提交 tests/mock。


## 2026-10-17 P0 后续开发

状态：以下三个代码缺口已补齐；本轮仅完成语法、工作流解析与隔离 CLI 拒绝路径验证，正式成功/故障恢复路径未验收。不将前轮隔离记录扩大为本轮新逻辑已通过。

1. **切换前失败恢复**：`prepare` 下载、清单、摘要或兼容检查失败时，不改原 bin；核对旧 build/source_commit/clean 身份后释放运行锁，重新启动旧版。仅旧版重新注册并核验加载身份后终结为 `rolled_back`，保留预检失败原因；恢复失败仍为 `needs_operator`。此方案仍有停机窗口，未实现停机前异步下载。
2. **CLI 操作确认闭环**：独立助手读取其实际子进程的 runtime sidecar，核对 PID/创建时间、注册时间、连接状态、进程 build/source_commit 与 clean 标记。无需 Nexus 存在同名操作记录；普通 `update-status` 保持只读。Nexus 在线状态确认也限定原助手启动的子进程，旧进程和仅磁盘版本不能确认成功。
3. **发布兼容证据接入**：Scout release workflow_dispatch 接收 `rollback_evidence_json`，通过环境变量交给清单生成器。每个新旧 build 对必须附全部发布平台的更新、回退、配置保留成功标记及 HTTPS 证据地址。生成器拒绝错目标、重复/缺失平台、无证据的旧 build 参数；保留证据并生成 rollback_build_ids。tag 自动触发和无证据发布仍不允许自动回退。该输入是发布负责人的实测声明，格式校验不证明报告内容真实。

本轮实际验证：
- 修改的 Python 文件 AST 语法解析、release YAML 解析、git diff --check 通过。
- 实际 `python3 -m mino_scout.cli` 使用全新临时 MINO_SCOUT_HOME：latest 被拒绝、非正式安装被拒绝、状态查询返回 not_found；未创建操作账本。
- 未运行 pytest、未新增仓库测试/mock、未改正式配置或业务库。

仍须正式验收：干净且明确兼容的一对发布包；网络/清单/哈希拒绝后旧版注册恢复；无 Nexus 同名记录的 CLI 更新成功及失败回退；旧 sidecar/错误 build 不确认；跨平台冻结包和服务托管竞争。未执行 Git 提交、远端 CI、Release 发布或正式服务更新。


## 2026-10-17 恢复路径第二轮

本轮修复：
- 新版 Popen 失败和启动即退出返回未核验，由助手继续回退；未确认旧/新进程停止时仍保留维护状态。
- 恢复前核对旧层身份和可执行文件，避免先移动当前安装再发现旧包损坏；候选目录切换失败时恢复保留旧层。
- 下载候选与显式回退来源统一检查 clean build/source_commit；保留 preparation_error、startup_error、recovery_error 与 phase，并避免旧内存快照覆盖诊断。
- 发布清单拒绝缺失/模糊的 dirty/validated 标记、无效 build、重复或不支持的平台；兼容证据 URL 检查 HTTPS 主机及凭据/空白。

本轮实际验证（独立临时 HOME，无 pytest/mock、无业务库操作）：
1. 真实 OS 启动不存在的 Scout 可执行文件：返回未核验，账本持久保存 FileNotFoundError，未错误返回成功。
2. 真实 CLI update-recover 遇到损坏的保留 BUILD：非零退出、needs_operator、保留 JSONDecodeError；当前安装标记文件及损坏旧包均保持，未创建替换目录。
3. 修改文件 Python AST 解析及限定 git diff --check 通过。

检查记录在当前任务工作区 exports/mino-d1-recovery-checks.json。这是故障路径检查，不是正式发布验收；干净发布包成功注册、实际二进制失败后的完整回退、切换阶段中断及跨平台服务托管仍待实测。没有运行远端 CI、发布、提交或重启正式服务。
