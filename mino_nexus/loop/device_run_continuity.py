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
    if state.avatar_profile_ready:
        return []
    if not _pre_requires_avatar_configured(precondition):
        return []
    reason = state.block_reason.strip() or (
        "同设备上一条用例未完成形象/onboarding 链路，"
        "本用例前置要求「已配置形象」。Nexus 将自动跳过直至链路跑通。"
    )
    return [reason]


def _fail_clears_avatar_chain(*, summary: str, case: dict[str, Any], precondition: str) -> bool:
    """仅形象/onboarding 实质失败才连坐「已配置形象」用例；熔断/登录态漂移不算。"""
    pre = str(precondition or case.get("precondition") or "")
    if not (_pre_avatar_setup_lane(pre) or _case_establishes_avatar(case)):
        return False
    s = str(summary or "")
    if "【熔断" in s:
        return False
    if re.search(
        r"logged_in_session_drift|session_drift|设备未登录|登录态漂移|登录页.*个人",
        s,
        re.I,
    ):
        return False
    if re.search(r"无进展|fsm_navigate|导航", s, re.I) and not re.search(
        r"形象|头像|资料|onboarding", s, re.I
    ):
        return False
    # 机态已在个人中心/资料页，步骤却在找 onboarding 多头像 picker → 前置与真机不符，勿连坐「已配置形象」
    if re.search(
        r"不存在多个头像|没有完成按钮|未进入.*形象|未出现.*形象选择|形象选择页",
        s,
        re.I,
    ) and re.search(
        r"个人中心|个人页|3333|我的发布|作品集|资料已",
        s,
        re.I,
    ):
        return False
    return True


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
    if not _fail_clears_avatar_chain(summary=summary, case=case, precondition=pre):
        return
    if _pre_avatar_setup_lane(pre) or _case_establishes_avatar(case):
        state.avatar_profile_ready = False
        cid = str(case.get("case_id") or "")
        state.block_reason = (
            f"设备机态：形象配置链路未在本批跑通（最近失败 case={cid}）。"
            f"摘要：{str(summary or '')[:120]}"
        )


def get_sn_state(store: dict[str, SnRunContinuity], sn: str) -> SnRunContinuity:
    key = str(sn or "").strip()
    if not key:
        return SnRunContinuity()
    if key not in store:
        store[key] = SnRunContinuity()
    return store[key]
