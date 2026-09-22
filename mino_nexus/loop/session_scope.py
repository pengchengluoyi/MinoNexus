"""用例级会话作用域：并行/乱序跑批时不把登记簿或其它用例结论当本任务真源。"""
from __future__ import annotations

from typing import Any

from mino_nexus.core.log import SLog
from mino_nexus.loop.session_persist import mark_session_dirty
from mino_nexus.runtime.session_gate import required_session

TAG = "SessionScope"


def begin_case_session_scope(ctx: Any, *, case_id: str, scene: dict[str, Any] | None) -> None:
    """开跑时标记本任务须自证 session；清掉可继承的 login 流标记。"""
    cid = str(case_id or "").strip()
    setattr(ctx, "case_session_scope_id", cid)
    mark_session_dirty(ctx, reason=f"case_start:{cid or '?'}")
    ctx.session_fact = {
        "session": "unknown",
        "identity": "",
        "seen": "",
        "source": "case_start",
        "reason": "新用例开跑，登录态须本任务 inspect/观察确认（勿沿用并行任务登记簿）",
    }
    for attr in (
        "_login_flow_transition_emitted",
        "login_flow_macro_active",
        "login_flow_interrupt",
        "picked_account",
    ):
        if hasattr(ctx, attr):
            try:
                delattr(ctx, attr)
            except Exception:
                setattr(ctx, attr, False if attr != "picked_account" else None)
    req = required_session(scene=scene)
    SLog.i(TAG, f"scope case={cid or '?'} required_session={req} session_dirty=1")
