"""收工只看里程碑状态和 decision.status，不从模型 thought 里找固定句子。"""
from __future__ import annotations


def thought_implies_signal_done(thought: str) -> bool:
    del thought
    return False


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
