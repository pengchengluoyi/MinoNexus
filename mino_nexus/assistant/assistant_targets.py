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
    from mino_nexus.resources import project_store as ps

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
    from mino_nexus.resources.project_env import resolve_profile_name
    document = ps.project_env(str(project["id"]))
    profiles = [row for row in document.get("environments") or [] if
                _mentioned(text, str(row.get("key") or "")) or
                (str(row.get("label") or "") not in ("测试", "") and _mentioned(text, str(row.get("label") or ""))) or
                (str(row.get("label") or "") and str(row["label"]) + "环境" in text)]
    environment = {"env_profile": str(profiles[0]["key"])} if len(profiles) == 1 else {}
    if len(hits) != 1:
        out = {"project_id": project["id"], "project_name": project.get("name") or "", **environment}
        explicit = [app for app in project.get("apps") or [] if any(_mentioned(text, str(app.get(key) or "")) for key in ("id", "name"))]
        if len(explicit) == 1:
            out.update(app_id=explicit[0]["id"], app_name=explicit[0].get("name") or "")
        return out
    channel = hits[0]
    apps = project.get("apps") or []
    explicit = [a for a in apps if any(_mentioned(text, str(a.get(k) or "")) for k in ("id", "name"))]
    selected = explicit if explicit else apps
    out = {"project_id": project["id"], "project_name": project.get("name") or "", **environment,
           "env_surface": channel["id"], "channel_label": channel.get("alias") or channel.get("label") or channel["id"],
           "target_platform": channel.get("platform") or ""}
    if len(selected) == 1:
        out.update(app_id=selected[0]["id"], app_name=selected[0].get("name") or "")
    return out


def channel_candidates(args: dict[str, Any], user_id: str, *, require_available: bool = True) -> dict[str, Any]:
    from mino_nexus.resources import project_store as ps
    from mino_nexus.resources.project_env import profile_snapshot, resolve_profile_name
    from mino_nexus.assistant.assistant_tools import _visible_devices

    app = ps.require_app(str(args["app_id"])) if args.get("app_id") else None
    pid = str((app or {}).get("project_id") or args.get("project_id") or "")
    doc = ps.project_env(pid)
    requested = str(args.get("env_profile") or "")
    environments = doc.get("environments") or []
    if requested:
        matched = [row for row in environments if requested in (str(row.get("key") or ""), str(row.get("label") or ""))]
        if len(matched) != 1:
            raise ValueError("本次环境不存在，请选择项目已经配置的运行环境。")
        requested = str(matched[0]["key"])
    profile = resolve_profile_name(doc, requested)
    snapshot = (doc.get("profiles") or {}).get(profile) or {}
    choices, unavailable = [], []
    devices = _visible_devices(user_id) if require_available and app else []
    for channel in doc.get("channels") or []:
        if app and channel.get("app_id") != app.get("id"):
            continue
        cid, field = str(channel["id"]), str(channel.get("field") or "value")
        value = str((snapshot.get(cid) or {}).get(field) or "").strip()
        label = str(channel.get("alias") or channel.get("label") or cid)
        row = {"id": cid, "label": label, "profile": profile, "configured": bool(value)}
        if not value:
            unavailable.append({**row, "reason": "当前环境尚未配置应用地址或标识"})
        elif channel.get("kind") == "server" or channel.get("platform") == "server":
            unavailable.append({**row, "reason": "当前 UI 测试不支持 Server 渠道"})
        elif require_available and app:
            compatible = compatible_devices({**args, "env_surface": cid, "env_profile": profile}, devices)
            if any(device.get("executable") for device in compatible):
                choices.append(row)
            else:
                reasons = list(dict.fromkeys(str(device.get("blocked_reason") or "设备未就绪") for device in compatible))
                unavailable.append({**row, "reason": "；".join(reasons) if reasons else "没有兼容的在线设备"})
        else:
            choices.append(row)
    return {"items": choices, "unavailable": unavailable, "env_profile": profile}


def compatible_devices(args: dict[str, Any], devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from mino_nexus.resources import project_store as ps
    from mino_nexus.resources.device_targets import evaluate, target_channel

    app = ps.require_app(str(args["app_id"])) if args.get("app_id") else None
    out = []
    for device in devices:
        if app is not None:
            try:
                target_channel(app, device, surface=str(args.get("env_surface") or ""), profile=str(args.get("env_profile") or ""))
            except (ValueError, KeyError):
                continue
        out.append(evaluate(device, app=app, surface=str(args.get("env_surface") or ""),
            profile=str(args.get("env_profile") or ""), action_scheme=str(args.get("action_scheme") or "dom_preferred"),
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
