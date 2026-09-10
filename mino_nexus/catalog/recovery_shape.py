"""kind=recovery 的统一语义：同一 catalog kind，按 payload 形状拆成规则或原子能力。"""
from __future__ import annotations

from typing import Any

from mino_nexus.catalog.exec_classes import LOCAL_ORCH_IDS


def is_recovery_rule_payload(payload: dict[str, Any]) -> bool:
    """L0 恢复规则：match/actions/verify 编排。"""
    if not isinstance(payload, dict):
        return False
    if payload.get("actions"):
        return True
    match = payload.get("match") or {}
    if isinstance(match, dict):
        for key in ("evidence", "evidence_any", "screen_text_any", "top_window_pkg_prefix"):
            if match.get(key):
                return True
    if str(payload.get("mode") or "") == "execute" and payload.get("verify"):
        return True
    when = str(payload.get("when") or "").strip()
    if when and isinstance(match, dict) and match:
        return True
    return False


def is_recovery_atomic_payload(entry_id: str, payload: dict[str, Any]) -> bool:
    """恢复原子能力：implementations / 本地编排 / 资源网关。"""
    if not isinstance(payload, dict):
        return False
    if payload.get("implementations"):
        return True
    if str(entry_id or "") in LOCAL_ORCH_IDS:
        return True
    if payload.get("caller"):
        return True
    return False
