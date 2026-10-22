"""阶段契约的**只记录**校验：能力声明的 phases 与实际所处阶段不符时落一条事件。

为什么不是硬门禁：目录里有些行没有声明 `phases`（程序派发的系统能力、
临时注册的 recovery 能力），`phase_allows` 对这类行返回 False。
直接拿它当硬门禁会在真实跑批里拒掉合法调用，而且是静默拒（模型只会看到"被拒绝"）。

所以这里只写 `session_events`，不改控制流：
先攒够"到底有多少次真错阶段"的数据，再决定在哪一层收紧。
事件名 `phase/contract`，字段固定为 cap_id / phase / declared / verdict，
排查时按 verdict=mismatch 直接筛。
"""
from __future__ import annotations

from typing import Any

from mino_nexus.catalog.skill_channel import phase_allows

TAG = "PhaseContract"


def note_phase_contract(
    *,
    writer: Any,
    cap_id: str,
    phase: str,
) -> None:
    """把"这一轮的能力和阶段是否匹配"写进会话轨迹，不做拒绝。"""
    if writer is None:
        return
    cap = str(cap_id or "").strip()
    ph = str(phase or "").strip().lower()
    if not cap or not ph:
        return
    allowed = phase_allows(cap, ph)
    if allowed:
        return
    from mino_nexus.catalog.registry import get_capability

    row = get_capability(cap)
    declared = list(getattr(row, "phases", None) or []) if row is not None else []
    try:
        writer.append(
            "phase/contract",
            {
                "cap_id": cap,
                "phase": ph,
                "declared": declared,
                "verdict": "mismatch" if declared else "undeclared",
            },
        )
    except Exception:  # noqa: BLE001 — 记账失败不能拖垮跑批
        return
