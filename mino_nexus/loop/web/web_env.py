"""Web 槽环境清理：由 Nexus 决定何时关页面 / 关 Chromium，Scout 只执行 close_app。

批内默认仍「每条用例可独立环境」；若**下一条/本条**前置要求已登录会话（logged_in + 非登录模块），
则跨用例保留 Chromium/Cookie。guest / 登录模块 / 清缓存等仍强制关浏览器再起。

不改 agent 决策与步骤执行逻辑。
"""
from __future__ import annotations

from typing import Any, Literal, Optional

WebBrowserPolicy = Literal["preserve", "reset"]

from mino_nexus.core.log import SLog
from mino_nexus.core.schemas import PlanEvent
from mino_nexus.loop.router_proxy import RouterProxy
from mino_nexus.runtime.run_context import is_web_slot

TAG = "WebEnv"

# 框架 step_idx：与 agent 步骤 1..N 错开，且每个 case 独占一段。
FRAME_STEP = 10_000
# 每个 case 内 slot 0/1/9 留给 reset / launch / cleanup；agent 回合从 10 起。
FRAME_AGENT_SLOT_BASE = 10


def frame_step(case_seq: int, slot: int) -> int:
    return FRAME_STEP + max(0, int(case_seq)) * 10 + int(slot)


def agent_step_idx(case_seq: int, turn: int) -> int:
    """Agent 第 turn 回合（1..N）对应的 Scout step_idx，与框架 slot 0/1/9 错开。"""
    slot = FRAME_AGENT_SLOT_BASE + max(1, int(turn))
    return frame_step(case_seq, slot)


def recovery_action_step_idx(case_seq: int, agent_turn: int, action_idx: int) -> int:
    """Recovery 规则内子动作的 step_idx，与 agent 回合 / fsm 内嵌 tap 的幂等键错开。

    Scout 以 (run_id, step_idx) 缓存 RESULT；若 recovery 复用 agent_step_idx(case, 1..3)，
    会误命中同回合 fsm_navigate 的 tap_element 缓存（表现为 close_app/launch_app「无坐标」）。
    """
    base = FRAME_STEP + max(0, int(case_seq)) * 10 + 40
    return base + max(0, int(agent_turn)) * 5 + max(1, int(action_idx))


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


def web_case_browser_policy(case: dict[str, Any] | None) -> WebBrowserPolicy:
    """本条用例开跑时是否应保留已有 Chromium（批内上一条留下的 Cookie/页面）。

    preserve：前置要求已登录且非登录模块，且未要求清缓存/登出 prep。
    reset：guest、登录模块、logout/relogin、清缓存、required_session=any 等。
    """
    if not isinstance(case, dict):
        return "reset"
    if case_keep_browser(case):
        return "preserve"
    pre = str(case.get("precondition") or "").strip()
    rk = case.get("resource_key") if isinstance(case.get("resource_key"), dict) else None
    scene_raw = case.get("case_scene") if isinstance(case.get("case_scene"), dict) else None
    if scene_raw is None and isinstance(case.get("scene"), dict):
        scene_raw = case.get("scene")
    from mino_nexus.runtime.session_gate import ensure_case_scene, is_login_module_case

    scene = ensure_case_scene(case, scene_raw)
    from mino_nexus.services.resource_preflight import claim_requires_clear_cache

    if claim_requires_clear_cache(
        rk,
        scene,
        pre,
    ):
        return "reset"
    prep = str(scene.get("session_prep") or "skip").strip().lower()
    req = str(scene.get("required_session") or "any").strip().lower()
    # CaseScene 已声明「已登录、不跑登录流」时优先保留浏览器（勿被前置里的「登录」字样误判为登录模块）。
    if req == "logged_in" and prep == "skip":
        return "preserve"
    if prep in ("logout", "relogin"):
        return "reset"
    if req == "guest":
        return "reset"
    if is_login_module_case(case, scene):
        return "reset"
    from mino_nexus.loop.session_ensure import account_need_from_case

    need = account_need_from_case(case, scene)
    session_hint = str(need.get("session") or "").strip().lower()
    if session_hint == "guest":
        return "reset"
    if session_hint == "logged_in":
        return "preserve"
    for item in scene.get("prep_items") or []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "").strip().lower()
        if kind in ("check_not_logged_in", "clear_cache"):
            return "reset"
    return "reset"


def should_shutdown_browser_before_case(case: dict[str, Any] | None) -> bool:
    return web_case_browser_policy(case) == "reset"


def should_shutdown_browser_after_case(
    case: dict[str, Any] | None,
    *,
    next_case: dict[str, Any] | None = None,
) -> bool:
    """用例结束是否关 Chromium。下一条 preserve 时保留给后续已登录用例。"""
    if not isinstance(case, dict):
        return True
    if case_keep_browser(case):
        return False
    if isinstance(next_case, dict) and web_case_browser_policy(next_case) == "preserve":
        return False
    return True


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
    """Web 批内：guest/登录模块等关 Chromium；已登录前置则保留上条 Cookie。"""
    if not is_web_context(ctx):
        return
    if not should_shutdown_browser_before_case(case):
        SLog.i(TAG, f"case_seq={case_seq} preserve browser (logged_in prep)")
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
    next_case: dict[str, Any] | None = None,
) -> None:
    """Case 结束：下一条已登录则保留 Chromium；guest 或末条则关。"""
    if not is_web_context(ctx):
        return
    if case_keep_browser(case):
        _dispatch_close(
            proxy,
            run_id=run_id,
            step_idx=frame_step(case_seq, 9),
            shutdown_browser=False,
            label="用例结束后关闭页面",
        )
        return
    if not should_shutdown_browser_after_case(case, next_case=next_case):
        SLog.i(TAG, f"case_seq={case_seq} keep Chromium for next logged_in case")
        return
    _dispatch_close(
        proxy,
        run_id=run_id,
        step_idx=frame_step(case_seq, 9),
        shutdown_browser=True,
        label="用例结束后关闭 Chromium",
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


def release_devices_for_run(
    run_id: str,
    *,
    sns: Optional[list[str]] = None,
    platforms_by_sn: Optional[dict[str, str]] = None,
) -> None:
    """任务结束/取消：Web 关页 + 各平台通知 Scout cancel_run。"""
    release_web_for_run(run_id, sns=sns, platforms_by_sn=platforms_by_sn)
    seen: set[str] = set()
    for sn in sns or []:
        sn = str(sn or "").strip()
        if not sn or sn in seen:
            continue
        seen.add(sn)
        plat = (platforms_by_sn or {}).get(sn, "")
        if is_web_slot(sn, plat):
            continue
        try:
            RouterProxy(sn, run_id=run_id).notify_scout_cancel_run(run_id)
        except Exception as exc:
            SLog.w(TAG, f"cancel_run sn={sn}: {exc!r}")
