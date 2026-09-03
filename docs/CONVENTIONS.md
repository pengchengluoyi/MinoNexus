# CONVENTIONS — MinoNexus

## 1. 日志

模块级 `TAG` 常量 + `SLog.{d,i,w,e}(TAG, msg)`（沿用上游形态）。

| 规则 | 说明 |
|---|---|
| TAG 用驼峰模块名 | `CaseRunner` / `RouterProxy` / `NodeRegistry` |
| **不记凭据** | `device_hint` 里的 password、token 一律打 `***` |
| **不记截图 base64** | 打长度和尺寸：`shot 1080x2340 bytes=182344` |
| LLM 调用必须落痕 | 走 `dispatch_log.record_llm`，便于事后归因决策 |
| 每步一行摘要 | `run_id` 前 8 位 + step + cap + executor + status + 耗时，与 Scout 的日志可对齐 |

## 2. 异常

| 层 | 约定 |
|---|---|
| `routers/` | 业务错误抛 `HTTPException` + 明确 message；不要把 500 堆栈丢给 UI |
| `loop/` | 循环内单步失败不终止整轮 —— 记 `fail` 继续。只有闸门失败、连续截图失败、`decline` 才终止 |
| `local_executors/` | 与 Scout 侧同规：**永不抛，返回五态** |
| `RouterProxy` | Scout 超时 / 断连 → 返回 `EventStatus.FAIL` + 原因，**不要抛到循环里** |
| `ai/llm_client` | LLM 不可用返回 `None` + meta，由调用方决定降级；不要抛 |

## 3. 五态

`pass` / `fail` / `skipped` / `blocked` / `declined`（**小写**，且是 `pass` 不是 `OK`）。取值与上游 `EventStatus` 逐字一致，语义与 Router 处置见 `docs/PROTOCOL.md` §4.6.1。

Nexus 侧的额外规定：

- Scout 回 `declined` 时，`RouterProxy` **不自行换 executor 重发** —— fallback 链已在 `executor_order` 里表达，由 Scout 内部走完
- Scout 回 `blocked` → 循环转入 HITL 流程，**不当失败**
- `RouterProxy` 自己产生的失败（超时、序列化错误）一律 `fail`，`executor_used` 填 `router_proxy`
- `skipped` 不计入失败率，但要在覆盖度里体现为未观察

## 4. 守门脚本

`scripts/verify_*.py`：纯静态、无需起服务、退出码非 0 即失败、失败时打出文件与行号。

```bash
python scripts/verify_all.py
```

| 脚本 | 断言 |
|---|---|
| `verify_no_device_driver.py` | 无设备驱动与图像算法依赖 |
| `verify_no_scout_import.py` | 不 import Scout / 上游 server 包 |
| `verify_protocol_contract.py` | protocol.py round-trip 全部 fixture + 哈希一致 |
| `check_http_contract.py` | 登录 / 节点 / Scout 凭证 / `X-Mino-Client` 门禁 / 下发 / 角色 / qa-process / Figma（需安装依赖，不进 `verify_all`） |

新增硬约束时同时新增 verify 脚本 —— **写在文档里的约束会漂移，写在脚本里的不会。**

## 5. 提交与 PR

- 协议改动必须两仓同 PR 周期完成，四步流程见 `CLAUDE.md` §5
- 从上游搬文件的 commit 注明原路径：`port(nexus): agent_executor.py from MiniOrangeServer server/services/regression/agent_executor.py`
- **不在同一个 commit 里既搬迁又重构。** 先原样搬（保证行为一致），再单独 commit 改造 —— 否则行为回归时无法二分
