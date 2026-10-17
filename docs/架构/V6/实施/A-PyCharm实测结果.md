# A 阶段：PyCharm MCP 接入与 Workbench 执行实测

日期：2026-10-15。使用用户已打开的 PyCharm 2026.1 与真实 Workbench `dev`；Nexus、Scout 未停启或更新。以下为审计客户端直连，不是已完成的 Nexus→Scout→IDE 产品链。

## 接入与项目选择

| 项目 | 结果 |
| --- | --- |
| HTTP 流端点 | `http://127.0.0.1:64342/stream`，initialize HTTP 200 |
| 协议 | `2025-03-26`，复用返回的会话标识，initialized 通知 202 |
| 认证 | 当前本机请求未携带 Authorization 即可调用；不外推其他机器或配置 |
| 工具发现 | 28 个工具，listChanged=true |
| 项目入口 | `/Users/changpengcheng/code/MinoNexus`；其他两仓路径不被识别为独立 MCP 项目入口 |
| 共享窗口 | get_project_modules 返回 Nexus、Scout、Workbench 及历史 Studio；仅前三者纳入方案 |
| 运行配置 | dev、dev:web、cli、cli (1)、service；错误 projectPath 被拒，不回落到其他项目 |
| 配置映射 | IDE 配置文件确认 dev 为 MinoWorkbench/package.json 的 npm run dev；dev:web 是历史 Studio，未调用 |
| 未验证项 | SSE 传输、独立窗口多项目路由、Nexus/Scout 配置的执行和环境隔离 |

用户手工开启 MCP 并复制 HTTP 流配置。早期 63342/mcp 的 404 与 64342/stream 的成功不是同端点前后对照，不能把 404 一律判为未授权。Brave 设置由截图确认已勾选，本轮成功执行未再要求确认；未发现可查询该设置的接口，不显示“自动检测已开启”。

## 原生工具边界

execute_run_configuration 参数为 configurationName、projectPath、timeout（毫秒）、maxLinesCount、truncateMode。没有 waitForExit、fullOutputPath 或动态环境覆盖参数；返回 exitCode、timedOut、output，没有稳定运行实例 ID。

本机公开工具未发现独立 stop/restart/list-running/status 或输出续读接口。execute_terminal_command 的只读 pwd 调用返回正常；随后对已核对的 Workbench dev 实测了受限 SIGTERM，再用 execute_run_configuration 启动。不能因此向 Agent 开放任意终端命令或凭端口号杀进程。

## 真实 Workbench 调试记录

### 首次：已有实例占用端口

首次对 dev 调用 timeout=10000，HTTP 客户端等待约 40 秒后超时，没有收到结果；调用前后 5273 监听 PID 均为 11345。随后用户截图明确显示第二个 dev 报 `Port 5273 is already in use`，原实例仍运行。由此确认该调用新执行了配置，没有接管或重启原实例。

首次 HTTP 超时的原因未确定；不能归因于 MCP 未启用、Brave 未授权或某个已证实的插件故障。后续受控复测正常返回，未复现该回传超时。

### 第二次：停止旧实例后成功启动

用户在 IDE 停止原先 Workbench dev，并通过面板确认。检查 5273 无监听后，仅发送一次：

```json
{
  "configurationName": "dev",
  "projectPath": "/Users/changpengcheng/code/MinoNexus",
  "timeout": 5000,
  "maxLinesCount": 60,
  "truncateMode": "END"
}
```

- 约 5.012 秒得到 HTTP 200、application/json，MCP isError=false。
- structuredContent 返回 timedOut=true、Vite ready 输出，exitCode 未提供。
- Workbench 新进程 PID 25527 在 5273 监听，HTTP GET `/` 返回 200；MCP 返回后仍运行。
- 结论：原生 MCP 启动及单次输出已通过。timedOut=true 在本例表示等待窗口结束，不是启动失败，也没有停止服务。

### 第三次：受控复现端口冲突

为核对首次错误的回传，保持第二次实例运行，用相同 dev、timeout=5000 再调用一次。约 0.599 秒返回 exitCode=1、端口占用输出，MCP isError=false。PID 25527 继续监听，HTTP 200。此次仅产生失败的启动尝试，未重启现有实例。

结论：MCP 调用成功与程序执行成功必须分开判定；非零 exitCode 是执行失败，不能只检查 isError。重复调用 dev 不能代替重启。

### 第四次：IDE 人工“重新运行”验证

用户选中正在运行的 Workbench dev，点击 PyCharm 运行工具窗口的“重新运行”，确认出现新输出。5273 的监听 PID 从 25527 换成 28803，HTTP GET `/` 仍返回 200；未操作 Nexus/Scout。PyCharm 官方运行窗口说明：Rerun 会停止当前程序并重新运行；“Rerun automatically”用于相关源码变化触发测试重跑，两者不是同一按钮。本次证明 IDE 的人工一键重启可行，不证明 MCP 可以远程调用 Rerun，也不凭 HTTP 200 单独证明新代码版本。

### 第五次：受控终止 + MCP 启动

按用户要求减少人工步骤，先核对 5273 唯一监听 PID 28803，工作目录 MinoWorkbench、所有者当前用户，父进程 PID 28784 是 PyCharm PID 41515 启动的 `npm run dev`。IDE MCP execute_terminal_command 运行 `/bin/kill -TERM 28803`，约 0.201 秒返回 command_exit_code=0；旧 Vite/npm 与 esbuild 子进程退出，5273 释放。随后仅执行一次 MCP `execute_run_configuration(dev, projectPath=MinoNexus, timeout=5000)`，约 5.035 秒返回 timedOut=true 与 Vite ready；新 PID 29501 监听 5273，HTTP GET `/` 返回 200。没有 SIGKILL，也没有停止 Nexus/Scout。这是审计客户端手动调度的实测，Scout 产品能力尚未实现。

## 已确定的实施路径

| 能力 | 结论与首期处理 |
| --- | --- |
| 配置导入、初始化、工具发现、项目配置读取 | 可进入 Scout 接入实施；配置保存在指定 Scout，Nexus 保存引用 |
| 启动已登记配置、获取单次输出 | Workbench 已实测通过；随后由 Scout 做应用就绪探测 |
| 复用已有服务测试 | 可行，但可访问不证明本轮代码生效；仍需目标/版本检查或 HMR 验证 |
| 停止/重启 | IDE 人工 Rerun 与受控 SIGTERM→MCP 启动均已实测；正式 Scout 的自动识别、任务门禁和隔离尚待实施 |
| 长驻超时 | 有回执 timedOut=true 时探测就绪，不重发；无回执的 HTTP 超时记 unknown |
| 错误输出 | 非零 exitCode 判失败；端口冲突提示复用或人工停止，不自动换号/杀进程 |
| 输出续读、实时运行列表 | 未提供独立接口，本期不伪造实时进程状态 |
| 自动停止/重启硬要求 | Scout 用应用已有测试地址和源码目录定位旧进程；无法唯一定位或权限不足时给出原因，转 IDE 人工处理，不默认 SIGKILL |

## 前端提示与后续执行

前端区分“已连接”“配置执行已验证”“服务就绪”“用例通过”。有回执 timedOut=true 提示“已返回运行输出，正在检查服务是否就绪”，不直接显示失败；无回执则提示结果未确认；exitCode=1 显示启动错误。完整交互见 [前端合同](B-开发环境前端交互.md)。

下一步实施 B：Scout HTTP 流客户端、连接配置存储、项目/配置绑定、按应用已有配置定位并停止旧进程、既有 EXECUTE 通道和单次输出回传。Scout 已依赖 psutil；进程创建时间用于防 PID 复用，不要求用户额外登记进程身份。先在真正的测试端口及数据根验收正式 Nexus→正式 Scout→IDE。Nexus/Scout 测试实例本轮未执行。

本轮沿用现有 Workbench 5273，未修改源码、运行配置或前后端连接地址；不代表规划的测试端口 5274、运行时地址或正式/测试隔离已经落地。结束时保留受控重启后的 Workbench 正常运行。

取证文件：pycharm-tools.json、pycharm-configs-sanitized.json、workbench-execution-result.json（首次）、workbench-start-result.json、workbench-conflict-result.json、workbench-term-result.json、workbench-restart-result.json。后四者为真实 MCP 回执；客户端会话标识未写入产物。
