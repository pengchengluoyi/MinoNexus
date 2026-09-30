"""原生 App 槽环境：case 开环冷启动（关进程再 launch）。"""
from __future__ import annotations

from typing import Any

from mino_nexus.core.log import SLog
from mino_nexus.core.protocol import EventStatus
from mino_nexus.core.schemas import PlanEvent
from mino_nexus.loop.router_proxy import RouterProxy
from mino_nexus.loop.web.web_env import frame_step
from mino_nexus.runtime.run_context import is_web_slot
TAG = "AppEnv"


def _dispatch(proxy: RouterProxy, *, run_id: str, step_idx: int, cap: str, params: dict, label: str) -> bool:
    event = PlanEvent(
        seq=0,
        capability_id=cap,
        event_kind=cap,
        params=dict(params or {}),
        ai_reasoning=label,
        label=label[:80],
    )
    try:
        res = proxy.dispatch(event, run_id=run_id, step_idx=step_idx)
        st = res.status.value if hasattr(res.status, "value") else str(res.status)
        return st == EventStatus.PASS.value or st == "pass"
    except Exception as exc:
        SLog.w(TAG, f"{cap} sn={proxy.sn} step={step_idx}: {exc!r}")
        return False


def reset_native_app_before_case(
    proxy: RouterProxy,
    ctx: Any,
    *,
    run_id: str,
    case_seq: int,
    case: dict[str, Any] | None = None,
    login_module: bool = False,
    defer_launch: bool = False,
) -> bool:
    """可选开环：close_app → wait → launch_app。仅当调用方显式 login_module=True 时执行（默认不自动冷启动）。

    defer_launch=True：仅关进程不 launch（前置须先 clear_app_cache 再打开应用）。
    """
    if is_web_slot(str(getattr(ctx, "sn", "") or ""), str(getattr(ctx, "platform", "") or "")):
        return False
    if not login_module:
        return False
    pkg = str(getattr(ctx, "target_package", "") or "").strip()
    if not pkg:
        return False
    base = frame_step(case_seq, 1)
    ok_close = _dispatch(
        proxy,
        run_id=run_id,
        step_idx=base,
        cap="close_app",
        params={},
        label="登录模块用例开环：关闭被测 App",
    )
    _dispatch(
        proxy,
        run_id=run_id,
        step_idx=base + 1,
        cap="wait_ms",
        params={"duration_ms": 1500},
        label="冷启动等待",
    )
    if defer_launch:
        return ok_close
    ok_launch = _dispatch(
        proxy,
        run_id=run_id,
        step_idx=base + 2,
        cap="launch_app",
        params={"package": pkg},
        label="登录模块用例开环：冷启动被测 App",
    )
    return ok_close or ok_launch


def launch_if_hierarchy_away(
    proxy: RouterProxy,
    ctx: Any,
    *,
    run_id: str,
    case_seq: int,
    nodes: list[dict[str, Any]] | None,
) -> dict[str, Any] | None:
    """hierarchy 已能看出桌面/错包时，程序 launch_app，不把点击交给模型。每案最多一次。"""
    if is_web_slot(str(getattr(ctx, "sn", "") or ""), str(getattr(ctx, "platform", "") or "")):
        return None
    if getattr(ctx, "_program_launched_app", False):
        return None
    pkg = str(getattr(ctx, "target_package", "") or "").strip()
    if not pkg:
        return None
    from mino_nexus.services.nav_capture_store import run_guard_foreground

    fg = run_guard_foreground(
        nodes or [],
        target_package=pkg,
        platform=str(getattr(ctx, "platform", "") or ""),
    )
    if str(fg.get("app_foreground") or "") != "no":
        return None
    ok = _dispatch(
        proxy,
        run_id=run_id,
        step_idx=frame_step(case_seq, 3),
        cap="launch_app",
        params={"package": pkg},
        label="hierarchy 判定前台不是被测 App，程序启动",
    )
    setattr(ctx, "_program_launched_app", True)
    return {
        "status": "pass" if ok else "fail",
        "summary": f"程序启动 {pkg}（前台不是被测应用）",
        "package": pkg,
    }


__all__ = ["reset_native_app_before_case", "launch_if_hierarchy_away"]
