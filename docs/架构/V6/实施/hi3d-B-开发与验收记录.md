# B 开发与验收记录

日期：2026-10-17。状态：核心实现已落地，macOS 源码隔离联调通过，完整发布验收未完成。不包含 V5 遗留问题收口。

## 实现

- Nexus：应用开发配置与 revision、节点归属和维护权限、持久操作队列/幂等/未知结果人工处理、系统能力自动同步、development MCP 工具及会话截图 image block。
- Scout：本机 PyCharm Streamable HTTP MCP、连接 JSON、实际工具 schema 过滤、检查/执行/构建/受控重启/探测、持久回执、同节点 APK 安装及版本核对、配置日志只读脱敏。
- Workbench：应用配置中的开发环境入口、连接导入、项目和运行配置、权限确认、操作结果及状态刷新、APK 与日志入口。开发启动要求显式 Nexus 地址并隔离登录 token。
- 原始 cli/dev 配置保留；新增 cli V6-B（19105，独立数据目录，关闭 mDNS）和 dev V6-B（15287，连接 19105）。测试目标端口 15275 被无关进程占用时拒绝停止，改用空闲测试端口。

## 已验证

- 实际 PyCharm MCP Server 2026.1：读取配置及隔离 cli/dev 启动、重启；重启后监听 PID 改变。timedOut 回执继续探测，不重复启动。
- 隔离 Nexus HTTP → 真实 WebSocket → ScoutCore/NodeTransport → PyCharm。复用操作 ID、无执行许可拒绝、认证 header 拒绝均经过联调。
- 系统能力定义缺失 implementation id 的初始错误已修复为 ide.prepare.v1；后续隔离启动成功加载 8 项能力。
- Chromium：页面资源加载、保存、连接检查状态刷新、APK 区域显示通过，无页面/请求错误。
- 使用实际浏览器截图，经业务 publish_screen、session writer 落盘后，通过 MCP session_screenshot 取得 image block。验收会话：v6-b-browser-8cc5cfa56da443ad98436293b8ace3a9。历史已截断图片明确报错，不伪造恢复。
- TypeScript typecheck、相关 ESLint、Python 语法及应用路由加载通过。未使用 pytest，未提交本地测试脚本。

## 尚未完成的验收

- 正式安装版 Nexus → 正式安装版 Scout → 真实业务用例全链路；本次使用隔离数据目录与实际组件，不代表正式发布验收。
- Android 真机 APK 构建/安装、Linux 停止、冻结包 Scout 停止命令、配置日志读取的运行验收。
- 未配置 expected_build 时 build_verified=false；HTTP 就绪不代表目标版本已核对。
- Windows 自动停止、SSE transport、认证 IDE headers 属后续范围。

仓库存在同时进行的 A/D 改动，本次不将这些改动计入 B，也未代替用户提交或发布。

## 第二轮开发与验收（2026-10-17）

新增原回执恢复 HTTP/MCP、节点回执阶段记录、断线/过期收敛、节点归属复核、任务准入互斥、
不同操作异步调度；未知操作不会因后续日志记录超过 20 条而从操作列表消失。
前端补齐能力/阶段/版本/升级阻塞/恢复展示、APK 结果和可停止的有界日志刷新。
实现显式旧式 SSE、认证环境变量引用、Windows 文件锁与 WM_CLOSE 协作关闭；没有强杀分支。
APK 安装超时按未知处理，拒绝不确定情况下自动重装。回执持久写入 fsync。

真实隔离链路新增通过：版本匹配/不匹配、运行服务日志读取、目录外日志拒绝、运行中阶段读取、
慢探测期间并行日志读取、真实 PyCharm SSE initialize/tools/配置读取、第二个实际账号的截图与操作越权拒绝。
真实 WebSocket 断开使操作进入 unknown，同一 Scout 重连后通过原回执恢复，未重放原操作。
浏览器保存/检查/APK 显示及 MCP 图片返回继续通过，无页面或请求错误。
环境变量引用/缺失拒绝、明文秘密脱敏、同 IDE 排他锁、类型/语法/相关 ESLint 已检查。

用户确认正式包尚未产出、Android 尚未连接，因此正式安装链路、真机安装与已发布 build 的验证
仍等待环境就绪；本轮版本比较使用真实运行服务的 source-unpackaged 标识，只验证比较机制，
不宣称证明新发布代码已生效。Linux、Windows 与冻结 Scout 的运行实测仍未完成。
真实带鉴权 IDE 服务尚未提供，凭据引用和脱敏已验证，不宣称认证服务联调通过。
原有业务用例需要明确应用、用例与测试目标后执行，不能用隔离配置检查冒充业务用例通过。

## 本轮收尾

最终受控 cli/dev 启停及断线恢复再次通过。原回执只有 ID/参数摘要核验通过才允许恢复终态，读取错误不释放原占用。
用户指定 hi3d global 全量用例，已只读核对 Hi3d平台 / webhi3d_global / test 下 3 条项目用例。当前会话没有已登录 Nexus MCP/HTTP 连接，因此尚未提交真实跑批，等待接入正常登录入口；未读取凭据或绕过权限。
