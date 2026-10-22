"""导航键默认引用：纯 slug 规则，主键真源仍在 casework。"""
from __future__ import annotations

import re


def default_block_key_ref(block_id: str) -> str:
    bid = str(block_id or "").strip()
    slug = re.sub(r"[^\w]+", "_", bid).strip("_").lower()
    return f"operation.block.{slug or 'unknown'}"


def default_screen_key_ref(state_id: str) -> str:
    sid = str(state_id or "").strip()
    slug = re.sub(r"[^\w]+", "_", sid).strip("_").lower()
    return f"operation.nav.{slug or 'unknown'}"
