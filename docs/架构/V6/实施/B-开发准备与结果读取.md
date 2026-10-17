# B：开发准备与结果读取

## 代码依据

Nexus rRuntime.py 已用 Execute(run_id="", step_idx=-1) 下发循环外节点/插件命令，在线连接发送 EXECUTE。新增开发准备服务复用该传输，不能让 Web 请求绕过节点权限，也不把准备伪造成用例 run。

Scout core.py 区分 node/plugin 请求；新增 PyCharm 接入属于程序系统能力：由 Nexus 启动时自动注册和升级能力目录版本，代码声明平台、caller、phases、版本和受限参数；不走 Workbench 手工维护/点击升级。Scout 实现对应处理器并报告支持情况，Nexus 派发完整载荷，旧节点不支持时明确拒绝。MCP 客户端依赖进入 runtime 层兼容要求，适配代码进入 app 层，不能仅发 app 让旧 runtime 缺依赖。

已有 MCP 工具定义 task.get、session.events、session.list、task.list、case.run 等；截图取得、IDE 输出读取需要补齐并验收。业务读取有账号/应用/节点范围，文件读取限配置目录。

## 前端提示与实测约束

按 [开发环境前端交互](B-开发环境前端交互.md) 实现连接/项目/能力分离状态、Brave 未知提示、人工步骤及结果不确定状态。以实际 schema 生成能力支持，不把官方其他版本的 waitForExit 等参数发给本机 2026.1。现提供执行回执与配置日志的有界快照读取，支持显式定时刷新；不是无限日志流。

## 工作项与验收

连接配置使用 Scout config.py 的 JSON ide_connections 列表，不扩 im_store.py/SQLite。保存时合并其他配置字段，避免覆盖 nexus_url/token；无鉴权连接优先；认证仅使用 Scout 本机环境变量名称引用，不接收或存储明文认证 header。读取配置、启动、构建及输出优先用已有 MCP 工具，停止优先使用 MCP 终端的固定动作，仅工具不可用时用 Scout 本地后备。

1. 系统能力在新库启动时自动注册，旧库升级启动时同步版本/声明，重复启动不产生重复项；无需 Workbench 人工升级。随后完成应用开发环境配置及 revision；无配置不影响正常测试。
2. 检查连接、读配置、执行配置、查结果，持久 operation 与必要节点回执。
3. 停止前核对测试端口、源码 cwd、当前用户及 PyCharm 父进程链；复用 process_control.py 的 PID/create_time 校验思路，发信号前复核，不调用其 Scout 专用 stop 文件机制来停其他应用。仅 SIGTERM，超时报告、不升级 SIGKILL；旧进程退出且端口释放后由 MCP 启动。macOS/Linux 分别验收，Windows 停止后置。重复操作不重复启动，无端口目标不套用此后备方案。
4. Web/Python 准备→实际版本→真实用例；APK 同节点构建安装并核对版本。
5. Agent 仅连正式 MCP 即可读取失败步骤、事件、截图和构建输出。跨目标秘密不泄露，超时和截断可见。

协议实现和文档同步，旧 Scout 未支持时返回 unsupported；是否提升 wire 版本取决于合同兼容性，而不是仅因为新增业务能力。

## 实施进度

见 [开发与验收记录](hi3d-B-开发与验收记录.md)：核心实现与 macOS 隔离联调已完成，正式安装链路、真实用例及设备验收仍待完成。
