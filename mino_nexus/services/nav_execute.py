"""Nav 边 execute 载荷字段（target_page 等）统一读取。"""
from __future__ import annotations

from typing import Any


def execute_target_page(execute: dict[str, Any] | None) -> str:
    """目标逻辑页：展示名/别名语义；兼容旧 target_tab。"""
    exe = execute if isinstance(execute, dict) else {}
    return str(exe.get("target_page") or exe.get("target_tab") or "").strip()
