# D3：Workbench MCP 更新入口（2026-10-17）

## 已完成的范围

Nexus 新增六个 MCP 工具，复用已安装的 `launcher/mino-maintain`；安装端 `updates/hi3d-<operation>.json` 是操作权威，不在 Nexus 新建一份升级状态机。HTTP 与 MCP 均要求当前账号具有管理员角色。

首期仅支持 **macOS、Nexus 同机、一个显式登记的 Workbench 安装**。没有登记时明确拒绝。跨主机更新、Linux/Windows、正式 Release 发布、正式业务 build 组合，以及打包后的正式 Nexus 发起成功升级/回退的端到端验收仍未完成。

## 登记与暂存

在 Nexus 服务自身的环境配置中设置 `MINO_WORKBENCH_INSTALL_ROOT`，值为 Workbench 安装根的已解析绝对路径。只在终端临时设置不会改变已有服务的环境。该目录必须已由 Workbench 原生安装器创建，包含安装身份和独立维护入口。不要指向源码目录、旧基线、符号链接或远程挂载的另一台机器。

操作者从指定 Release 获取并暂存目标包，目录固定为：

```text
<Workbench安装根>/updates/prepared/<build_id>/
  hi3d-manifest.json
  hi3d-package.zip
```

清单内的 build 必须与目录和请求一致。文件不能使用符号链接或硬链接。暂存由本机部署操作者负责；API 不接收任意路径、命令、URL、服务 label 或 `local_verification`。实际更新仍由原生助手验证平台、包摘要/大小、安装边界及已实测 Nexus build 组合。

版本检查只读取平台清单，返回 `package_verified=false`、`compatibility_verified=false`；不能把检查成功当作升级准入成功。回退使用已保留的版本，不要求另暂存包，但同样受兼容性检查约束。

## MCP 合同

| 工具 | 参数 | 行为 |
|---|---|---|
| `workbench_installation` | 无 | 获取登记的 installation_id 与磁盘 current build；不冒充进程健康验证 |
| `workbench_update_check` | installation_id、build_id | 检查指定暂存清单 |
| `workbench_update` | installation_id、operation_id、build_id、可选 rollback | 请求固定版本更新；rollback=true 回退保留版本 |
| `workbench_update_status` | installation_id、operation_id | 只读安装端原操作记录 |
| `workbench_update_cancel` | installation_id、operation_id | 取消 requested 操作；安装开始后不强行取消 |
| `workbench_update_recover` | installation_id、operation_id | 恢复原助手操作；终态不重装；拒绝恢复 local-verification 操作 |

除安装身份查询外，各工具必须携带准确的 installation_id。相同 operation/参数复用原记录，不同 build、更新/回退类型或本地验收标记发生冲突时拒绝。相同 MCP JSON-RPC 写请求的回执也保留 installation_id、operation_id、build_id 和 state，便于继续查询。

正常路径为：查询安装 → 检查指定版本 → 更新 → 按原 operation_id 查询。若助手启动确认丢失，返回已确认的原操作与恢复说明；本机命令等待超过 30 秒时返回 observation=unknown 和可取得的 last_confirmed，不杀掉仍在执行的请求，不自动创建替代操作。查询仍以安装端记录为准。

dirty 包不能通过 MCP 更新或回退；本地显式验收请求也不能借 MCP recover 继续执行。MCP 可以查询或取消尚未执行的本地请求。needs_operator 仍需本机维护，不提供强制解锁、删除记录或 Nexus 数据库回滚。

HTTP 路由与工具一一对应：

- GET `/sys/workbench/installation`
- GET `/sys/workbench/updates/check`
- POST `/sys/workbench/updates/request`
- GET `/sys/workbench/updates/status`
- POST `/sys/workbench/updates/cancel`
- POST `/sys/workbench/updates/recover`

GET 的参数使用 query；POST 使用 JSON。POST 拒绝额外字段。未登录返回 401，非管理员返回 403；身份、暂存或操作冲突返回 409，无效参数返回 422。

## 本轮验收证据

使用新的隔离 Nexus 数据目录、真实 HTTP/MCP、已有最终原生安装包和安装后的独立助手验证，没有使用 mock 或 pytest，没有修改正式服务、发布 Release 或触碰真实业务数据库。

通过：六工具发现；安装身份读取；原生平台清单检查；dirty 更新与回退拒绝；本地验收操作恢复拒绝；操作参数冲突拒绝；原生取消；相同/不同 JSON-RPC ID 的取消幂等；权威状态查询；匿名/普通账号拒绝；错误或空安装身份、路径越界参数与绕过字段拒绝。安装 current 保持不变，隔离 Nexus 已停止，操作与日志保留。

验收还修复了两个实际问题：PyInstaller 通用失败尾行遮住具体拒绝原因；通用 MCP 写回执没有保存 Workbench 的 operation_id。错误现在保留具体异常原因，重复写请求仍能取得原操作身份。

本轮使用 dirty 本地验收包，因此**没有宣称 MCP 发起的正式更新/回退已成功，也没有宣称业务兼容组合已通过**。上一轮已通过的本地 CLI 升级、热切换、回退和中断恢复结论保持原范围。
