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

        image = ""
        mime = "image/png"
        if shot is not None and getattr(shot, "has_image", lambda: False)():
            image = shot.image_base64
            mime = getattr(shot, "image_mime", None) or "image/png"
        row = inspect_session(image_base64=image, image_mime=mime)
        session = str(row.get("session") or "unknown")
        reason = str(row.get("reason") or "")
        ok = bool(row.get("ok"))
        summary = reason or f"会话观察：{session}"
        return _result(
            event,
            status=EventStatus.PASS if ok else EventStatus.FAIL,
            summary=summary,
            error="" if ok else summary,
            executor="vlm",
            elapsed_ms=int((time.time() - t0) * 1000),
        )
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
