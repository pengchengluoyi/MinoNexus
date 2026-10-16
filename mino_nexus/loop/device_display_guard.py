"""任务生命周期内：子线程轮询 probe/截图，黑屏或锁屏时 wake + dismiss；主循环每 turn 前 wait_ready。"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Optional

from mino_nexus.core.log import SLog

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
    # Desktop 的准入、捕获与输入由 Cua 校验，不套用手机 wake/unlock 轮询。
    return plat not in ("web", "playwright", "desktop", "mac", "win", "")


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
    from mino_nexus.loop.recovery import apply_rule, match_rules

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

    # 没有启用且匹配的目录规则时，不能用硬编码动作绕过配置。
    SLog.w(TAG, "没有匹配的屏幕唤醒/解锁规则，未下发恢复动作")
    return False


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
        self._failed = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self, *, initial_timeout: float = 90.0) -> None:
        self._ready.clear()
        from mino_nexus.core.execution_user import user_thread

        self._failed.clear()
        self._thread = user_thread(
            target=self._run,
            name="device-display-guard",
            daemon=True,
        )
        setattr(self._ctx, "display_guard_active", True)
        try:
            self._thread.start()
        except Exception:
            setattr(self._ctx, "display_guard_active", False)
            raise
        if not self.wait_ready(timeout=initial_timeout):
            write_stopped = bool(getattr(self._ctx, "recovery_write_stopped", False))
            reason = ("恢复写动作已停止，保留资源隔离" if write_stopped
                      else "设备屏幕守护未能就绪，交回主流程恢复")
            SLog.w(TAG, reason)
            self.stop()
            raise RuntimeError(reason)

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
            if self._failed.is_set():
                return False
            if self._cancel and self._cancel():
                return True
            if self._ready.wait(timeout=min(2.0, max(0.1, deadline - time.time()))):
                return True
            if time.time() >= deadline:
                return False
        return False

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                if self._cancel and self._cancel():
                    self._stop.set()
                    break
                if getattr(self._ctx, "recovery_write_stopped", False):
                    self._failed.set()
                    break
                ev = _collect_evidence(self._ctx, self._router, target_package=self._pkg)
                if not _display_needs_fix(ev):
                    self._ready.set()
                else:
                    self._ready.clear()
                    SLog.i(TAG, f"display needs wake/unlock {ev.brief()}")
                    _apply_wake_unlock(self._ctx, self._router, ev, target_package=self._pkg)
                    if getattr(self._ctx, "recovery_write_stopped", False):
                        self._failed.set()
                        break
                if self._stop.wait(self._poll):
                    break
        except Exception as exc:
            self._failed.set()
            from mino_nexus.loop.recovery import _trace_recovery

            _trace_recovery(self._ctx, "recovery/guard", {
                "status": "failed", "error_type": type(exc).__name__,
            })
            SLog.e(TAG, f"guard thread crashed: {exc!r}")
        finally:
            self._ready.clear()
