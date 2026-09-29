"""同 SN 跑批连续性的机态推断（全托管：失败后续开跑前拒跑，不要求人工清设备）。"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class SnRunContinuity:
    avatar_profile_ready: bool = True
    block_reason: str = ""


def _pre_requires_avatar_configured(precondition: str) -> bool:
    pre = str(precondition or "")
    if re.search(r"未配置形象", pre, re.I):
        return False
    return bool(re.search(r"已配置形象|资料已填|头像已", pre, re.I))


def _pre_avatar_setup_lane(precondition: str) -> bool:
    return bool(re.search(r"未配置形象", str(precondition or ""), re.I))


def _case_establishes_avatar(case: dict[str, Any]) -> bool:
    pre = str(case.get("precondition") or "")
    if not _pre_avatar_setup_lane(pre):
        return False
    blob = " ".join(
        str(case.get(k) or "")
        for k in ("name", "steps_raw", "goal")
    )
    steps = case.get("steps")
    if isinstance(steps, list):
        for s in steps:
            if isinstance(s, dict):
                blob += " " + str(s.get("instruction") or s.get("text") or "")
            else:
                blob += " " + str(s)
    return bool(re.search(r"完成|形象", blob, re.I))


def continuity_blockers(
    state: SnRunContinuity,
    *,
    precondition: str,
) -> list[str]:
    """上一条用例的失败不拦截下一条。每条用例自己跑前置。"""
    _ = (state, precondition)
    return []


def update_after_case(
    state: SnRunContinuity,
    *,
    status: str,
    precondition: str,
    case: dict[str, Any],
    summary: str = "",
) -> None:
    st = str(status or "").strip().lower()
    pre = str(precondition or case.get("precondition") or "")
    if st == "pass":
        if _pre_requires_avatar_configured(pre):
            state.avatar_profile_ready = True
            state.block_reason = ""
        elif _case_establishes_avatar(case):
            state.avatar_profile_ready = True
            state.block_reason = ""
        return
    if st not in ("fail", "blocked"):
        return
    # 失败只记在本条用例上，不把摘要写成同设备后续用例的开跑失败。


def get_sn_state(store: dict[str, SnRunContinuity], sn: str) -> SnRunContinuity:
    key = str(sn or "").strip()
    if not key:
        return SnRunContinuity()
    if key not in store:
        store[key] = SnRunContinuity()
    return store[key]
