"""OpenHands 集成：环境变量或 Workbench 写入 settings.integrations.openhands。"""
from __future__ import annotations

import os
from typing import Any

import httpx


def resolve_openhands_api_url() -> tuple[str, str]:
    """返回 (根地址, 来源 env|settings|none)。"""
    env = str(os.environ.get("OPENHANDS_API_URL") or "").strip().rstrip("/")
    if env:
        return env, "env"
    from mino_nexus.services.settings_store import get_openhands_settings

    cfg = get_openhands_settings()
    url = str(cfg.get("api_url") or "").strip().rstrip("/")
    if url:
        return url, "settings"
    return "", "none"


def openhands_is_configured() -> bool:
    return bool(resolve_openhands_api_url()[0])


def openhands_conversation_urls(base: str, payload: dict[str, Any]) -> dict[str, str]:
    """根据 OpenHands REST 响应拼可点击的会话 URL（OSS 本地与 Cloud 通用查询参数形式）。"""
    root = str(base or "").strip().rstrip("/")
    if not root or not isinstance(payload, dict):
        return {}
    cid = str(
        payload.get("conversation_id")
        or payload.get("app_conversation_id")
        or (payload.get("conversation") or {}).get("conversation_id")
        or ""
    ).strip()
    if not cid:
        return {}
    return {
        "conversation_id": cid,
        "ui_url": f"{root}/?conversation_id={cid}",
        "ui_url_alt": f"{root}/conversations/{cid}",
    }


def openhands_status() -> dict[str, Any]:
    from mino_nexus.services.settings_store import get_openhands_settings

    base, source = resolve_openhands_api_url()
    cfg = get_openhands_settings()
    if not base:
        return {
            "configured": False,
            "reachable": False,
            "url": "",
            "config_source": "none",
            "editable": bool(cfg.get("editable")),
            "message": "未配置 OpenHands 服务地址。",
            "setup": "在「Bot 与市场」填写 OpenHands 根地址并保存（例如 http://localhost:3000），或由管理员设置 OPENHANDS_API_URL。",
            "oss": "openhands",
            "ui_home": "https://github.com/All-Hands-AI/OpenHands",
        }
    try:
        resp = httpx.get(f"{base}/api/health", timeout=8.0)
        ok = resp.status_code < 500
        return {
            "configured": True,
            "reachable": ok,
            "url": base,
            "config_source": source,
            "editable": bool(cfg.get("editable")),
            "status_code": resp.status_code,
            "message": "OpenHands 已连接" if ok else f"OpenHands 返回 HTTP {resp.status_code}",
            "oss": "openhands",
            "ui_home": base,
            "embed_hint": "开发会话在 OpenHands 官方 UI 中继续；Workbench 提供跳转链接。",
        }
    except httpx.HTTPError as exc:
        return {
            "configured": True,
            "reachable": False,
            "url": base,
            "config_source": source,
            "editable": bool(cfg.get("editable")),
            "message": f"地址已保存，但无法连接：{exc}",
            "oss": "openhands",
            "ui_home": base,
        }


def save_openhands_api_url(api_url: str) -> dict[str, Any]:
    from mino_nexus.services.settings_store import save_openhands_settings

    save_openhands_settings(api_url)
    return openhands_status()
