"""Run completion notifications for referenced workspaces; no legacy execution queue."""
from __future__ import annotations

from typing import Any

from mino_nexus.core.log import SLog

TAG = "CollabEvents"


def emit(kind: str, **ctx: Any) -> None:
    # Only referenced run completion is part of the V7 workspace contract.
    # Source, compilation and resource events must not awaken an execution queue.
    if kind != 'run_finished':
        return
    from mino_nexus.services.collab_workspace import notify_run_finished
    try:
        notify_run_finished(ctx.get('doc') or {})
    except Exception as exc:
        SLog.w(TAG, f"workspace notification: {type(exc).__name__}: {exc}")
    try:
        from mino_nexus.services.workspace_automation import on_finished
        on_finished(ctx.get('doc') or {})
    except Exception as exc:
        SLog.w(TAG, f"workspace automation: {type(exc).__name__}: {exc}")


def on_run_finished(doc: dict[str, Any]) -> None:
    emit("run_finished", doc=doc)
