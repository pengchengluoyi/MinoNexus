"""IM 执行目标：渠道来自项目定义，设备兼容性复用任务准入。"""
from __future__ import annotations

import re
from typing import Any


def _mentioned(text: str, name: str) -> bool:
    value = str(name or "").strip()
    if not value:
        return False
    return bool(re.search(r"(?<![a-z0-9_])" + re.escape(value) + r"(?![a-z0-9_])", text, re.I))


def named_target(text: str) -> dict[str, Any]:
    """仅解析本条消息明确提到且唯一的项目/应用/渠道；不使用历史默认值。"""
    from mino_nexus.services import project_store as ps

    projects = ps.list_projects()
    named = [p for p in projects if any(_mentioned(text, str(p.get(k) or "")) for k in ("id", "name"))]
    if not named:
        named = [p for p in projects if any(_mentioned(text, str(a.get(k) or ""))
                 for a in p.get("apps") or [] for k in ("id", "name"))]
    if len(named) != 1:
        return {}
    project = named[0]
    channels = ps.project_env(str(project["id"])).get("channels") or []
    scored = [(max((len(str(c.get(k) or "")) for k in ("id", "alias", "label", "app_identifier")
                    if _mentioned(text, str(c.get(k) or ""))), default=0), c) for c in channels]
    longest = max((score for score, _ in scored), default=0)
    hits = [c for score, c in scored if score == longest and score > 0]
    if len(hits) != 1:
        return {"project_id": project["id"], "project_name": project.get("name") or ""}
    channel = hits[0]
    apps = project.get("apps") or []
    explicit = [a for a in apps if any(_mentioned(text, str(a.get(k) or "")) for k in ("id", "name"))]
    selected = explicit if explicit else apps
    out = {"project_id": project["id"], "project_name": project.get("name") or "",
           "env_surface": channel["id"], "channel_label": channel.get("alias") or channel.get("label") or channel["id"],
           "target_platform": channel.get("platform") or ""}
    if len(selected) == 1:
        out.update(app_id=selected[0]["id"], app_name=selected[0].get("name") or "")
    return out


def compatible_devices(args: dict[str, Any], devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from mino_nexus.services import project_store as ps
    from mino_nexus.services.device_targets import evaluate, target_channel

    app = ps.require_app(str(args["app_id"])) if args.get("app_id") else None
    out = []
    for device in devices:
        if app is not None:
            try:
                target_channel(app, device, surface=str(args.get("env_surface") or ""), profile=str(args.get("env_profile") or ""))
            except (ValueError, KeyError):
                continue
        out.append(evaluate(device, app=app, surface=str(args.get("env_surface") or ""),
            profile=str(args.get("env_profile") or ""), action_scheme=str(args.get("action_scheme") or "visual"),
            headless=bool(args.get("playwright_headless", True))))
    return out


def device_brief(row: dict[str, Any]) -> dict[str, Any]:
    """online/offline 只表示连通性；ready 是单独的桌面就绪事实。"""
    out = {k: row.get(k) for k in ("sn", "platform", "os", "model", "node_name", "status", "executable", "blocking_code", "blocked_reason") if row.get(k) is not None}
    platform = str(row.get("platform") or "")
    family = {"desktop": "PC 桌面", "web": "Web 浏览器", "android": "Android", "ios": "iOS"}.get(platform, platform)
    os_name = {"macos": "macOS", "windows": "Windows", "linux": "Linux"}.get(str(row.get("os") or ""), "")
    state = str(row.get("desktop_state") or "")
    if platform == "desktop":
        out["desktop_state"] = state
        out["desktop_state_label"] = "桌面就绪" if state == "ready" else str(row.get("blocked_reason") or state or "桌面未就绪")
    out["display_label"] = " · ".join(str(x) for x in (family, os_name, row.get("node_name"),
        f"状态 {row.get('status') or 'offline'}", out.get("desktop_state_label"), row.get("sn")) if x)
    return out


def device_result(rows: list[dict[str, Any]], *, targeted: bool) -> dict[str, Any]:
    available = [device_brief(r) for r in rows if r.get("executable")]
    blocked = [device_brief(r) for r in rows if not r.get("executable")]
    return {"count": len(available), "items": available, "unavailable": blocked,
            "target_filtered": targeted,
            "status_meaning": "status=online/offline 表示设备通道连通性；desktop_state=ready 表示桌面就绪，两者独立。"}
