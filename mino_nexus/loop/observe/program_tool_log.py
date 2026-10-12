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
    result: Any = None,
) -> None:
    if writer is None:
        return
    cap = str(capability_id or "").strip()
    from mino_nexus.loop.observe.execution_diagnostics import execution_result_metadata
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
            "error": str(getattr(result, "error", "") or "")[:800],
            "executor_used": str(getattr(result, "executor_used", "") or executor_used),
            "source": source,
            **execution_result_metadata(result),
        },
    )
