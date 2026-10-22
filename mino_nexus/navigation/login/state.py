"""会话事实失效：登录态一旦可疑，立刻把 ctx 的会话事实置为未知。

原在 `loop/observe/session_persist.py`；`services/resource_transition` 需要它，
但 services 不该为此 import loop。
"""
from __future__ import annotations

from typing import Any

from mino_nexus.navigation.login.transition import update_auth_state  # noqa: F401


def mark_session_dirty(ctx: Any, *, reason: str = "") -> None:
    ctx.session_dirty = True
    update_auth_state(ctx, device_session="unknown")
    update_auth_state(ctx, device_session_source="invalidated")
    update_auth_state(ctx, login_evidence={})
    ctx.web_auth_hint = None
    ctx.session_contradiction = None
    if reason not in {"login_form_visible", "login_submission_transition"}:
        from mino_nexus.navigation.login.policy import clear_submission
        clear_submission(ctx)
    prev = dict(getattr(ctx, "session_fact", None) or {})
    prev["session"] = "unknown"
    prev["login_state_verified"] = False
    if reason:
        prev["stale_reason"] = str(reason)[:200]
    update_auth_state(ctx, session_fact=prev)
    update_auth_state(ctx, task_session=dict(prev))
