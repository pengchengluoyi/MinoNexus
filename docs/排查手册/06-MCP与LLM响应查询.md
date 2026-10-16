# MCP 与 LLM 响应查询

需求：让外部 Agent 通过 mino MCP 分析任务失败，不必读取数据库、索取登录凭据或依赖截断的任务摘要。

## 1. 接入与调用顺序

使用 Workbench 生成的个人 MCP token 接入 Nexus `/mcp`，保持现有认证和工具白名单。客户端配置文件由 MCP 客户端读取，不能把 token 打印到日志或发给模型。

连接后调用 `tools/list`，下列诊断工具仅开放给 MCP，均为只读，不会重跑用例或操作设备：

| 工具名 | 参数与用途 |
|---|---|
| `task_get` | `task_id`：任务状态和用例结果；MCP 返回完整结构，不再替换成 4000 字符 preview |
| `session_list` | `run_id`，可选 `case_id/status/limit/offset`：查完整会话 ID |
| `session_events` | `session_id/from_seq/limit`：读取原始事件及工具、守卫、回看事实 |
| `session_llm_list` | `session_id`，可选 `turn/from_seq/limit`：查模型调用及 `dispatch_id` |
| `llm_get` | `dispatch_id`，可选 `section/offset/limit`：读取响应、请求或元信息 |

定位 `cr-c8a13f5e7b41` 的调用顺序：

```json
{"name":"session_list","arguments":{"run_id":"cr-c8a13f5e7b41"}}
{"name":"session_llm_list","arguments":{"session_id":"<上一步返回的完整 session_id>"}}
{"name":"llm_get","arguments":{"dispatch_id":"<目标回合的 dispatch_id>","section":"response"}}
{"name":"llm_get","arguments":{"dispatch_id":"<同一 dispatch_id>","section":"request"}}
```

以上为 MCP `tools/call` 的 `params`，不是聊天正文。查看 `session_events` 中的 `review/hold`、`review/fail` 与 `exec/vision`，再用同回合的 LLM 响应核对模型是否返回 `prior_status`。

## 2. 分页与完整性

- `session_list` 按 `offset/limit` 翻页，结合 `total` 判断结束。
- `session_events`、`session_llm_list` 按 `next_from_seq` 翻页。后者在数据库内筛选 LLM 事件，不受旧投影只读取前 2000 个事件的限制。
- `llm_get` 的 `content` 是所选 section 的 JSON 文本片段。`has_more=true` 时使用 `next_offset` 继续；顺序拼接全部片段后再解析 JSON。`total_chars` 为总字符数，不是字节数。
- MCP 不经过 IM 的 4000 字符摘要裁剪；IM 原有行为保持不变。客户端自身的显示或上下文限制不属于服务端截断，应优先分页查询。

## 3. 原始响应与历史限制

新 LLM 记录增加 `raw_response`，保存 JSON 解析及工具决策归约前的助手文本与原始 `tool_calls`，包括 function arguments 字符串；不走旧输出的 8000 字符裁剪。图片仍通过现有媒体提取处理，不返回 base64 全图。这里的“原始响应”不是供应商完整 HTTP 报文。

`llm_get(section=response)` 同时提供：

- `raw_response`：解析前文本和工具调用。
- `parsed_output`：原有日志输出，供前后比较；历史记录可能已在落盘时裁剪。
- `parsed_output_truncated`：新记录明确标识解析输出是否在落盘时裁剪；旧记录为 `null`，代表没有该标记。不能把裁剪造成的字段缺失归因于解析器。
- `raw_response_available`：是否实际保留了解析前响应。
- `raw_response_redacted`：是否因原有助手隐私规则隐藏正文。

历史记录不能补回之前没有保存的原始参数。`raw_response_available=false` 时，只能把 `parsed_output` 称为已有的解析结果，不能断言模型当时确实漏填字段。`section=request` 读取原有保存的系统提示和用户输入，历史 8000 字符裁剪也无法还原。

不公开 `/settings` 配置、provider key、认证信息或任意 HTTP 代理。诊断详情复用既有调度记录，走 `/case-runner/llm/{dispatch_id}` 的独立只读接口。已超过调度日志保留范围的记录返回 404，不伪造空的成功结果。

## 4. 发布与验收

发布后重启 Nexus，并让 MCP 客户端重新连接、刷新工具列表。无需升级 Scout 或修改 Nexus/Scout 协议。

验收以真实 Session 为准：按 run_id 找会话，按回合找 dispatch_id，获取完整响应并对比原始 function arguments 与解析输出；验证分页续读、历史不可用标记、助手隐私脱敏以及不存在的 ID 返回错误。运行服务未加载新版本之前，不能将这些检查标为已通过。


## 2026-10-15：通过 MCP 更新 Scout

新增 `scout_list`、`scout_update(node_id, reason?)`、`scout_update_status(node_id)`。先列节点取得准确 node_id（不是设备 SN），明确更新目标后调用更新，再查询状态。服务需加载本次代码，客户端刷新 tools/list。

更新复用现有 node.update，使用 Scout 已配置的 manifest（默认最新发布）。不支持 version、降级或自定义 URL；额外参数明确拒绝，不会静默忽略用户指定的版本。通用 stop/restart/command 路由不向 MCP 放开。

节点归属、在线状态、已知活动任务和更新在途均检查；同一 Nexus 进程内并发更新拒绝。沿用账号与稳定 JSON-RPC id 的写收据，重取不重放。Scout 失败返回 MCP isError；超时或异常为结果不确定，先查状态，不能换请求 ID 盲重试。

成功返回 accepted 与 running_version_verified=false，仅代表 Scout 已应答。查询返回 running_version（在线节点上报）、last_reported_version、process_generation、connection_generation、update_job 和 command_inflight；离线 running_version=null。应在重连后核对实际运行版本，不能从“下载完成”或旧版本号判定更新成功。

当前入口不是 V6 D1 的持久更新队列：尚无跨进程/全入口原子 update_hold，活动任务检查后新任务仍可能竞入；应在维护空闲窗口调用。更新不会清除已有未知写隔离。固定版本、回退、排队和自动终态核验需 Scout/Nexus 后续共同实现。

本次用隔离 SQLite 和真实 ASGI/MCP 完成鉴权、工具发现、离线拒绝、重复请求和参数边界检查；未使用 pytest/mock，未修改业务库，未执行真实节点升级。线上下载安装/重连验收另记。
