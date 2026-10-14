# 辅助请求幂等键冲突与 IM 确认异常：cr-72f483f92cdf

日期：2026-10-14。关联 `cr-72f483f92cdf · case-bc8b51cf` 和 16:29:48–16:31:01 微信入站日志。排查仅只读共享 SQLite；验证使用独立临时 MINO_NEXUS_DATA_DIR。

## 实际任务失败点

会话 `cr-72f483f92cdf::case-bc8b51cf::5fda2f6d`，16:24:02 开始、16:24:18 结束，30 个事件，状态 fail、summary=request_key_conflict。前置进行了冷启动准备、设备恢复和版本读取，第一个循环回合在 14ms 内停止，没有模型决策或帖子详情校验。因此不是上次 cr-008eeb42ce13 的 checkpoint_results 解析问题。

seq 7/13/17 的设备证据还记录屏幕从 asleep 恢复到 awake，但两次 dismiss_keyguard 后仍 locked=yes、capture_ok=no。请求身份修复不能证明设备已解锁，也不能放行实际缺图；后续验收须核对真实设备事实。

## 两处报错的共同根因

前一轮 V5 引入 Scout RequestGuard，以 `(run_id,step_idx)` 登记完整 Execute。守卫正确拒绝同键不同内容，但 Nexus 的 legacy observe/cancel/plugin 与无任务 RPC 仍固定使用 step_idx=-1，node_plugin_call 更始终使用空 run_id 和 -1。第一次请求占键后，不同参数截图、下一条发送或 ACK 都复用旧键，于是冲突。完整守卫与辅助发送方没有接齐，这是此次改动造成的回归。

微信处理失败和两次 im-inbound 线程异常均指向 PluginCallError/request_key_conflict。业务处理本身已捕获失败并记入 receipt，但成功后的 else 分支和已处理消息的提前返回分支在 try 范围之外调用 `_ack`，因此确认异常穿透线程。

## 修复

- 新增 core/wire_identity.py，辅助负编号全进程原子递减、以启动微秒分代；-1 留作待分配哨兵。首次发送补编号，同载荷重取保持编号和完整内容。保留原 run_id，包括无任务 RPC 的空值，避免把聊天或主机命令伪造为活动任务。已有正编号和 focus_ref 不变，V5 缺 run_id 拒绝。
- NodeConnection.request 在序列化前兜住全部未编号 EXECUTE；node_plugin_call 建载荷时使用同一 helper。日志/运行状态、主机命令、输入法与 IM 持有权也受此闸门保护，不只修截图或 ACK 一个调用点。
- RouterProxy 观察、具名插件和结束通知改用同一辅助分配器，V5 读请求共享编号空间。正写编号分配器不变，不消耗写步骤、不换任务 run_id。
- `_ack` 捕获失败，返回 False 并仅记录闭集原因或异常类型；保留 receipt 的 done/uncertain 状态。渠道重投再尝试 ACK，不重放已处理或副作用未知的业务；不伪造确认成功。
- 两仓 PROTOCOL 同步消除旧「负编号不做幂等」说明；协议字段不变，Scout RequestGuard 不变，不需替换安装的 Scout。

## 防回归要求

不同请求不能复用空 run_id/-1 或同任务/-1；首次补身份与同请求重取必须分开。不关闭完整请求守卫、不忽略 params/timeout 等内容变化、不自动改正动作键重放未知副作用。

保留 IM delivery_id、receipt、owner 和来源校验；ACK 失败不等于业务失败，不能把 done 改为 retryable 或重放 uncertain 变更。日志不能打印上下文 token、账号、聊天正文和完整载荷。无任务 RPC 不伪造活动任务。

保留真实 wire tap 回执/focus_ref、项目归属、取证缓存失效、有界墙钟和 cr-008 的逐项校验契约。MinoWorkbench 是唯一前端，没有 Console/Studio 配置或验收入口。

## 验证及生效边界

已完成 41 项本地检查，包含真实 NodeConnection、两仓协议和 RequestGuard 的本地 WebSocket 回放：原固定截图键和插件 send/ack 键能复现冲突；修复后不同截图、send/ack/history、主机/日志/持有权请求独立完成；同载荷重取稳定，真实正键不同内容与在途重复仍被拒绝。并发 400 个辅助编号无重复，当前日期负编号可由前端数字精确表示，正动作和 focus_ref 保持。

ACK 失败、done 重投、后续 ACK 恢复与 uncertain 消息通过真实临时库 receipt 状态机检查。不向外部 IM 发送消息、不操作真机、不调用模型、不跑 pytest、不提交测试/模拟文件、不写共享数据库。

源码修复不代表线上整案已通过。Nexus 没有热重载，须加载新代码后在 Workbench 复跑，并核对微信下次入站不再出现固定键冲突及未捕获 ACK 异常。历史任务、旧 receipt 和旧 verdict 不直接改写。锁屏若仍存在，依据新的 session 单独处理，不伪报通过。
