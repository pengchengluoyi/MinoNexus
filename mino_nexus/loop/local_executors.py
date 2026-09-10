"""Nexus 本地 executor：问人 / 等待 / 视觉断言，不出网。"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from mino_nexus.services.account_lease import format_accounts_brief, lease_for_context
from mino_nexus.services.project_env import account_ident
from mino_nexus.loop.router_proxy import LOCAL_CAPS, LOCAL_CAP_PREFIXES, is_local_cap
from mino_nexus.core.protocol import EventStatus
from mino_nexus.core.schemas import EventResult, PlanEvent


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _result(event: PlanEvent, *, status: EventStatus, summary: str, elapsed_ms: int = 0, error: str = "", executor: str = "internal") -> EventResult:
    return EventResult(
        seq=event.seq,
        capability_id=event.capability_id,
        event_kind=event.event_kind or event.capability_id,
        status=status,
        executor_used=executor,
        elapsed_ms=elapsed_ms,
        summary=summary,
        error=error,
        ai_reasoning=event.ai_reasoning or "",
        plan_event=event.model_dump(exclude_none=True),
        started_at=_now(),
        finished_at=_now(),
    )


def dispatch_local(
    event: PlanEvent,
    *,
    shot: Any = None,
    ctx: Any = None,
    router: Any = None,
    target_package: str = "",
) -> EventResult:
    cap = event.capability_id
    t0 = time.time()
    if cap == "wait_ms":
        ms = int((event.params or {}).get("duration_ms") or (event.params or {}).get("ms") or 500)
        ms = max(0, min(ms, 60_000))
        if ms:
            time.sleep(ms / 1000.0)
        return _result(event, status=EventStatus.PASS, summary=f"等待 {ms}ms", elapsed_ms=int((time.time() - t0) * 1000))
    if cap == "wait_screen_ready":
        return _wait_screen_ready(
            event,
            shot=shot,
            ctx=ctx,
            router=router,
            target_package=target_package,
            t0=t0,
        )
    if cap.startswith("human_"):
        return _result(
            event,
            status=EventStatus.BLOCKED,
            summary="需要人在回路，HITL 界面尚未接线",
            error="HITL 尚未搬迁。请在 Studio 人工处理这一步，或改用例避开问人。",
            executor="hitl",
        )
    if cap == "assert_visual":
        params = event.params or {}
        expectation = str(params.get("expectation") or params.get("expected") or "").strip()
        has_image = bool(shot is not None and getattr(shot, "has_image", lambda: False)())
        if not has_image:
            return _result(
                event,
                status=EventStatus.FAIL,
                summary="视觉断言没有截图，不能记为通过",
                error="no screenshot",
                executor="vlm",
                elapsed_ms=int((time.time() - t0) * 1000),
            )
        from mino_nexus.ai.planner import verify_step_expected

        knowledge_ctx = str(params.get("knowledge_context") or "").strip()
        if knowledge_ctx:
            knowledge_ctx = f"==== 相关知识（供断言参考）====\n{knowledge_ctx}"

        res = verify_step_expected(
            expectation=expectation,
            image_base64=shot.image_base64,
            image_mime=getattr(shot, "image_mime", None) or "image/png",
            context_block=knowledge_ctx,
        )
        ok = bool(res.passed)
        summary = (res.evidence or res.ai_reasoning or "").strip() or (
            "预期成立" if ok else "预期未成立"
        )
        return _result(
            event,
            status=EventStatus.PASS if ok else EventStatus.FAIL,
            summary=summary,
            error="" if ok else summary,
            executor="vlm",
            elapsed_ms=int((time.time() - t0) * 1000),
        )
    if cap == "relogin":
        from mino_nexus.ai.planner import inspect_session
        from mino_nexus.runtime.session_gate import (
            format_required_session_brief,
            required_session as required_session_enum,
        )

        image = ""
        mime = "image/png"
        if shot is not None and getattr(shot, "has_image", lambda: False)():
            image = shot.image_base64
            mime = getattr(shot, "image_mime", None) or "image/png"
        scene = getattr(ctx, "case_scene", None) if ctx is not None else None
        req = required_session_enum(scene=scene)
        row = inspect_session(
            required_session=format_required_session_brief(scene),
            accounts_brief=str(getattr(ctx, "accounts_brief", "") or "") if ctx else "",
            image_base64=image,
            image_mime=mime,
        )
        session = str(row.get("session") or "unknown").strip().lower()
        reason = str(row.get("reason") or "").strip()
        if not row.get("ok"):
            summary = reason or "会话观察失败"
            return _result(
                event,
                status=EventStatus.FAIL,
                summary=summary,
                error=summary,
                executor="vlm",
                elapsed_ms=int((time.time() - t0) * 1000),
            )
        if req == "logged_in":
            if session == "logged_in":
                summary = reason or "session=logged_in"
                status = EventStatus.PASS
            else:
                summary = f"观察完成，登录未完成（session={session}）"
                if reason:
                    summary = f"{summary}；{reason}"
                status = EventStatus.FAIL
        elif req == "guest":
            summary = reason or f"会话观察：{session}"
            status = EventStatus.PASS if session in ("guest", "logged_out", "unknown") else EventStatus.PASS
        else:
            summary = reason or f"会话观察：{session}"
            status = EventStatus.PASS
        return _result(
            event,
            status=status,
            summary=summary,
            error="" if status == EventStatus.PASS else summary,
            executor="vlm",
            elapsed_ms=int((time.time() - t0) * 1000),
        )
    if cap == "check_run_env":
        return _check_run_env(event, ctx=ctx, t0=t0)
    if cap == "lease_account":
        params = dict(event.params or {})
        row, err = lease_for_context(
            ctx,
            params,
            ai_reasoning=str(event.ai_reasoning or ""),
        )
        if row:
            brief = format_accounts_brief(row)
            return _result(
                event,
                status=EventStatus.PASS,
                summary=brief or f"已租账号 {account_ident(row)}",
                executor="internal",
                elapsed_ms=int((time.time() - t0) * 1000),
            )
        return _result(
            event,
            status=EventStatus.FAIL,
            summary=err or "租号失败",
            error=err or "lease failed",
            executor="internal",
            elapsed_ms=int((time.time() - t0) * 1000),
        )
    if cap == "persona_subtask":
        return _result(
            event,
            status=EventStatus.DECLINED,
            summary="拟人化编排尚未搬迁",
            error="persona_subtask 本地 executor 尚未接线",
            executor="ai_persona",
        )
    return _result(
        event,
        status=EventStatus.DECLINED,
        summary=f"未知本地能力 {cap}",
        error=f"cap={cap} 标为本地但没有 executor",
    )


def _check_run_env(event: PlanEvent, *, ctx: Any, t0: float) -> EventResult:
    """确认本任务运行环境（每 run 一次），摘要供后续用例引用。"""
    from mino_nexus.catalog import registry as catalog_reg
    from mino_nexus.runtime.env_names import canon_run_env
    from mino_nexus.services import run_store

    run_id = str(getattr(ctx, "run_id", "") or "").strip() if ctx is not None else ""
    doc = run_store.get(run_id) if run_id else None
    existing = str((doc or {}).get("env_brief") or getattr(ctx, "env_label", "") or "").strip()
    if existing:
        if ctx is not None:
            ctx.env_label = existing
            ctx.env_fact = {"brief": existing, "confirmed": True}
        return _result(
            event,
            status=EventStatus.PASS,
            summary=existing,
            executor="internal",
            elapsed_ms=int((time.time() - t0) * 1000),
        )

    env_raw = str((doc or {}).get("env_profile") or getattr(ctx, "env_profile", "") or "test")
    env = canon_run_env(env_raw) or "test"
    platform = str(getattr(ctx, "platform", "") or "android") if ctx is not None else "android"
    flags = getattr(ctx, "connectivity_flags", None) if ctx is not None else {}
    has_otp = any(
        cap.id == "get_otp"
        for cap in catalog_reg.filter_capabilities(
            flags or {"internal": True},
            kinds=["prep", "recovery"],
            platform=platform,
        )
    )
    parts = [f"env={env}", f"platform={platform}", f"channel={platform}"]
    parts.append("otp_via=get_otp" if has_otp else "otp_via=manual")
    sn = str(getattr(ctx, "sn", "") or "").strip() if ctx is not None else ""
    if sn:
        parts.append(f"sn={sn}")
    brief = "; ".join(parts)
    if ctx is not None:
        ctx.env_profile = env
        ctx.env_label = brief
        ctx.env_fact = {
            "brief": brief,
            "env": env,
            "platform": platform,
            "otp_via": "get_otp" if has_otp else "manual",
            "confirmed": True,
        }
    if run_id and doc is not None:
        patched = dict(doc)
        patched["env_brief"] = brief
        run_store.put(patched)
    return _result(
        event,
        status=EventStatus.PASS,
        summary=brief,
        executor="internal",
        elapsed_ms=int((time.time() - t0) * 1000),
    )


def _wait_screen_ready(
    event: PlanEvent,
    *,
    shot: Any,
    ctx: Any,
    router: Any,
    target_package: str,
    t0: float,
) -> EventResult:
    from mino_nexus.loop.recovery import recover_if_needed
    from mino_nexus.loop.screen_capture import analyze_shot, shot_usable

    elapsed = lambda: int((time.time() - t0) * 1000)
    facts = analyze_shot(shot)
    need_recovery = facts.get("capture_ok") == "no" or facts.get("capture_black") == "yes"

    if not need_recovery:
        return _result(
            event,
            status=EventStatus.PASS,
            summary="屏幕内容可读",
            elapsed_ms=elapsed(),
        )

    if ctx is not None and router is not None:
        out = recover_if_needed(ctx, router, target_package=target_package, shot=shot)
        if out and out.recovered and hasattr(router, "observe"):
            fresh = router.observe("screenshot", force_fresh=True)
            if shot_usable(fresh):
                summary = out.summary() if callable(getattr(out, "summary", None)) else out.rule_id
                return _result(
                    event,
                    status=EventStatus.PASS,
                    summary=f"恢复后屏幕可读：{summary}",
                    elapsed_ms=elapsed(),
                )

    if router is not None and hasattr(router, "observe"):
        ms = min(int((event.params or {}).get("timeout_ms") or 3000), 15_000)
        if ms > 0:
            time.sleep(ms / 1000.0)
            fresh = router.observe("screenshot", force_fresh=True)
            if shot_usable(fresh):
                return _result(
                    event,
                    status=EventStatus.PASS,
                    summary=f"等待 {ms}ms 后屏幕可读",
                    elapsed_ms=elapsed(),
                )

    detail = (
        f"capture_ok={facts.get('capture_ok')} capture_black={facts.get('capture_black')}"
    )
    return _result(
        event,
        status=EventStatus.FAIL,
        summary=f"截图不可用或全黑，recovery 未能恢复（{detail}）",
        error="screen not readable",
        elapsed_ms=elapsed(),
    )


__all__ = ["LOCAL_CAPS", "LOCAL_CAP_PREFIXES", "dispatch_local", "is_local_cap"]
