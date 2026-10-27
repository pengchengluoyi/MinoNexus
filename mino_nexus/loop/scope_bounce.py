"""校验发现「页面不对」时退回操作阶段（有预算、有次数上限）。

`check_scope_mismatch` 的原状是：do 阶段以为到达了目标页，check 阶段一看截图不是，
于是整条用例以 `unverified` 结束 —— 但预算往往只用了 20%，而校验阶段按设计不能点屏幕
（cr-869c1b6bc98b 的 api-1-212：57 秒、预算 300 秒、实际停在相机取景界面）。

这里只做一件事：把这次 mismatch 当成「操作阶段还没走到位」，退回 do 让模型重新到达；
同一步只用一次，预算不足就直接放弃（仍按原来的 blocked 记账）。
"""
from __future__ import annotations

from typing import Any

SCOPE_MISMATCH_CODES = ("check_scope_mismatch", "check_observation_channel_mismatch")
BOUNCE_BUDGET = 1


def scope_budget_left(ctx: Any) -> bool:
    from mino_nexus.resources.run_budget import remaining

    case = getattr(ctx, "case", None) or {}
    left = remaining(getattr(ctx, "execution_policy", None) or {}, case if isinstance(case, dict) else None)
    return True if left is None else left > 30


def bounce_to_do_after_scope_mismatch(
    ctx: Any,
    cursor: Any,
    *,
    case_step: int,
    evidence: str,
    writer: Any = None,
) -> bool:
    """页面不对 + 还有预算 + 这一步还没退过 → 退回 do。返回是否真的退了。"""
    used = dict(getattr(cursor, "check_scope_bounces", None) or {})
    key = str(int(case_step or 0))
    if int(used.get(key) or 0) >= BOUNCE_BUDGET:
        return False
    if not scope_budget_left(ctx):
        return False
    if not hasattr(cursor, "reopen_do") or not cursor.reopen_do(case_step=int(case_step or 0)):
        return False
    used[key] = int(used.get(key) or 0) + 1
    cursor.check_scope_bounces = used
    from mino_nexus.loop.milestones import read_state, write_state

    state = read_state(cursor)
    reopened = ""
    # 校验阶段能进来，说明 do 的里程碑都已经是 pass —— 要重开的是**最后一个** do 步骤，
    # 否则模型手上没有任何待做项，只会立刻再宣告完成，白白耗掉一次退让机会。
    while not reopened and state.get("milestones"):
        last = None
        for row in state.get("milestones") or []:
            if isinstance(row, dict) and str(row.get("id") or "").startswith("do_"):
                last = row
        if last is None:
            break
        last["status"] = "pending"
        last["kind"] = "visual_tap"
        last["device_cap"] = "tap_element"
        last["title"] = str(last.get("title") or "") + "（校验发现不在目标页面，需重新到达）"
        last["evidence"] = str(evidence or "")[:300]
        reopened = str(last.get("id") or "")
        break
    write_state(cursor, state)
    hint = ("校验时截图显示当前不是本步骤要求的页面（" + str(evidence or "")[:140] +
            "）。先用操作把页面走到位，再重新校验；不要直接宣告完成。")
    if hasattr(cursor, "correction_hint"):
        cursor.correction_hint = hint
    if writer:
        writer.append("check/scope_bounce", {
            "case_step": int(case_step or 0),
            "reason_code": "check_scope_mismatch",
            "evidence": str(evidence or "")[:400],
            "reopened_milestone": reopened,
            "bounces_used": used[key],
            "to_phase": "do",
        })
    return True
