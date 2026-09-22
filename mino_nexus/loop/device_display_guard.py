"""任务生命周期内：子线程轮询 probe/截图，黑屏或锁屏时 wake + dismiss；主循环每 turn 前 wait_ready。"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Optional

from mino_nexus.core.log import SLog
from mino_nexus.core.protocol import EventStatus

TAG = "DisplayGuard"
_GUARD_AGENT_TURN = 8800


def platform_needs_display_guard(ctx: Any) -> bool:
    plat = str(getattr(ctx, "platform", "") or "").lower()
    if not str(getattr(ctx, "sn", "") or "").strip():
        return False
    try:
        from mino_nexus.runtime.run_context import is_web_slot

        if is_web_slot(str(getattr(ctx, "sn", "") or ""), plat):
            return False
    except Exception:
        pass
    return plat not in ("web", "playwright", "")


def _infer_blocked_from_capture(ev: Any) -> None:
    blocked = str(getattr(ev, "screen_blocked", "") or "").strip().lower()
    if blocked == "no":
        return
    cap_black = str(getattr(ev, "capture_black", "") or "").strip().lower()
    cap_ok = str(getattr(ev, "capture_ok", "") or "").strip().lower()
    awake = str(getattr(ev, "awake", "") or "").strip().lower()
    if cap_ok == "no" and blocked in ("", "unknown"):
        setattr(ev, "screen_blocked", "yes")
        return
    if cap_black != "yes":
        return
    if blocked in ("", "unknown") and awake != "yes":
        setattr(ev, "screen_blocked", "yes")
    elif blocked in ("", "unknown") and str(getattr(ev, "locked", "") or "") == "yes":
        setattr(ev, "screen_blocked", "yes")


def _display_needs_fix(ev: Any) -> bool:
    blocked = str(getattr(ev, "screen_blocked", "") or "").strip().lower()
    if blocked == "yes":
        return True
    if str(getattr(ev, "awake", "") or "") == "no":
        return True
    if str(getattr(ev, "locked", "") or "") == "yes":
        return True
    cap_ok = str(getattr(ev, "capture_ok", "") or "").strip().lower()
    cap_black = str(getattr(ev, "capture_black", "") or "").strip().lower()
    if cap_ok == "no" and blocked in ("yes", "unknown", ""):
        return True
    if cap_black == "yes" and blocked != "no":
        return True
    return False


def _collect_evidence(ctx: Any, router: Any, *, target_package: str) -> Any:
    from mino_nexus.loop.recovery import collect_evidence

    ev = collect_evidence(ctx, router, target_package=target_package)
    if not hasattr(router, "observe"):
        _infer_blocked_from_capture(ev)
        return ev
    try:
        shot = router.observe("screenshot", force_fresh=False)
        from mino_nexus.loop.screen_capture import merge_shot_evidence

        merge_shot_evidence(ev, shot)
    except Exception:
        pass
    _infer_blocked_from_capture(ev)
    return ev


def _apply_wake_unlock(ctx: Any, router: Any, ev: Any, *, target_package: str) -> bool:
    from mino_nexus.core.schemas import PlanEvent
    from mino_nexus.loop.recovery import _dispatch, apply_rule, match_rules

    plat = str(getattr(ctx, "platform", "") or "")
    hits = match_rules(ev, platform=plat)
    for hit in hits:
        if hit.rule_id != "screen_asleep_or_locked":
            continue
        if str(getattr(hit.rule, "mode", "") or "") != "execute":
            continue
        try:
            out = apply_rule(
                hit,
                ctx,
                router,
                target_package=target_package,
                agent_turn=_GUARD_AGENT_TURN,
            )
            SLog.i(
                TAG,
                f"screen_asleep via rule recovered={out.recovered} actions={len(out.actions or [])} "
                f"{out.evidence or ev.brief()}",
            )
            return bool(out.applied or out.recovered)
        except Exception as exc:
            SLog.w(TAG, f"apply_rule screen_asleep failed: {exc!r}")

    ok_any = False
    for idx, cap in enumerate(("wake_screen", "dismiss_keyguard"), start=1):
        event = PlanEvent(
            seq=idx,
            capability_id=cap,
            event_kind=cap,
            params={},
            ai_reasoning="DisplayGuard：子线程唤醒/解锁",
            label=f"DisplayGuard {cap}",
        )
        try:
            res = _dispatch(
                router,
                ctx,
                event,
                agent_turn=_GUARD_AGENT_TURN,
                action_idx=idx,
            )
            st = getattr(res.status, "value", res.status)
            ok = str(st) in ("pass", EventStatus.PASS.value)
            ok_any = ok_any or ok
            SLog.i(
                TAG,
                f"direct {cap} status={st} summary={str(getattr(res, 'summary', '') or '')[:120]}",
            )
        except Exception as exc:
            SLog.w(TAG, f"direct {cap} failed: {exc!r}")
    return ok_any


class DeviceDisplayGuard:
    def __init__(
        self,
        ctx: Any,
        router: Any,
        *,
        target_package: str = "",
        cancel_check: Optional[Callable[[], bool]] = None,
        poll_sec: float = 2.0,
    ) -> None:
        self._ctx = ctx
        self._router = router
        self._pkg = target_package or str(getattr(ctx, "target_package", "") or "")
        self._cancel = cancel_check
        self._poll = max(0.5, float(poll_sec))
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self, *, initial_timeout: float = 90.0) -> None:
        setattr(self._ctx, "display_guard_active", True)
        self._ready.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="device-display-guard",
            daemon=True,
        )
        self._thread.start()
        if not self.wait_ready(timeout=initial_timeout):
            SLog.w(TAG, f"initial wait_ready timeout after {initial_timeout}s")

    def stop(self) -> None:
        self._stop.set()
        self._ready.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=8.0)
        setattr(self._ctx, "display_guard_active", False)

    def wait_ready(self, *, timeout: float = 60.0) -> bool:
        deadline = time.time() + max(0.0, float(timeout))
        while not self._stop.is_set():
            if self._cancel and self._cancel():
                return True
            if self._ready.wait(timeout=min(2.0, max(0.1, deadline - time.time()))):
                return True
            if time.time() >= deadline:
                return False
        return True

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                if self._cancel and self._cancel():
                    self._stop.set()
                    break
                ev = _collect_evidence(self._ctx, self._router, target_package=self._pkg)
                if not _display_needs_fix(ev):
                    self._ready.set()
                else:
                    self._ready.clear()
                    SLog.i(TAG, f"display needs wake/unlock {ev.brief()}")
                    _apply_wake_unlock(self._ctx, self._router, ev, target_package=self._pkg)
                if self._stop.wait(self._poll):
                    break
        except Exception as exc:
            SLog.e(TAG, f"guard thread crashed: {exc!r}")
        finally:
            self._ready.set()
