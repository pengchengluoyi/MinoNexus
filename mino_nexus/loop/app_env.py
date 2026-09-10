"""原生 App 槽环境：case 开环冷启动（关进程再 launch）。"""
from __future__ import annotations

from typing import Any

from mino_nexus.core.log import SLog
from mino_nexus.core.protocol import EventStatus
from mino_nexus.core.schemas import PlanEvent
from mino_nexus.loop.router_proxy import RouterProxy
from mino_nexus.loop.web_env import frame_step
from mino_nexus.runtime.run_context import is_web_slot
from mino_nexus.runtime.session_gate import is_login_module_case

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
) -> bool:
    """登录模块用例开环：close_app → wait → launch_app，减轻批跑页面栈污染。"""
    if is_web_slot(str(getattr(ctx, "sn", "") or ""), str(getattr(ctx, "platform", "") or "")):
        return False
    scene = getattr(ctx, "case_scene", None)
    if not login_module and not is_login_module_case(case=case, scene=scene):
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
    ok_launch = _dispatch(
        proxy,
        run_id=run_id,
        step_idx=base + 2,
        cap="launch_app",
        params={"package": pkg},
        label="登录模块用例开环：冷启动被测 App",
    )
    return ok_close or ok_launch


__all__ = ["reset_native_app_before_case"]
