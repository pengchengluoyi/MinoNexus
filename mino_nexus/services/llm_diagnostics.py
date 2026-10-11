"""面向 Agent 的只读 LLM 诊断投影与分页。"""
from __future__ import annotations

import json
from typing import Any


def list_session_calls(
    session_id: str,
    *,
    from_seq: int = 0,
    limit: int = 50,
    turn: int | None = None,
) -> dict[str, Any]:
    from mino_nexus.core.database import SessionLocal, ensure_db
    from mino_nexus.models.session_event import SessionEvent

    ensure_db()
    db = SessionLocal()
    try:
        query = db.query(SessionEvent).filter(
            SessionEvent.session_id == session_id,
            SessionEvent.type == "llm/response",
            SessionEvent.seq >= from_seq,
        )
        if turn is not None:
            query = query.filter(SessionEvent.turn == turn)
        rows = query.order_by(SessionEvent.seq.asc()).limit(limit + 1).all()
        has_more = len(rows) > limit
        items = [
            {
                **dict(row.payload or {}),
                "seq": int(row.seq),
                "turn": int(row.turn or 0),
                "phase": row.phase or "",
                "ts": row.ts,
            }
            for row in rows[:limit]
        ]
        return {
            "session_id": session_id,
            "items": items,
            "count": len(items),
            "from_seq": from_seq,
            "has_more": has_more,
            "next_from_seq": items[-1]["seq"] + 1 if has_more else None,
        }
    finally:
        db.close()


def get_call_page(
    dispatch_id: str,
    *,
    section: str = "response",
    offset: int = 0,
    limit: int = 12000,
) -> dict[str, Any] | None:
    from mino_nexus.ai.dispatch_log import get_call

    row = get_call(dispatch_id)
    if row is None:
        return None
    metadata = {
        key: row.get(key)
        for key in (
            "id", "at", "job", "model", "provider_id", "status", "error",
            "prompt_version", "prompt_sha", "elapsed_ms", "total_tokens",
            "used_tool_calls", "tool_name", "tools_downgraded", "finish_reason", "fail_kind",
        )
    }
    raw_available = bool(row.get("raw_response_available"))
    raw_redacted = bool(row.get("raw_response_redacted"))
    if section == "response":
        selected = {
            "raw_response": row.get("raw_response") if raw_available else None,
            "parsed_output": row.get("output") or "",
            "parsed_output_truncated": row.get("output_truncated"),
            "raw_response_available": raw_available,
            "raw_response_redacted": raw_redacted,
        }
    elif section == "request":
        selected = {
            "system_prompt": row.get("system_prompt") or "",
            "input": row.get("input") or "",
        }
    else:
        selected = metadata
    text = json.dumps(selected, ensure_ascii=False, default=str)
    end = min(offset + limit, len(text))
    has_more = end < len(text)
    return {
        "dispatch_id": dispatch_id,
        "metadata": metadata,
        "section": section,
        "content": text[offset:end],
        "content_format": "json",
        "offset": offset,
        "limit": limit,
        "total_chars": len(text),
        "has_more": has_more,
        "next_offset": end if has_more else None,
        "raw_response_available": raw_available,
        "raw_response_redacted": raw_redacted,
    }
