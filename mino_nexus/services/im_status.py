from __future__ import annotations

from typing import Any


def identity_channel(channel: str) -> str:
    value = str(channel or "").removeprefix("langbot:")
    return {"lark": "feishu", "wecombot": "wecom", "weixin": "wechat"}.get(value, value)


def normalize_im_channels(plugins: Any) -> list[dict[str, Any]]:
    channels = []
    for plugin in plugins or []:
        if not isinstance(plugin, dict):
            continue
        pid = str(plugin.get("id") or "")
        status = plugin.get("status") if isinstance(plugin.get("status"), dict) else {}
        installed = plugin.get("installed") if isinstance(plugin.get("installed"), bool) else None
        running = status.get("state") == "running" if status.get("state") else None
        if pid == "wechat" and plugin.get("class") == "bot":
            entries = [("wechat", status)]
        elif pid == "langbot" and plugin.get("class") == "im":
            platforms = plugin.get("platforms") if isinstance(plugin.get("platforms"), dict) else {}
            entries = [(f"langbot:{platform}", state) for platform, state in platforms.items() if isinstance(state, dict)]
            if not entries:
                entries = [("langbot", {})]
        else:
            continue
        for channel, state in entries:
            reported = isinstance(state.get("connected"), bool)
            verified = pid != "langbot" or state.get("connection_verified") is True
            connected = state.get("connected") if reported and verified else None
            holding = state.get("holding") if isinstance(state.get("holding"), bool) else None
            error = str(state.get("error") or status.get("error") or "")[:200]
            try:
                last_message = int(state.get("last_message_at") or 0)
            except (TypeError, ValueError):
                last_message = 0
            if installed is False:
                connection_state = "not_installed"
            elif state.get("enabled") is False:
                connection_state = "disabled"
            elif error:
                connection_state = "error"
            elif holding is False:
                connection_state = "yielded"
            elif not reported:
                connection_state = "unreported"
            elif not verified:
                connection_state = "pending"
            elif connected:
                connection_state = "connected"
            else:
                connection_state = "disconnected"
            channels.append({
                "channel": channel,
                "instance_id": str(state.get("instance_id") or ""),
                "installed": installed,
                "running": running,
                "process_state": str(status.get("state") or ""),
                "enabled": state.get("enabled"),
                "connected": connected,
                "holding": holding,
                "connection_state": connection_state,
                "connection_verified": reported and verified,
                "connection_source": str(state.get("connection_source") or ""),
                "error": error,
                "last_message_at": last_message,
            })
    return channels
