"""thought 与 decision capability 一致性（收工语义）。"""
from __future__ import annotations

import re

_THOUGHT_DONE_RE = re.compile(
    r"signal_done|本步.{0,8}完成|操作.{0,6}完成|应收工|前置.{0,8}(已|完成)|结束前置",
    re.I,
)


def thought_implies_signal_done(thought: str) -> bool:
    return bool(_THOUGHT_DONE_RE.search(str(thought or "")))


def should_coerce_mutate_to_signal_done(
    *,
    thought: str,
    cap_id: str,
    phase: str,
) -> bool:
    ph = str(phase or "")
    if ph not in ("do", "prep"):
        return False
    cap = str(cap_id or "").strip()
    if not cap or cap.startswith("signal_") or cap in ("wait_ms",):
        return False
    if cap.startswith("recover_"):
        return False
    return thought_implies_signal_done(thought)
