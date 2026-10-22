"""登录参考图：不可变的导航目标，不是当前证据。"""
from __future__ import annotations

from typing import Any

def _evidence_scope(ctx):
    from mino_nexus.navigation.login.evidence import scope
    return scope(ctx)





def restore_execution_reference(cursor, ctx):
    """Reference image is an immutable navigation target, never current evidence."""
    from mino_nexus.runtime.cursor_state import in_progress_milestone
    from mino_nexus.runtime.cursor_state import read_state
    from mino_nexus.navigation.login.evidence import can_commit
    row = in_progress_milestone(read_state(cursor)) or {}
    ref = getattr(cursor, "login_navigation_reference", None) or {}
    if (not str(row.get("id") or "").startswith("login_context_restore_")
            or ref.get("context_id") != id(ctx) or ref.get("scope") != _evidence_scope(ctx)
            or not ref.get("image_base64") or not can_commit(ctx)):
        return {}
    return {"image_base64": ref["image_base64"], "image_mime": ref["image_mime"]}
