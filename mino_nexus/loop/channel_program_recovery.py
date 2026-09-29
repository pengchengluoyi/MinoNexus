"""按 UiChannel 门控程序恢复（P2）：Web 不 launch 原生 App。"""
from __future__ import annotations

from typing import Any, Optional

from mino_nexus.loop.ui_channel import UiChannel, ui_channel_from_ctx

# 仅 Android / iOS 程序恢复；Web 走浏览器前台逻辑
_NATIVE_ONLY_CAPS = frozenset(
    {
        "launch_app",
        "reset_native_app_before_case",
        "clear_app_cache",
        "force_stop_app",
    }
)


def channel_supports_capability(ctx: Any, capability_id: str) -> bool:
    cap = str(capability_id or "").strip()
    if not cap:
        return True
    ch = ui_channel_from_ctx(ctx)
    if ch == UiChannel.WEB and cap in _NATIVE_ONLY_CAPS:
        return False
    return True


def maybe_recover_foreground(
    proxy: Any,
    ctx: Any,
    *,
    run_id: str,
    case_seq: int,
    nodes: list[dict[str, Any]] | None,
) -> Optional[dict[str, Any]]:
    """hierarchy 错前台时程序拉起；Web 槽直接跳过。"""
    if ui_channel_from_ctx(ctx) == UiChannel.WEB:
        return None
    from mino_nexus.loop.app_env import launch_if_hierarchy_away

    return launch_if_hierarchy_away(
        proxy,
        ctx,
        run_id=run_id,
        case_seq=case_seq,
        nodes=nodes,
    )


__all__ = [
    "channel_supports_capability",
    "maybe_recover_foreground",
    "UiChannel",
]
