"""文档库检索结果 → prompt 块。"""
from __future__ import annotations

from typing import Any


def build_doc_context(hits: list[dict[str, Any]], *, max_chars: int = 1200) -> str:
    """把 FTS 命中格式化为 agent-decide 的 doc_context 槽。"""
    budget = max(200, int(max_chars or 1200))
    lines: list[str] = []
    for row in hits or []:
        title = str(row.get("title") or "").strip()
        heading = str(row.get("heading") or "").strip()
        body = str(row.get("text") or row.get("snippet") or "").strip()
        if not body:
            continue
        head = f"《{title}》" if title else "（文档）"
        if heading:
            head += f" · {heading}"
        piece = f"- {head}\n{body}"
        if len(piece) > budget:
            if budget < 120:
                break
            piece = piece[: budget - 1].rstrip() + "…"
            lines.append(piece)
            break
        lines.append(piece)
        budget -= len(piece) + 1
        if budget < 120:
            break
    return "\n".join(lines).strip()
