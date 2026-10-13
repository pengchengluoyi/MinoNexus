# 模型 finish_reason=tool_calls 但无 tool_calls（cr-f15eb6081870）

## 现象
任务 `cr-f15eb6081870`，用例 `case-b1d321c8` 在 prep 阶段 `blocked`：
「模型响应标记了工具调用，但没有可执行的操作内容；本轮未下发设备操作」（seq 233 `contract/blocked`，`reason_code=model_no_tool_calls`）。

## 排查（只看 session 轨迹）
- seq 225 / 230：两次 `agent-vision-exec` 的 `llm/response` 均 `status=error`、`http_status=200`、`finish_reason=tool_calls`，
  `completion_tokens=77`，`output_len=0`。
- `response_trace`：`provider_parsed.tool_count=0`、`sdk.tool_count=0`、`empty_call_origin=provider_parsed_output_empty`
  —— 空发生在 **provider 返回层**（火山 `doubao-seed-2-1-turbo`），不是 Nexus 解码或 SDK 归一化丢的。
- 调用参数：`tool_choice=required`、`thinking=disabled`、`max_tokens=768`、`temperature=0.1`、11 个 tools。
- 设备侧正常（`probe_device_state` pass，目标 App 在前台），与设备无关。

## 根因
模型实际生成了 77 token（工具调用），但火山侧未能解析成 `tool_calls`，返回 `finish_reason=tool_calls` + 空数组。
`call_chat_text` 的重试虽然会再打一轮（non-stream），但**参数完全相同且低温**，输出近似确定性复现，
两轮都空 → `no_tool_calls` → 本轮 `give_up/blocked`。

## 修复
`mino_nexus/ai/llm_client.py::call_chat_text` 重试轮：当上一轮 `finish_reason ∈ {tool_calls, function_call}` 且无有效 tool_calls 时
- `tool_choice: required → auto`（避开强制模式下的解析路径）
- `temperature` 抬到 ≥0.4（换采样路径）
- `max_tokens` 至少 1536
- `meta.empty_tool_calls_retry=True`，便于在 dispatch 日志里区分
仍保留 tools，不降级成正文 JSON 执行（`agent-vision-exec` 为 `allow_tools_downgrade=False`）。

## 验证方式（不写 pytest）
重跑同类用例，查 `llm/response.payload.meta`：出现 `empty_tool_calls_retry=true` 且第二轮拿到 `tool_name` 即生效；
若仍连续空，下一步看火山侧是否需要对该模型开启 `thinking` 或缩小 tools 集合（`tool_count` 11 → 当前里程碑相关子集）。
