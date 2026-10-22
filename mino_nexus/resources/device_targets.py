"""Shared task selection and dispatch checks, using authenticated Scout facts."""
from __future__ import annotations

import ntpath
from fastapi import HTTPException
from mino_nexus.resources import project_store as ps

_NATIVE_OS = {"mac": "macos", "macos": "macos", "win": "windows", "pc": "windows", "windows": "windows", "linux": "linux"}
_REQUIRED = {"screenshot", "tap_xy", "input_text"}


def target_channel(app: dict, row: dict, *, surface="", profile="", env_doc=None) -> dict:
    from mino_nexus.resources.app_automation import get_automation_config
    from mino_nexus.resources.project_env import resolve_run_env_profile
    cfg = get_automation_config(app, hydrate_cases=False)
    env = env_doc if env_doc is not None else ps.project_env(str(app.get("project_id") or ""))
    resolved = resolve_run_env_profile(env, requested=profile, automation_default=str(cfg.get("env_profile") or ""))
    channels = [c for c in env.get("channels") or []
                if not c.get("app_id") or c.get("app_id") == app.get("id")]
    owned = [c for c in channels if c.get("app_id") == app.get("id")]
    if owned:
        channels = owned
    chosen = surface or str(cfg.get("env_surface") or "")
    if chosen:
        channel = next((c for c in channels if c.get("id") == chosen), None)
        if channel is None:
            raise ValueError("被测应用渠道不存在，请重新选择")
    else:
        snap = (env.get("profiles") or {}).get(resolved) or {}
        configured = [c for c in channels if str((snap.get(c.get("id")) or {}).get(c.get("field") or "value") or "").strip()]
        if row.get("platform") == "desktop":
            if len(configured) != 1:
                raise ValueError("请明确选择被测应用的 macOS / Windows / Linux 原生渠道")
            channel = configured[0]
        else:
            compatible = [c for c in configured if c.get("platform") == row.get("platform")]
            if configured and len(compatible) != 1:
                raise ValueError("请明确选择与执行设备兼容的被测应用渠道")
            channel = compatible[0] if compatible else {"platform": row.get("platform"), "id": ""}
    platform = str(channel.get("platform") or "")
    native_os = _NATIVE_OS.get(platform)
    if native_os:
        if ((row.get("platform") in {"mac", "win"} and platform != row.get("platform"))
                or (row.get("platform") not in {"mac", "win"} and
                    (row.get("platform") != "desktop" or row.get("os") != native_os))):
            raise ValueError(f"原生 {native_os} 应用需要同系统的 PC 桌面")
    elif platform != row.get("platform"):
        raise ValueError("设备与被测应用渠道不兼容；网站请选择 Web 浏览器")
    if row.get("platform") in {"desktop", "mac", "win"} and not native_os:
        raise ValueError("缺少明确的原生应用目标系统")
    if native_os:
        identifier = str(((env.get("profiles") or {}).get(resolved) or {}).get(channel.get("id"), {}).get(channel.get("field") or "value") or "").strip()
        if not identifier or any(ord(char) < 32 for char in identifier):
            raise ValueError("请配置被测 PC 应用标识")
        if native_os == "macos" and ("." not in identifier or not all(c.isalnum() or c in "._-" for c in identifier)):
            raise ValueError("macOS 应用必须配置 bundle ID")
        if native_os == "windows":
            drive, tail = ntpath.splitdrive(identifier)
            executable = bool(drive and tail.startswith(("\\", "/")) and identifier.lower().endswith(".exe"))
            aumid = identifier.count("!") == 1 and all(part and all(c.isalnum() or c in "._-" for c in part) for part in identifier.split("!"))
            if not executable and not aumid:
                raise ValueError("Windows 应用必须配置完整 EXE 路径或 AUMID")
    return {**channel, "env_profile": resolved}


def evaluate(row: dict, *, app: dict | None = None, surface="", profile="", headless=True, action_scheme="dom_preferred", allow_queue=False) -> dict:
    from mino_nexus.resources.device_admission import busy_for
    out = dict(row)
    code, reason = "", ""
    platform = str(row.get("platform") or row.get("type") or "")
    from mino_nexus.platform.identity import current_user_id
    owner = str(row.get("owner_user_id") or "")
    if not owner:
        code, reason = "unclaimed", "历史节点未归属，请由管理员认领后执行"
    elif current_user_id() and owner != current_user_id():
        code, reason = "not_owned", "设备不属于当前账号"
    elif not row.get("node_alive", row.get("status") == "online"):
        code, reason = "node_offline", "所属 Scout 节点离线"
    elif platform in {"desktop", "mac", "win"}:
        if not row.get("session_generation") or row.get("capabilities") is None:
            code, reason = "desktop_unreported", "未上报桌面能力，请升级 Scout"
        elif row.get("desktop_state") != "ready":
            code, reason = str(row.get("desktop_state") or "desktop_unreported"), str(row.get("blocked_reason") or "桌面未就绪")
        elif not _REQUIRED.issubset(set(row.get("capabilities") or [])):
            code, reason = "capability_missing", "桌面缺少截图、点击或输入能力"
        elif row.get("status") != "online":
            code, reason = "channel_offline", "Desktop 执行通道未连接"
        elif action_scheme not in ("visual", "dom", "dom_preferred"):
            code, reason = "scheme_unsupported", "未知执行策略"
    elif platform not in ("android", "ios", "web"):
        code, reason = "platform_unsupported", "设备平台没有对应执行通道"
    elif row.get("status") != "online":
        code, reason = "channel_offline", "设备执行通道未连接"
    if not code and app is not None:
        try:
            channel = target_channel(app, row, surface=surface, profile=profile)
            out["target_surface"] = channel.get("id") or ""
            out["target_platform"] = channel.get("platform") or ""
        except (ValueError, KeyError) as exc:
            code, reason = "app_incompatible", str(exc)
    if not code and not (allow_queue and platform == "web" and headless):
        busy = busy_for(row, headless=headless)
        if busy:
            code, reason = "busy", "设备或共享桌面繁忙，请等待当前任务结束"
    out.update(executable=not bool(code), blocking_code=code, blocked_reason=reason)
    return out


def select(app: dict, sns: list[str], **kwargs) -> tuple[list[str], dict[str, dict]]:
    from mino_nexus.resources.access_scope import require_owned_device
    from mino_nexus.resources.ui_devices import ui_devices
    roster = {r["sn"]: r for r in ui_devices()}
    if not sns:
        # Desktop selection is always explicit, never choose a different PC.
        candidates = [evaluate(r, app=app, **kwargs) for r in roster.values() if r.get("platform") not in {"desktop", "mac", "win"}]
        sns = [r["sn"] for r in candidates if r["executable"]][:1]
    if not sns:
        raise ValueError("没有兼容且就绪的执行设备，请在 Workbench 选择设备")
    targets = {}
    for sn in sns:
        require_owned_device(sn)
        row = roster.get(sn)
        if row is None:
            raise HTTPException(status_code=404, detail="没有这个设备")
        target = evaluate(row, app=app, **kwargs)
        if not target["executable"]:
            raise HTTPException(status_code=409, detail={"sn": sn, "blocking_code": target["blocking_code"], "message": target["blocked_reason"]})
        targets[sn] = target
    return sns, targets
