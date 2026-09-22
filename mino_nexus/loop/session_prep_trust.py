"""前置收工：session_block / session_fact / 登记簿 分层信任，防并行污染与空收工。"""
from __future__ import annotations

from typing import Any, Optional

from mino_nexus.loop.session_ensure import parse_session_value, session_mismatch_reason


def prep_session_signal_done_block_reason(
    *,
    scene: dict[str, Any] | None,
    session_block: str,
    ctx: Any | None,
) -> Optional[str]:
    """prep + signal_done 是否允许收工。返回 None 表示通过。"""
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

    block_sess = parse_session_value(str(session_block or ""))
    fact: dict[str, Any] = dict(getattr(ctx, "session_fact", None) or {}) if ctx is not None else {}
    fact_sess = str(fact.get("session") or "").strip().lower()
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

    if dirty or fact_sess in ("", "unknown") or block_sess in ("", "unknown"):
        return (
            "前置要求已登录，但本任务尚未确认 session（"
            f"block={block_sess or 'empty'} fact={fact_sess or 'empty'}）。"
            "请先观察主界面或 inspect_session；并行跑批时勿信任登记簿 alone。"
            "禁止 signal_done。"
        )

    sn = str(getattr(ctx, "sn", "") or "").strip() if ctx is not None else ""
    pkg = str(getattr(ctx, "target_package") or "").strip() if ctx is not None else ""
    if sn and pkg and ctx is not None and not dirty:
        from mino_nexus.services.resource_preflight import effective_device_session

        cur = effective_device_session(ctx, sn=sn, package_id=pkg)
        if cur == "logged_in":
            return None

    return (
        "前置要求已登录，session 仍未确认。"
        "请 inspect_session 或完成租号后再 signal_done。"
    )
