"""Web 槽环境清理：由 Nexus 决定何时关页面 / 关 Chromium，Scout 只执行 close_app。

不改 agent 决策与步骤执行逻辑。
"""
from __future__ import annotations

from typing import Any, Optional

from mino_nexus.core.log import SLog
from mino_nexus.core.schemas import PlanEvent
from mino_nexus.loop.router_proxy import RouterProxy
from mino_nexus.runtime.run_context import is_web_slot

TAG = "WebEnv"

# 框架 step_idx：与 agent 步骤 1..N 错开，且每个 case 独占一段。
FRAME_STEP = 10_000


def frame_step(case_seq: int, slot: int) -> int:
    return FRAME_STEP + max(0, int(case_seq)) * 10 + int(slot)


def is_web_context(ctx: Any) -> bool:
    return is_web_slot(
        str(getattr(ctx, "sn", "") or ""),
        str(getattr(ctx, "platform", "") or ""),
    )


def case_keep_browser(case: dict[str, Any] | None) -> bool:
    """用例级开关：为 True 时 case 结束后不关 Chromium（只关 Context）。"""
    if not isinstance(case, dict):
        return False
    for key in ("web_keep_browser", "keep_browser"):
        if key in case:
            return bool(case.get(key))
    return False


def _dispatch_close(
    proxy: RouterProxy,
    *,
    run_id: str,
    step_idx: int,
    shutdown_browser: bool,
    label: str,
) -> None:
    params = {"shutdown_browser": True} if shutdown_browser else {}
    event = PlanEvent(
        seq=0,
        capability_id="close_app",
        event_kind="close_app",
        params=params,
        ai_reasoning=label,
        label=label,
        expected_executor="playwright",
    )
    try:
        proxy.dispatch(event, run_id=run_id, step_idx=step_idx)
    except Exception as exc:
        SLog.w(TAG, f"close_app sn={proxy.sn} step={step_idx} shutdown={shutdown_browser}: {exc!r}")


def reset_before_case(
    proxy: RouterProxy,
    ctx: Any,
    *,
    run_id: str,
    case_seq: int,
    case: dict[str, Any] | None = None,
) -> None:
    """下一 case 默认要干净环境：先关 Chromium（除非本 case 声明 keep_browser）。"""
    if not is_web_context(ctx):
        return
    if case_keep_browser(case):
        return
    _dispatch_close(
        proxy,
        run_id=run_id,
        step_idx=frame_step(case_seq, 0),
        shutdown_browser=True,
        label="用例开始前清理 Chromium",
    )


def cleanup_after_case(
    proxy: RouterProxy,
    ctx: Any,
    *,
    run_id: str,
    case_seq: int,
    case: dict[str, Any] | None = None,
) -> None:
    """Case 结束：默认关 Chromium；keep_browser 时只关 Context。"""
    if not is_web_context(ctx):
        return
    keep = case_keep_browser(case)
    _dispatch_close(
        proxy,
        run_id=run_id,
        step_idx=frame_step(case_seq, 9),
        shutdown_browser=not keep,
        label="用例结束后关闭页面" if keep else "用例结束后关闭 Chromium",
    )


def release_run(proxy: RouterProxy, *, run_id: str) -> None:
    """Run 结束或取消：Nexus 显式关 Chromium + 通知 Scout 清幂等缓存。"""
    _dispatch_close(
        proxy,
        run_id=run_id,
        step_idx=FRAME_STEP + 9998,
        shutdown_browser=True,
        label="任务结束关闭 Chromium",
    )
    try:
        proxy.notify_scout_cancel_run(run_id)
    except Exception as exc:
        SLog.w(TAG, f"cancel_run sn={proxy.sn}: {exc!r}")


def release_web_for_run(
    run_id: str,
    *,
    sns: Optional[list[str]] = None,
    platforms_by_sn: Optional[dict[str, str]] = None,
) -> None:
    """对任务里所有 Web 槽 best-effort 释放环境。"""
    seen: set[str] = set()
    for sn in sns or []:
        sn = str(sn or "").strip()
        if not sn or sn in seen:
            continue
        seen.add(sn)
        plat = (platforms_by_sn or {}).get(sn, "")
        if not is_web_slot(sn, plat):
            continue
        release_run(RouterProxy(sn, run_id=run_id), run_id=run_id)
