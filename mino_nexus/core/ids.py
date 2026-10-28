"""业务 ID。前缀和主体直接相连，中间不加连接符。"""
from __future__ import annotations

import uuid
from datetime import datetime


def prefixed(prefix: str, hex_len: int = 0) -> str:
    """hex_len<=0 时用完整 32 位 hex。"""
    body = uuid.uuid4().hex if hex_len <= 0 else uuid.uuid4().hex[:hex_len]
    return f"{prefix}{body}"


def serial(existing: list[str], prefix: str) -> str:
    """前缀加自增序号。不足 3 位补零，超过 999 按实际位数。"""
    highest = 0
    width = len(prefix)
    for raw in existing:
        text = str(raw or "")
        tail = text[width:] if text.startswith(prefix) else ""
        if tail.isdigit():
            highest = max(highest, int(tail))
    number = highest + 1
    body = f"{number:03d}" if number < 1000 else str(number)
    return prefix + body


def task_id(when: datetime | None = None) -> str:
    """CR + 本地时间 yyMMddHHmmss + 3 位毫秒。"""
    when = when or datetime.now()
    return "CR" + when.strftime("%y%m%d%H%M%S") + f"{when.microsecond // 1000:03d}"
