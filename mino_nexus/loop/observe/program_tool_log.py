"""程序链 / 逻辑块 dispatch 写入 session_events（与 agent_loop 主路径对齐）。"""
from __future__ import annotations

from typing import Any


def log_program_tool(
    writer: Any,
    *,
    capability_id: str,
    params: dict[str, Any] | None,
    status: str,
    summary: str,
    executor_used: str = "internal",
    source: str = "flow_block",
) -> None:
    if writer is None:
        return
    cap = str(capability_id or "").strip()
    writer.append(
        "tool/call",
        {
            "capability_id": cap,
            "params": dict(params or {}),
            "source": source,
        },
    )
    writer.append(
        "tool/result",
        {
            "capability_id": cap,
            "status": str(status or ""),
            "summary": str(summary or "")[:800],
            "error": "",
            "executor_used": executor_used,
            "source": source,
        },
    )
