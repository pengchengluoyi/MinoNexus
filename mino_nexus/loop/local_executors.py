"""Nexus 本地 executor：问人 / 等待 / 视觉断言，不出网。"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any

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


def dispatch_local(event: PlanEvent, *, shot: Any = None) -> EventResult:
    cap = event.capability_id
    t0 = time.time()
    if cap == "wait_ms":
        ms = int((event.params or {}).get("duration_ms") or (event.params or {}).get("ms") or 500)
        ms = max(0, min(ms, 60_000))
        if ms:
            time.sleep(ms / 1000.0)
        return _result(event, status=EventStatus.PASS, summary=f"等待 {ms}ms", elapsed_ms=int((time.time() - t0) * 1000))
    if cap == "wait_screen_ready":
        return _result(event, status=EventStatus.PASS, summary="跳过 wait_screen_ready（本仓只确认截图通道）", elapsed_ms=int((time.time() - t0) * 1000))
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

        res = verify_step_expected(
            expectation=expectation,
            image_base64=shot.image_base64,
            image_mime=getattr(shot, "image_mime", None) or "image/png",
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
        return _result(
            event,
            status=EventStatus.FAIL,
            summary="账号池尚未接线",
            error="lease_account 没有账号源",
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


__all__ = ["LOCAL_CAPS", "LOCAL_CAP_PREFIXES", "dispatch_local", "is_local_cap"]
