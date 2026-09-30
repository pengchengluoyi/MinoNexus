"""前置收工：session_block / session_fact / 登记簿 分层信任，防并行污染与空收工。"""
from __future__ import annotations

import re
from typing import Any, Optional

from mino_nexus.loop.session_ensure import parse_session_value, session_mismatch_reason


def _history_has_lease_account_pass(history_lines: list[str] | None) -> bool:
    for line in history_lines or []:
        if "lease_account" not in line:
            continue
        if "→ pass" in line or re.search(r"\bpass\b", line, re.I):
            return True
    return False


def prep_lease_account_signal_done_block_reason(
    *,
    ctx: Any | None,
    case: dict[str, Any] | None,
    scene: dict[str, Any] | None,
    history_lines: list[str] | None = None,
) -> Optional[str]:
    """need_account 时须先 lease_account，禁止前置看屏假收工。"""
    from mino_nexus.loop.session_ensure import account_need_from_case

    need = account_need_from_case(
        case if isinstance(case, dict) else {},
        scene if isinstance(scene, dict) else {},
    )
    if not need.get("need_account"):
        return None
    if ctx is not None and bool(getattr(ctx, "prep_lease_account_done", False)):
        return None
    if ctx is not None:
        lease = getattr(ctx, "resource_lease", None)
        if isinstance(lease, dict) and str(lease.get("account_id") or "").strip():
            return None
        picked = getattr(ctx, "picked_account", None)
        if isinstance(picked, dict) and str(
            picked.get("account_id") or picked.get("id") or ""
        ).strip():
            return None
    if _history_has_lease_account_pass(history_lines):
        return None
    return (
        "【前置门槛】本用例需要号池账号：须先调用 lease_account 完成筛选租号，"
        "再 signal_done。禁止仅凭界面猜测「未注册/未登录」跳过租号。"
    )


def prep_session_signal_done_block_reason(
    *,
    scene: dict[str, Any] | None,
    session_block: str,
    ctx: Any | None,
) -> Optional[str]:
    """prep + signal_done 是否允许收工。返回 None 表示通过。"""
    case_row = getattr(ctx, "case", None) if ctx is not None else None
    hist = getattr(ctx, "prep_signal_done_history", None) if ctx is not None else None
    lease_reason = prep_lease_account_signal_done_block_reason(
        ctx=ctx,
        case=case_row if isinstance(case_row, dict) else None,
        scene=scene,
        history_lines=list(hist or []),
    )
    if lease_reason:
        return lease_reason

    from mino_nexus.runtime.session_gate import required_session

    req = required_session(scene=scene)
    if not req or req == "any":
        return None

    reason = session_mismatch_reason(scene=scene, session_block=str(session_block or ""))
    if reason and req == "guest":
        return reason

    if req == "guest":
        return None

    if req != "logged_in":
        return reason

    from mino_nexus.services.session_match import normalize_device_session

    block_sess = normalize_device_session(parse_session_value(str(session_block or "")))
    fact: dict[str, Any] = dict(getattr(ctx, "session_fact", None) or {}) if ctx is not None else {}
    fact_sess = normalize_device_session(fact.get("session") or "")
    dirty = bool(getattr(ctx, "session_dirty", False)) if ctx is not None else True

    if block_sess in ("logged_out", "guest") or fact_sess in ("logged_out", "guest"):
        return (
            "当前未登录，本条要求已登录。"
            "请先 lease_account / inspect_session 确认，禁止 signal_done。"
        )

    if fact_sess == "logged_in" and not dirty:
        return None
    if block_sess == "logged_in":
        return None

    sn = str(getattr(ctx, "sn", "") or "").strip() if ctx is not None else ""
    pkg = str(getattr(ctx, "target_package") or "").strip() if ctx is not None else ""
    if sn and pkg and ctx is not None and not dirty:
        from mino_nexus.services.resource_preflight import effective_device_session

        cur = effective_device_session(ctx, sn=sn, package_id=pkg)
        if cur == "logged_in":
            return None

    return (
        "当前不是已登录，本条要求已登录。"
        "请先完成登录后再 signal_done。"
    )
