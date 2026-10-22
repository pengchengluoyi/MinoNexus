"""页面就绪：Web 页面、桌面窗口、进度门与截图指纹。"""
from __future__ import annotations

from typing import Any

from mino_nexus.core.schemas import PlanEvent
from mino_nexus.loop.observe.execution_diagnostics import execution_result_metadata
from mino_nexus.loop.web.web_env import frame_step

_NON_FATAL_LOCAL_REASONS = frozenset(
    {"local_cap_misrouted", "cap_not_in_catalog", "no_impl_for_device", "fsm_degraded"}
)

def _program_executor(ctx: Any) -> str:
    from mino_nexus.contract.execution.ui import UiChannel, ui_channel_from_ctx

    channel = ui_channel_from_ctx(ctx)
    if channel == UiChannel.WEB:
        return "playwright"
    if channel == UiChannel.DESKTOP:
        return "desktop"
    if channel == UiChannel.IOS:
        return "ios_wda"
    return "adb"


def _screen_fp(shot, hierarchy_text: str = "", *, ctx: Any = None) -> str:
    from mino_nexus.loop.channel_observation import progress_key

    return progress_key(
        ctx,
        nodes=list(getattr(ctx, "nav_hierarchy_nodes", None) or []) if ctx is not None else None,
        hierarchy_text=str(hierarchy_text or ""),
        image_base64=getattr(shot, "image_base64", "") or "",
        width=int(getattr(shot, "width", 0) or 0),
        height=int(getattr(shot, "height", 0) or 0),
    )


def _progress_gate_web_kwargs(ctx: Any, proxy: Any) -> dict[str, Any]:
    from mino_nexus.contract.execution.ui import UiChannel, ui_channel_from_ctx
    from mino_nexus.loop.web.web_progress import refresh_web_focus, web_focus_fingerprint

    if ui_channel_from_ctx(ctx) != UiChannel.WEB:
        return {"web_channel": False, "web_focus_fp": ""}
    if proxy is not None:
        refresh_web_focus(ctx, proxy)
    return {
        "web_channel": True,
        "web_focus_fp": web_focus_fingerprint(getattr(ctx, "web_focus", None)),
    }


def _ensure_desktop_window(proxy: RouterProxy, ctx, *, run_id: str, case_seq: int = 0, writer=None) -> Optional[dict[str, Any]]:
    if _program_executor(ctx) != "desktop":
        return None
    package = str(getattr(ctx, "target_package", "") or "").strip()
    event = PlanEvent(
        seq=0, capability_id="launch_app", event_kind="launch_app", params={"package": package},
        ai_reasoning="PC 首张截图前先启动被测应用并确认目标窗口绑定", label="准备被测窗口",
        expected_executor="desktop",
    )
    if writer:
        writer.append("tool/call", {"capability_id": event.capability_id, "params": event.params})
    # Slot 4 is distinct from reset/cold-start (1..3) and app-version (1).
    result = proxy.dispatch(event, run_id=run_id, step_idx=frame_step(case_seq, 4))
    status = result.status.value if hasattr(result.status, "value") else str(result.status)
    return {
        "status": status, "summary": result.summary or "准备被测窗口",
        "error": result.error or "", "executor_used": result.executor_used or "desktop",
        "fatal": status != "pass", "execution_metadata": execution_result_metadata(result),
        "params": event.params, "thought": event.ai_reasoning,
    }


def _ensure_web_page(proxy: RouterProxy, ctx, *, run_id: str, case_seq: int = 0, writer=None) -> Optional[dict[str, Any]]:
    from mino_nexus.runtime.run_context import is_web_slot

    if not is_web_slot(str(getattr(ctx, "sn", "") or ""), str(getattr(ctx, "platform", "") or "")):
        return None
    url = str(getattr(ctx, "target_package", "") or "").strip()
    event = PlanEvent(
        seq=0,
        capability_id="launch_app",
        event_kind="launch_app",
        params={"url": url, "package": url} if url else {},
        ai_reasoning="Web 槽截图前先打开被测页",
        label="打开页面",
        expected_executor="playwright",
    )
    if writer:
        writer.append("tool/call", {"capability_id": event.capability_id, "params": event.params})
    result = proxy.dispatch(event, run_id=run_id, step_idx=frame_step(case_seq, 1))
    status_val = result.status.value if hasattr(result.status, "value") else str(result.status)
    err = result.error or result.summary or ""
    raw = result.raw_response if isinstance(getattr(result, "raw_response", None), dict) else {}
    local_reason = str(raw.get("local_reason") or "")
    fatal = status_val in ("fail", "failed", "declined", "blocked") and local_reason not in _NON_FATAL_LOCAL_REASONS
    return {
        "status": status_val,
        "summary": result.summary or (f"打开 {url}" if url else "打开空白页"),
        "error": result.error or "",
        "executor_used": result.executor_used or "playwright",
        "fatal": fatal,
        "execution_metadata": execution_result_metadata(result),
        "params": event.params,
        "thought": event.ai_reasoning,
    }


def _sync_app_foreground_ctx(
    ctx,
    nav: Any,
    target_pkg: str,
    router: Any = None,
) -> dict[str, Any]:
    nodes: list[Any] = []
    if nav is not None:
        snap = getattr(nav, "snapshot", None)
        if snap is not None:
            nodes = list(getattr(snap, "nodes", None) or [])
    ctx.nav_hierarchy_nodes = nodes
    ctx.observation_contract = dict(getattr(getattr(nav, "snapshot", None), "observation", None) or {})
    from mino_nexus.loop.recovery import resolve_app_foreground_guard

    fg = dict(getattr(nav, "foreground", None) or {}) or resolve_app_foreground_guard(
        ctx,
        router,
        nodes=nodes,
        target_package=str(target_pkg or getattr(ctx, "target_package", "") or ""),
    )
    ctx.system_overlay = str(fg.get("system_overlay") or "")
    ctx.app_foreground = str(fg.get("app_foreground") or "")
    return fg
